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
from dataclasses import dataclass, field, replace
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


class ProfileFenceLostError(Exception):
    """A fenced write found that its session's lease no longer holds the profile lock; nothing
    was written (#169, ADR-0025's fenced `save_session`)."""


class ProfileStaleError(ProfileFenceLostError):
    """The archive changed since this session read it (its generation moved on): the
    compare-and-set refused the write, and nothing was written (ADR-0025 §1)."""


# A write's expected generation: `NO_ARCHIVE` when the session found no archive; `None` for an
# unconditional write (provisioning a new profile, before any session exists).
NO_ARCHIVE = 0


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
    # The stored row's compare-and-set counter (1 on the first write); 0 before it is stored.
    generation: int = 0


class ProfileArchiveStore(Protocol):
    """`app.browser_profile_archives`, one row per environment (the browser worker's role)."""

    def get(self, environment_id: uuid.UUID) -> SealedArchive | None: ...

    def put(
        self, archive: SealedArchive, *, fence: object | None = None, expected: int | None = None
    ) -> None:
        """In one transaction: insert or replace the archive, and record its storage key and key
        ID on the environment's row. Raises `ProfileRecordError` (and writes nothing) unless
        exactly that one row was updated.

        `expected` is the generation the writer read (`NO_ARCHIVE` when there was none). The
        write replaces only that generation and moves it up by one; any other raises
        `ProfileStaleError` and writes nothing. `None` writes unconditionally (provisioning).

        `fence` is the writing session's own profile lease. With a fence, the lease's hold on
        the profile lock is checked **inside the same write** (not before it), so a session
        whose lock was lost can never replace a newer archive: the store raises
        `ProfileFenceLostError` and writes nothing. Only a new profile (provisioning, before
        any session exists) is written without a fence."""
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

    def seal(
        self,
        environment: BrowserEnvironment,
        archive: bytes,
        *,
        fence: object | None = None,
        expected: int | None = None,
    ) -> SealedArchive:
        """Seal and store the archive as the environment's last good profile; with `fence`,
        only while that session's lease still holds the profile lock, and with `expected`, only
        over the generation the session read (`ProfileArchiveStore.put`)."""
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
        self._store.put(sealed, fence=fence, expected=expected)
        return sealed

    def open(self, environment: BrowserEnvironment) -> bytes | None:
        """The environment's archive, or None when it has none yet."""
        return self.read(environment)[0]

    def read(self, environment: BrowserEnvironment) -> tuple[bytes | None, int]:
        """The archive and its generation (`NO_ARCHIVE` when there is none yet)."""
        sealed = self._store.get(environment.id)
        if sealed is None:
            return None, NO_ARCHIVE
        if (sealed.organization_id, sealed.shop_id) != (
            environment.organization_id,
            environment.shop_id,
        ):
            raise ProfileUnsafeError("the archive's row belongs to another shop")
        opened = self._sealer.open_blob(sealed.blob, context=profile_context(environment))
        return opened, sealed.generation

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
        return self._vault.seal(environment, _empty_archive())

    @contextmanager
    def checkout(
        self, environment: BrowserEnvironment, *, fence: object | None = None
    ) -> Iterator[Path]:
        """One session's unpacked profile; the clean exit's re-seal is written under `fence`,
        the session's own lease (#169), and only over the generation it unpacked (ADR-0025 §1),
        so a newer archive written meanwhile is never replaced (`ProfileStaleError`)."""
        # mkdtemp creates the directory readable by this user only (0o700).
        directory = Path(tempfile.mkdtemp(prefix="profile-", dir=self._root))
        try:
            archive, generation = self._vault.read(environment)
            if archive is not None:
                self._unpack(archive, directory)
            yield directory
            self._vault.seal(environment, self._packed(directory), fence=fence, expected=generation)
        finally:
            _wipe(directory)

    def _packed(self, directory: Path) -> bytes:
        """The session's profile, packed in one pass that never follows a link, and refused
        before sealing when the next checkout would refuse it (the member and unpacked caps)."""
        return _Packer(self._max_members, self._max_unpacked).pack(directory)

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


# Linux (the browser worker): every entry is opened relative to its parent's descriptor with
# O_NOFOLLOW, so swapping a file or a directory for a link after the listing cannot lead outside
# the profile. Elsewhere (developer machines) the same walk runs by path, with each opened file's
# identity checked against its listing and its real path checked to be inside the profile.
_FD_WALK = (
    os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
)
_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)
# Non-blocking: a file swapped for a FIFO after its stat must not hang the open; the fstat
# below then refuses it. A regular file ignores the flag.
_READ = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
_OPEN_DIRECTORY = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW


def _empty_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz"):
        pass
    return buffer.getvalue()


def _entries(directory: int | Path) -> list[os.DirEntry[str]]:
    """One directory's entries, in name order (a descriptor on Linux, a path elsewhere)."""
    with os.scandir(directory) as listing:
        return sorted(listing, key=lambda entry: entry.name)


