"""Sealed browser profiles and the profile provisioner (slice 8A.1, ADR-0020 §2 and §8).

A shop's Chromium profile (`user_data_dir`) exists at rest only as a **sealed archive**: a
gzip tar of the profile's files, sealed with envelope encryption under the browser profile key
ring (separate from the Etsy token vault's), with `org|shop|environment` as associated data. An
archive copied to another shop's or environment's row therefore does not open (AC-8A.1.1).

The archives live in `app.browser_profile_archives`, which only the browser worker's role
(`htco_browser`) can read or write, at most `MAX_ARCHIVE_BYTES` each (main's decision of
2026-10-25: no Supabase Storage key in the browser worker). `profile_storage_key` is the logical
name, `browser-profiles/{org}/{shop}/{environment}`, recorded on the environment's row.

`ProfileWorkspaces.checkout` unpacks the archive into a fresh private directory under the
worker's tmpfs root for one session. On a clean exit the directory is packed and sealed again
(the new last good archive); on any exit the directory is wiped, so nothing of the profile stays
on disk (AC-8A.1.3). A failed session keeps the previous archive. Packing keeps regular files
and directories only, without Chromium's caches, links, sockets or lock files; unpacking refuses
any other entry, any path outside the directory, and archives past the size and member caps.

`SealedProfileProvisioner` is the real `EnvironmentProvisioner`: it seals an empty profile for a
`PROVISIONING` environment, records its storage key and key ID, then runs the first health check.
A new profile is signed out, so without a check (until slice 8A.3) it ends `LOGIN_REQUIRED`.
"""

import io
import os
import shutil
import stat
import tarfile
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from htco_application.browser_environments import (
    EnvironmentNotFoundError,
    ProvisioningResult,
    StaleEnvironmentError,
)
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus

MAX_ARCHIVE_BYTES = 25 * 1024 * 1024  # the sealed archive; the database repeats the cap
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20_000
# Chromium's caches are not session state: they are rebuilt, and they would only grow the
# archive. Singleton* are the running browser's lock, socket and cookie links.
_SKIPPED_DIRECTORIES = frozenset(
    {
        "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
        "GrShaderCache", "GraphiteDawnCache", "ShaderCache", "CacheStorage", "ScriptCache",
        "component_crx_cache", "extensions_crx_cache", "Crashpad", "Crash Reports",
    }
)  # fmt: skip
_SKIPPED_PREFIXES = ("Singleton",)
NEW_PROFILE_REASON = "new_profile_signed_out"


class ProfileTooLargeError(Exception):
    """The sealed archive would exceed `MAX_ARCHIVE_BYTES`; nothing was stored."""


class ProfileRecordError(Exception):
    """The environment's row was not there to record the archive on; nothing was written."""


class ProfileUnsafeError(Exception):
    """The archive's row or contents are not this environment's profile; nothing was unpacked."""


def profile_context(environment: BrowserEnvironment) -> bytes:
    """The associated data that binds a sealed archive to one organization, shop, environment."""
    return f"{environment.organization_id}|{environment.shop_id}|{environment.id}".encode("ascii")


def profile_storage_key(environment: BrowserEnvironment) -> str:
    return f"browser-profiles/{environment.organization_id}/{environment.shop_id}/{environment.id}"


# --- Ports --------------------------------------------------------------------------------------


class BlobSealer(Protocol):
    """Envelope encryption of one binary value (`htco_platform.crypto.EnvelopeSealer`). Opening
    with the wrong context, key or bytes raises the sealer's own error."""

    @property
    def current_key_id(self) -> str: ...

    def seal_blob(self, plaintext: bytes, *, context: bytes) -> bytes: ...

    def open_blob(self, blob: bytes, *, context: bytes) -> bytes: ...


@dataclass(frozen=True)
class SealedArchive:
    environment_id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    key_id: str
    blob: bytes = field(repr=False)
    sealed_at: datetime
    storage_key: str = ""


class ProfileArchiveStore(Protocol):
    """`app.browser_profile_archives`, one row per environment (the browser worker's role)."""

    def get(self, environment_id: uuid.UUID) -> SealedArchive | None: ...

    def put(self, archive: SealedArchive) -> None:
        """In one transaction: insert or replace the archive, and record its storage key and key
        ID on the environment's row. Raises `ProfileRecordError` (and writes nothing) unless
        exactly that one row was updated."""
        ...

    def delete(self, environment_id: uuid.UUID) -> None:
        """In one transaction: delete the archive and clear the row's profile columns; raises
        `ProfileRecordError` (and changes nothing) unless exactly that one row was updated."""
        ...


class ProfileRecords(Protocol):
    """The environment rows as the browser worker sees them."""

    def by_id(self, environment_id: uuid.UUID) -> BrowserEnvironment | None: ...


class FirstHealthCheck(Protocol):
    """The first preflight and login check of a new profile (slice 8A.3)."""

    async def check(self, environment: BrowserEnvironment) -> ProvisioningResult: ...


# --- The vault ----------------------------------------------------------------------------------


class ProfileVault:
    def __init__(
        self, sealer: BlobSealer, store: ProfileArchiveStore, clock: Callable[[], datetime]
    ) -> None:
        self._sealer = sealer
        self._store = store
        self._clock = clock

    def seal(self, environment: BrowserEnvironment, archive: bytes) -> SealedArchive:
        """Seal and store the archive as the environment's last good profile."""
        if len(archive) > MAX_ARCHIVE_BYTES:
            raise ProfileTooLargeError("the profile archive is over the cap")
        key_id = self._sealer.current_key_id
        blob = self._sealer.seal_blob(archive, context=profile_context(environment))
        if len(blob) > MAX_ARCHIVE_BYTES:
            raise ProfileTooLargeError("the sealed profile archive is over the cap")
        sealed = SealedArchive(
            environment_id=environment.id, organization_id=environment.organization_id,
            shop_id=environment.shop_id, key_id=key_id, blob=blob, sealed_at=self._clock(),
            storage_key=profile_storage_key(environment),
        )  # fmt: skip
        self._store.put(sealed)
        return sealed

    def open(self, environment: BrowserEnvironment) -> bytes | None:
        """The environment's archive, or None when it has none yet."""
        sealed = self._store.get(environment.id)
        if sealed is None:
            return None
        if (sealed.organization_id, sealed.shop_id) != (
            environment.organization_id,
            environment.shop_id,
        ):
            raise ProfileUnsafeError("the archive's row belongs to another shop")
        return self._sealer.open_blob(sealed.blob, context=profile_context(environment))

    def delete(self, environment: BrowserEnvironment) -> None:
        self._store.delete(environment.id)


# --- The workspace ------------------------------------------------------------------------------


class ProfileWorkspaces:
    """Unpacked profiles under one root: the browser worker's tmpfs (read-only root elsewhere)."""

    def __init__(
        self,
        vault: ProfileVault,
        root: Path,
        *,
        max_unpacked_bytes: int = MAX_UNPACKED_BYTES,
        max_members: int = MAX_MEMBERS,
    ) -> None:
        self._vault = vault
        self._root = root
        self._max_unpacked = max_unpacked_bytes
        self._max_members = max_members

    @property
    def vault(self) -> ProfileVault:
        return self._vault

    def create(self, environment: BrowserEnvironment) -> SealedArchive:
        """Seal an empty profile: Chromium fills it on the first launch."""
        return self._vault.seal(environment, _pack([]))

    @contextmanager
    def checkout(self, environment: BrowserEnvironment) -> Iterator[Path]:
        # mkdtemp creates the directory readable by this user only (0o700).
        directory = Path(tempfile.mkdtemp(prefix="profile-", dir=self._root))
        try:
            archive = self._vault.open(environment)
            if archive is not None:
                self._unpack(archive, directory)
            yield directory
            self._vault.seal(environment, self._packed(directory))
        finally:
            _wipe(directory)

    def _packed(self, directory: Path) -> bytes:
        """The session's profile, refused before sealing when the next checkout would refuse
        it, so an oversized session never replaces the last good archive."""
        files = list(_profile_files(directory))
        if len(files) > self._max_members:
            raise ProfileTooLargeError("the profile has too many entries")
        if sum(path.stat().st_size for path in files if path.is_file()) > self._max_unpacked:
            raise ProfileTooLargeError("the profile is past the unpacked cap")
        return _pack([(path, path.relative_to(directory).as_posix()) for path in files])

    def _unpack(self, archive: bytes, directory: Path) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                members = tar.getmembers()
                self._check(members)
                tar.extractall(directory, members=members, filter="data")
        except (tarfile.TarError, OSError, EOFError) as error:
            raise ProfileUnsafeError("the profile archive does not unpack safely") from error

    def _check(self, members: list[tarfile.TarInfo]) -> None:
        if len(members) > self._max_members:
            raise ProfileUnsafeError("the profile archive has too many entries")
        if sum(member.size for member in members) > self._max_unpacked:
            raise ProfileUnsafeError("the profile archive unpacks past the cap")
        for member in members:
            path = PurePosixPath(member.name)
            if not (member.isfile() or member.isdir()):
                raise ProfileUnsafeError("the profile archive holds a link or a device")
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ProfileUnsafeError("the profile archive names a path outside the profile")