def _skipped(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink() or entry.name.startswith(_SKIPPED_PREFIXES):
        return True
    return entry.is_dir(follow_symlinks=False) and entry.name in _SKIPPED_DIRECTORIES


class _Packer:
    def __init__(self, max_members: int, max_unpacked: int) -> None:
        self._max_members = max_members
        self._max_unpacked = max_unpacked
        self._members = 0
        self._bytes = 0

    def pack(self, directory: Path) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            if _FD_WALK:  # pragma: no branch - one side per platform
                root = os.open(directory, _OPEN_DIRECTORY)
                try:
                    self._walk_fd(tar, root, "")
                finally:
                    os.close(root)
            else:  # pragma: no cover - developer machines without dir_fd
                self._walk_path(tar, directory, directory.resolve(), "")
        packed = buffer.getvalue()
        if len(packed) > MAX_ARCHIVE_BYTES:
            raise ProfileTooLargeError("the profile archive is over the cap")
        return packed

    def _count(self, size: int) -> None:
        self._members += 1
        self._bytes += size
        if self._members > self._max_members:
            raise ProfileTooLargeError("the profile has too many entries")
        if self._bytes > self._max_unpacked:
            raise ProfileTooLargeError("the profile is past the unpacked cap")

    def _walk_fd(self, tar: tarfile.TarFile, parent: int, prefix: str) -> None:
        for entry in _entries(parent):
            if _skipped(entry):
                continue
            name = prefix + entry.name
            listed = entry.stat(follow_symlinks=False)
            _same_kind(entry, listed)
            if stat.S_ISDIR(listed.st_mode):
                child = _open_at(entry.name, _OPEN_DIRECTORY, parent, listed)
                try:
                    self._add_directory(tar, name, listed)
                    self._walk_fd(tar, child, name + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(listed.st_mode):
                self._add_file(tar, name, _open_at(entry.name, _READ, parent, listed))

    def _walk_path(  # pragma: no cover - developer machines without dir_fd
        self, tar: tarfile.TarFile, directory: Path, root: Path, prefix: str
    ) -> None:
        for entry in _entries(directory):
            if _skipped(entry):
                continue
            name = prefix + entry.name
            path = Path(entry.path)
            listed = path.lstat()  # Windows' listing has no inode numbers: the path's own
            was_dir = entry.is_dir(follow_symlinks=False)
            if stat.S_ISDIR(listed.st_mode) != was_dir or not path.resolve().is_relative_to(root):
                raise ProfileUnsafeError("a profile entry changed while it was packed")
            if stat.S_ISDIR(listed.st_mode):
                self._add_directory(tar, name, listed)
                self._walk_path(tar, path, root, name + "/")
            elif stat.S_ISREG(listed.st_mode):
                descriptor = os.open(path, _READ)
                _same(descriptor, listed)
                self._add_file(tar, name, descriptor)

    def _add_directory(self, tar: tarfile.TarFile, name: str, listed: os.stat_result) -> None:
        self._count(0)
        info = tarfile.TarInfo(name)
        info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o700, int(listed.st_mtime)
        tar.addfile(info)

    def _add_file(self, tar: tarfile.TarFile, name: str, descriptor: int) -> None:
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            self._count(opened.st_size)
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = opened.st_size, 0o600, int(opened.st_mtime)
            tar.addfile(info, handle)


def _same_kind(entry: os.DirEntry[str], listed: os.stat_result) -> None:
    """The kind the listing saw (a regular file or a directory) is the kind there now."""
    if (entry.is_dir(follow_symlinks=False), entry.is_file(follow_symlinks=False)) != (
        stat.S_ISDIR(listed.st_mode),
        stat.S_ISREG(listed.st_mode),
    ):
        raise ProfileUnsafeError("a profile entry changed while it was packed")


def _open_at(name: str, flags: int, parent: int, listed: os.stat_result) -> int:
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:  # a link now (ELOOP, ENOTDIR) or gone
        raise ProfileUnsafeError("a profile entry changed while it was packed") from error
    _same(descriptor, listed)
    return descriptor


def _same(descriptor: int, listed: os.stat_result) -> None:
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != (
        listed.st_dev,
        listed.st_ino,
        stat.S_IFMT(listed.st_mode),
    ):
        os.close(descriptor)
        raise ProfileUnsafeError("a profile entry changed while it was packed")


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
    whose row is gone (failure injection). `fenced` answers whether a fence (a lease) still
    holds its profile lock (`MemoryProfileLocks.owns`); it is asked under the store's lock, in
    the same step as the write."""

    archives: dict[uuid.UUID, SealedArchive] = field(default_factory=dict)
    profiles: dict[uuid.UUID, tuple[str | None, str | None]] = field(default_factory=dict)
    missing: set[uuid.UUID] = field(default_factory=set)
    fenced: Callable[[object, uuid.UUID], bool] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, environment_id: uuid.UUID) -> SealedArchive | None:
        with self._lock:
            return self.archives.get(environment_id)

    def put(
        self, archive: SealedArchive, *, fence: object | None = None, expected: int | None = None
    ) -> None:
        with self._lock:
            if (
                fence is not None
                and self.fenced is not None
                and not self.fenced(fence, archive.environment_id)
            ):
                raise ProfileFenceLostError("the session's lease no longer holds the profile")
            if archive.environment_id in self.missing:
                raise ProfileRecordError("no environment row to record the archive on")
            stored = self.archives.get(archive.environment_id)
            current = NO_ARCHIVE if stored is None else stored.generation
            if expected is not None and expected != current:
                raise ProfileStaleError("the archive changed since the session read it")
            self.archives[archive.environment_id] = replace(archive, generation=current + 1)
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