def _pack(files: list[tuple[Path, str]]) -> bytes:
    """Each entry is written from what was opened, never by following a path again: a file
    swapped for a link after the listing is refused (no time-of-check to time-of-use gap), and a
    hard link becomes its own regular member."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, name in files:
            _add(tar, path, name)
    packed = buffer.getvalue()
    if len(packed) > MAX_ARCHIVE_BYTES:
        raise ProfileTooLargeError("the profile archive is over the cap")
    return packed


def _add(tar: tarfile.TarFile, path: Path, name: str) -> None:
    before = path.lstat()
    if stat.S_ISDIR(before.st_mode):
        info = tarfile.TarInfo(name)
        info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o700, int(before.st_mtime)
        tar.addfile(info)
        return
    if not stat.S_ISREG(before.st_mode):
        raise ProfileUnsafeError("a profile entry is not a regular file")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(
            opened.st_mode
        ):
            raise ProfileUnsafeError("a profile entry changed while it was packed")
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = opened.st_size, 0o600, int(opened.st_mtime)
        tar.addfile(info, handle)


def _profile_files(directory: Path) -> Iterator[Path]:
    """Regular files and directories, in order; no caches, links, sockets or lock files."""
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or path.name.startswith(_SKIPPED_PREFIXES):
            continue
        if path.is_dir():
            if path.name in _SKIPPED_DIRECTORIES:
                continue
            yield path
            yield from _profile_files(path)
        elif path.is_file():
            yield path


def _wipe(directory: Path) -> None:
    def _writable_then_retry(function: Callable[[str], object], path: str, _: object) -> None:
        Path(path).chmod(0o700)  # pragma: no cover - only a read-only file blocks removal
        function(path)  # pragma: no cover

    shutil.rmtree(directory, onexc=_writable_then_retry)


# --- The provisioner ----------------------------------------------------------------------------


class SealedProfileProvisioner:
    """The `EnvironmentProvisioner` of slice 8A.1."""

    def __init__(
        self,
        records: ProfileRecords,
        workspaces: ProfileWorkspaces,
        first_check: FirstHealthCheck | None = None,
    ) -> None:
        self._records = records
        self._workspaces = workspaces
        self._first_check = first_check

    async def provision(self, environment_id: uuid.UUID) -> ProvisioningResult:
        environment = self._records.by_id(environment_id)
        if environment is None:
            raise EnvironmentNotFoundError(str(environment_id))
        if environment.status is not EnvironmentStatus.PROVISIONING:
            raise StaleEnvironmentError(EnvironmentStatus.PROVISIONING, environment.status)
        self._workspaces.create(environment)  # the archive and its record, in one write
        if self._first_check is None:
            return ProvisioningResult(EnvironmentStatus.LOGIN_REQUIRED, NEW_PROFILE_REASON)
        return await self._first_check.check(environment)

    async def deprovision(self, environment_id: uuid.UUID) -> None:
        """Destroy the sealed profile and clear its record (the environment is being deleted)."""
        environment = self._records.by_id(environment_id)
        if environment is None:
            return
        self._workspaces.vault.delete(environment)  # the archive and its record, in one write


# --- In memory ----------------------------------------------------------------------------------


@dataclass
class MemoryProfileArchiveStore:
    """The archives and the environments' profile columns; `missing` names environments
    whose row is gone (failure injection)."""

    archives: dict[uuid.UUID, SealedArchive] = field(default_factory=dict)
    profiles: dict[uuid.UUID, tuple[str | None, str | None]] = field(default_factory=dict)
    missing: set[uuid.UUID] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, environment_id: uuid.UUID) -> SealedArchive | None:
        with self._lock:
            return self.archives.get(environment_id)

    def put(self, archive: SealedArchive) -> None:
        with self._lock:
            if archive.environment_id in self.missing:
                raise ProfileRecordError("no environment row to record the archive on")
            self.archives[archive.environment_id] = archive
            self.profiles[archive.environment_id] = (archive.storage_key, archive.key_id)

    def delete(self, environment_id: uuid.UUID) -> None:
        with self._lock:
            if environment_id in self.missing:
                raise ProfileRecordError("no environment row to clear")
            self.archives.pop(environment_id, None)
            self.profiles[environment_id] = (None, None)


@dataclass
class MemoryProfileRecords:
    environments: dict[uuid.UUID, BrowserEnvironment] = field(default_factory=dict)

    def by_id(self, environment_id: uuid.UUID) -> BrowserEnvironment | None:
        return self.environments.get(environment_id)
