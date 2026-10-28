"""Sealed browser profiles (slice 8A.1, ADR-0020 §2).

A shop's Chromium profile lives only as a sealed archive: envelope encryption under the browser
profile key ring, with `org|shop|environment` as associated data, so an archive copied to another
shop's row does not open (AC-8A.1.1). It is unpacked into a private directory for one session
and wiped afterwards, whatever happens (AC-8A.1.3).
"""

import contextlib
import io
import os
import shutil
import tarfile
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

import htco_application.browser_profiles as browser_profiles_module
from htco_application.browser_environments import (
    EnvironmentNotFoundError,
    ProvisioningResult,
    StaleEnvironmentError,
)
from htco_application.browser_profiles import (
    MAX_ARCHIVE_BYTES,
    NO_ARCHIVE,
    MemoryProfileArchiveStore,
    MemoryProfileRecords,
    ProfileFenceLostError,
    ProfileRecordError,
    ProfileStaleError,
    ProfileTooLargeError,
    ProfileUnsafeError,
    ProfileVault,
    ProfileWorkspaces,
    SealedArchive,
    SealedProfileProvisioner,
    profile_context,
    profile_storage_key,
)
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_platform.crypto import EnvelopeSealer, KeyRing, SealingError

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("0a000000-0000-0000-0000-000000000001")
COOKIE = b"session-cookie-value-that-must-never-be-plain"


def sealer() -> EnvelopeSealer:
    return EnvelopeSealer(KeyRing(("browser-1", os.urandom(32))))


def environment(
    shop: uuid.UUID | None = None, status: EnvironmentStatus = EnvironmentStatus.PROVISIONING
) -> BrowserEnvironment:
    return BrowserEnvironment(
        id=uuid.uuid4(), organization_id=ORG, shop_id=shop or uuid.uuid4(), status=status
    )


def vault(store: MemoryProfileArchiveStore | None = None) -> ProfileVault:
    return ProfileVault(sealer(), store or MemoryProfileArchiveStore(), lambda: NOW)


def tar_of(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info, data in members:
            archive.addfile(info, None if data is None else io.BytesIO(data))
    return buffer.getvalue()


def file_member(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


# --- The vault: AC-8A.1.1 -----------------------------------------------------------------------


def test_identity__names_the_organization_shop_and_environment() -> None:
    env = environment()
    expected = f"{env.organization_id}|{env.shop_id}|{env.id}"
    assert profile_context(env) == expected.encode("ascii")
    assert profile_storage_key(env) == (
        f"browser-profiles/{env.organization_id}/{env.shop_id}/{env.id}"
    )


@pytest.mark.ac("AC-8A.1.1")
def test_vault__stores_ciphertext_only_and_opens_it_again() -> None:
    store = MemoryProfileArchiveStore()
    profiles = vault(store)
    env = environment()
    archive = tar_of(file_member("Default/Cookies", COOKIE))

    sealed = profiles.seal(env, archive)

    raw = store.archives[env.id].blob  # what storage holds, read straight
    assert COOKIE not in raw
    assert archive not in raw
    assert sealed.key_id == "browser-1"
    assert sealed.sealed_at == NOW
    assert "blob" not in repr(sealed)
    assert profiles.open(env) == archive


@pytest.mark.ac("AC-8A.1.1")
def test_vault__an_archive_copied_to_another_shops_row_does_not_open() -> None:
    store = MemoryProfileArchiveStore()
    profiles = vault(store)
    shop_a, shop_b = environment(), environment()
    profiles.seal(shop_a, tar_of(file_member("Default/Cookies", COOKIE)))
    stolen = store.archives[shop_a.id]

    store.put(
        SealedArchive(
            environment_id=shop_b.id, organization_id=shop_b.organization_id,
            shop_id=shop_b.shop_id, key_id=stolen.key_id, blob=stolen.blob, sealed_at=NOW,
        )
    )  # fmt: skip

    with pytest.raises(SealingError):
        profiles.open(shop_b)


def test_vault__refuses_a_row_of_another_identity_before_opening() -> None:
    store = MemoryProfileArchiveStore()
    profiles = vault(store)
    env = environment()
    profiles.seal(env, b"archive")
    moved = store.archives[env.id]
    store.archives[env.id] = SealedArchive(
        environment_id=env.id, organization_id=env.organization_id, shop_id=uuid.uuid4(),
        key_id=moved.key_id, blob=moved.blob, sealed_at=NOW,
    )  # fmt: skip
    with pytest.raises(ProfileUnsafeError):
        profiles.open(env)


def test_vault__refuses_an_archive_over_the_cap_and_keeps_the_last_good_one() -> None:
    store = MemoryProfileArchiveStore()
    profiles = vault(store)
    env = environment()
    profiles.seal(env, b"last good archive")
    with pytest.raises(ProfileTooLargeError):
        profiles.seal(env, os.urandom(MAX_ARCHIVE_BYTES))
    assert profiles.open(env) == b"last good archive"


def test_vault__a_missing_archive_opens_as_none_and_delete_removes_it() -> None:
    profiles = vault()
    env = environment()
    assert profiles.open(env) is None
    profiles.seal(env, b"archive")
    profiles.delete(env)
    assert profiles.open(env) is None


# --- The workspace: AC-8A.1.3 -------------------------------------------------------------------


def everything_under(root: Path) -> list[Path]:
    return sorted(root.rglob("*"))


@pytest.mark.ac("AC-8A.1.3")
def test_workspace__a_session_leaves_nothing_on_disk_and_keeps_its_changes(
    tmp_path: Path,
) -> None:
    store = MemoryProfileArchiveStore()
    profiles = vault(store)
    workspaces = ProfileWorkspaces(profiles, tmp_path)
    env = environment()
    workspaces.create(env)

    with workspaces.checkout(env) as directory:
        assert directory.parent == tmp_path
        (directory / "Default").mkdir()
        (directory / "Default" / "Cookies").write_bytes(COOKIE)

    assert everything_under(tmp_path) == []
    assert COOKIE not in store.archives[env.id].blob
    with workspaces.checkout(env) as directory:
        assert (directory / "Default" / "Cookies").read_bytes() == COOKIE
    assert everything_under(tmp_path) == []


@pytest.mark.ac("AC-8A.1.3")
def test_workspace__a_failed_session_is_wiped_and_the_last_good_archive_stays(
    tmp_path: Path,
) -> None:
    profiles = vault()
    workspaces = ProfileWorkspaces(profiles, tmp_path)
    env = environment()
    workspaces.create(env)
    good = profiles.open(env)

    def crash(path: Path) -> None:
        (path / "half-written").write_bytes(COOKIE)
        raise RuntimeError("engine crashed")

    with pytest.raises(RuntimeError, match="engine crashed"), workspaces.checkout(env) as path:
        crash(path)

    assert everything_under(tmp_path) == []
    assert profiles.open(env) == good


def test_workspace__a_session_without_an_archive_starts_empty(tmp_path: Path) -> None:
    workspaces = ProfileWorkspaces(vault(), tmp_path)
    with workspaces.checkout(environment()) as directory:
        assert list(directory.iterdir()) == []
    assert everything_under(tmp_path) == []


def test_workspace__caches_links_and_sockets_are_not_sealed(tmp_path: Path) -> None:
    profiles = vault()
    root = tmp_path / "tmpfs"
    root.mkdir()
    workspaces = ProfileWorkspaces(profiles, root)
    env = environment()
    with workspaces.checkout(env) as directory:
        (directory / "Default" / "Cache").mkdir(parents=True)
        (directory / "Default" / "Cache" / "data_0").write_bytes(b"cached page")
        (directory / "Default" / "Preferences").write_bytes(b"{}")
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"not the profile")
        with contextlib.suppress(OSError):  # Windows without the symlink privilege
            (directory / "SingletonLock").symlink_to(outside)
    with tarfile.open(fileobj=io.BytesIO(profiles.open(env) or b""), mode="r:*") as archive:
        names = archive.getnames()
    assert "Default/Preferences" in names
    assert not any("Cache" in name or "Singleton" in name for name in names)


@pytest.mark.parametrize(
    "member",
    [
        file_member("../escape", b"x"),
        file_member("/etc/absolute", b"x"),
        (tarfile.TarInfo("Default/link"), None),
    ],
    ids=["parent-path", "absolute-path", "symlink"],
)
def test_workspace__refuses_an_archive_with_unsafe_entries(
    tmp_path: Path, member: tuple[tarfile.TarInfo, bytes | None]
) -> None:
    info, _ = member
    if info.name == "Default/link":
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
    profiles = vault()
    env = environment()
    profiles.seal(env, tar_of(member))
    root = tmp_path / "tmpfs"
    root.mkdir()
    with pytest.raises(ProfileUnsafeError), ProfileWorkspaces(profiles, root).checkout(env):
        pass  # pragma: no cover - never entered
    assert everything_under(root) == []
    assert not (tmp_path / "escape").exists()


def test_workspace__refuses_an_archive_that_unpacks_past_the_cap(tmp_path: Path) -> None:
    profiles = vault()
    env = environment()
    info = tarfile.TarInfo("Default/huge")
    info.size = 0
    bomb = tar_of((info, b""))
    profiles.seal(env, bomb)
    workspaces = ProfileWorkspaces(profiles, tmp_path, max_unpacked_bytes=0, max_members=0)
    with pytest.raises(ProfileUnsafeError), workspaces.checkout(env):
        pass  # pragma: no cover - never entered
    assert everything_under(tmp_path) == []


def test_workspace__a_profile_too_large_to_seal_is_wiped_and_refused(tmp_path: Path) -> None:
    profiles = vault()
    env = environment()
    workspaces = ProfileWorkspaces(profiles, tmp_path)
    with pytest.raises(ProfileTooLargeError), workspaces.checkout(env) as directory:
        (directory / "big").write_bytes(os.urandom(MAX_ARCHIVE_BYTES + 1024))
    assert everything_under(tmp_path) == []
    assert profiles.open(env) is None


# --- The provisioner ----------------------------------------------------------------------------


class Checked:
    def __init__(self, result: ProvisioningResult) -> None:
        self.result = result
        self.seen: list[uuid.UUID] = []

    async def check(self, environment: BrowserEnvironment) -> ProvisioningResult:
        self.seen.append(environment.id)
        return self.result


def provisioner(
    tmp_path: Path, env: BrowserEnvironment, check: Checked | None = None
) -> tuple[SealedProfileProvisioner, MemoryProfileArchiveStore, ProfileVault]:
    records = MemoryProfileRecords({env.id: env})
    store = MemoryProfileArchiveStore()
    profiles = vault(store)
    return (
        SealedProfileProvisioner(records, ProfileWorkspaces(profiles, tmp_path), check),
        store,
        profiles,
    )


async def test_provision__seals_an_empty_profile_records_it_and_ends_signed_out(
    tmp_path: Path,
) -> None:
    env = environment()
    provision, store, profiles = provisioner(tmp_path, env)

    result = await provision.provision(env.id)

    assert result == ProvisioningResult(EnvironmentStatus.LOGIN_REQUIRED, "new_profile_signed_out")
    assert store.profiles[env.id] == (profile_storage_key(env), "browser-1")
    with tarfile.open(fileobj=io.BytesIO(profiles.open(env) or b""), mode="r:*") as archive:
        assert archive.getnames() == []
    assert everything_under(tmp_path) == []


async def test_provision__runs_the_first_health_check_when_one_is_given(tmp_path: Path) -> None:
    env = environment()
    check = Checked(ProvisioningResult(EnvironmentStatus.FAILED, "chromium_did_not_start"))
    provision, _, _ = provisioner(tmp_path, env, check)
    assert (await provision.provision(env.id)).status is EnvironmentStatus.FAILED
    assert check.seen == [env.id]


async def test_provision__refuses_an_unknown_or_moved_environment(tmp_path: Path) -> None:
    env = environment(status=EnvironmentStatus.READY)
    provision, store, _ = provisioner(tmp_path, env)
    with pytest.raises(EnvironmentNotFoundError):
        await provision.provision(uuid.uuid4())
    with pytest.raises(StaleEnvironmentError):
        await provision.provision(env.id)
    assert store.profiles == {}


async def test_deprovision__destroys_the_archive_and_clears_the_record(tmp_path: Path) -> None:
    env = environment()
    provision, store, profiles = provisioner(tmp_path, env)
    await provision.provision(env.id)

    await provision.deprovision(env.id)
    await provision.deprovision(uuid.uuid4())  # an unknown environment: nothing to destroy

    assert profiles.open(env) is None
    assert store.profiles[env.id] == (None, None)


def test_vault__refuses_a_plain_archive_over_the_cap() -> None:
    with pytest.raises(ProfileTooLargeError):
        vault().seal(environment(), b"\x00" * (MAX_ARCHIVE_BYTES + 1))


@pytest.mark.parametrize(
    ("archive", "caps"),
    [
        (b"not a gzip tar", {}),
        (tar_of(file_member("Default/Preferences", b"{}")), {"max_unpacked_bytes": 1}),
    ],
    ids=["corrupt", "unpacked-cap"],
)
def test_workspace__refuses_a_corrupt_or_oversized_archive(
    tmp_path: Path, archive: bytes, caps: dict[str, int]
) -> None:
    profiles = vault()
    env = environment()
    profiles.seal(env, archive)
    workspaces = ProfileWorkspaces(profiles, tmp_path, **caps)
    with pytest.raises(ProfileUnsafeError), workspaces.checkout(env):
        pass  # pragma: no cover - never entered
    assert everything_under(tmp_path) == []


@pytest.mark.parametrize(
    "caps", [{"max_unpacked_bytes": 4}, {"max_members": 1}], ids=["unpacked-cap", "member-cap"]
)
def test_workspace__a_session_past_the_unpack_caps_keeps_the_last_good_archive(
    tmp_path: Path, caps: dict[str, int]
) -> None:
    """Codex on #116: a profile the next checkout would refuse is never sealed over the good one."""
    profiles = vault()
    env = environment()
    workspaces = ProfileWorkspaces(profiles, tmp_path, **caps)
    workspaces.create(env)
    good = profiles.open(env)

    def grow(directory: Path) -> None:
        (directory / "a").write_bytes(b"12345")
        (directory / "b").write_bytes(b"")

    with pytest.raises(ProfileTooLargeError), workspaces.checkout(env) as directory:
        grow(directory)

    assert everything_under(tmp_path) == []
    assert profiles.open(env) == good
    with workspaces.checkout(env) as directory:
        assert list(directory.iterdir()) == []


def test_workspace__hard_linked_files_are_sealed_as_regular_files(tmp_path: Path) -> None:
    """Codex on #116: a hard link must not become a link member the next checkout refuses."""
    profiles = vault()
    env = environment()
    root = tmp_path / "tmpfs"
    root.mkdir()
    workspaces = ProfileWorkspaces(profiles, root)

    def link(directory: Path) -> None:
        (directory / "Cookies").write_bytes(COOKIE)
        os.link(directory / "Cookies", directory / "Cookies-backup")

    with workspaces.checkout(env) as directory:
        link(directory)

    with workspaces.checkout(env) as directory:
        assert (directory / "Cookies").read_bytes() == COOKIE
        assert (directory / "Cookies-backup").read_bytes() == COOKIE
    assert everything_under(root) == []


def _swap_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, swap: str, write: Callable[[Path], object]
) -> None:
    """Between listing a directory and reading the entry `swap`, that entry is replaced by a
    symlink to a directory outside the profile. Nothing outside may be read: the session is
    refused and the last good archive stays."""
    import htco_application.browser_profiles as module  # noqa: PLC0415

    profiles = vault()
    env = environment()
    root = tmp_path / "tmpfs"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "Default").mkdir(parents=True)
    for secret in (outside / "Cookies", outside / "Default" / "Cookies", outside / "Preferences"):
        secret.write_bytes(b"never in an archive")
    workspaces = ProfileWorkspaces(profiles, root)
    workspaces.create(env)
    good = profiles.open(env)
    listed = module._entries
    swapped = False

    def swap_after_listing(directory: int | Path) -> list[os.DirEntry[str]]:
        nonlocal swapped
        entries = listed(directory)
        victim = next((e for e in entries if e.name == swap), None)
        if victim is not None and not swapped:
            swapped = True
            base = (
                Path(f"/proc/self/fd/{directory}").readlink()
                if isinstance(directory, int)
                else Path(directory)
            )
            path = base / victim.name
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            target = outside / "Default" if swap == "Default" else outside / swap
            try:
                path.symlink_to(target, target_is_directory=target.is_dir())
            except OSError:  # pragma: no cover - Windows without the symlink privilege
                pytest.skip("symlinks need a privilege on this machine")
        return entries

    monkeypatch.setattr(module, "_entries", swap_after_listing)
    with pytest.raises(ProfileUnsafeError), workspaces.checkout(env) as directory:
        write(directory)
    assert swapped
    assert profiles.open(env) == good
    assert everything_under(root) == []


def test_workspace__a_file_swapped_for_a_symlink_while_packing_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """quill on #116: a file replaced by a symlink after the listing is refused."""
    _swap_test(
        tmp_path, monkeypatch, swap="Cookies",
        write=lambda d: (d / "Cookies").write_bytes(COOKIE),
    )  # fmt: skip


def test_workspace__a_directory_swapped_for_a_symlink_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex on #125: a listed subdirectory replaced by a symlink before its children are read
    must not let files from outside the profile into the archive."""

    def write(directory: Path) -> None:
        (directory / "Default").mkdir()
        (directory / "Default" / "Cookies").write_bytes(COOKIE)

    _swap_test(tmp_path, monkeypatch, swap="Default", write=write)


def test_vault__the_record_follows_every_seal_including_a_key_rotation() -> None:
    """CODEX (local) on #116: the environment's key ID is written with the archive itself."""
    store = MemoryProfileArchiveStore()
    env = environment()
    old, new = os.urandom(32), os.urandom(32)
    ProfileVault(EnvelopeSealer(KeyRing(("browser-1", old))), store, lambda: NOW).seal(env, b"a")
    rotated = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-2", new), [("browser-1", old)])), store, lambda: NOW
    )
    assert rotated.open(env) == b"a"
    rotated.seal(env, b"b")
    assert store.profiles[env.id] == (profile_storage_key(env), "browser-2")
    assert store.archives[env.id].key_id == "browser-2"


def test_vault__an_environment_row_that_is_gone_stores_nothing() -> None:
    """CODEX (local) on #116: the archive and its record are one write; with no environment
    row to record on, neither is stored."""
    env = environment()
    store = MemoryProfileArchiveStore(missing={env.id})
    profiles = vault(store)
    with pytest.raises(ProfileRecordError):
        profiles.seal(env, b"archive")
    with pytest.raises(ProfileRecordError):
        profiles.delete(env)
    assert store.archives == {}
    assert store.profiles == {}


_LINUX_WALK = pytest.mark.skipif(
    not browser_profiles_module._FD_WALK,
    reason="the descriptor walk needs dir_fd and O_NOFOLLOW (Linux, the browser worker)",
)


@_LINUX_WALK
def test_open_at__an_entry_replaced_or_removed_after_its_stat_is_refused(tmp_path: Path) -> None:
    """The last two guards of the descriptor walk: the opened entry must be the one listed."""
    open_at = browser_profiles_module._open_at
    (tmp_path / "Cookies").write_bytes(COOKIE)
    (tmp_path / "other").write_bytes(b"other")
    parent = os.open(tmp_path, os.O_RDONLY)
    try:
        listed = os.stat("Cookies", dir_fd=parent, follow_symlinks=False)
        (tmp_path / "other").replace(tmp_path / "Cookies")  # a different inode now
        with pytest.raises(ProfileUnsafeError):
            open_at("Cookies", os.O_RDONLY, parent, listed)
        (tmp_path / "Cookies").unlink()
        with pytest.raises(ProfileUnsafeError):
            open_at("Cookies", os.O_RDONLY, parent, listed)
    finally:
        os.close(parent)


@_LINUX_WALK
def test_workspace__a_socket_or_pipe_in_the_profile_is_not_sealed(tmp_path: Path) -> None:
    profiles = vault()
    env = environment()
    workspaces = ProfileWorkspaces(profiles, tmp_path)
    with workspaces.checkout(env) as directory:
        (directory / "Preferences").write_bytes(b"{}")
        getattr(os, "mkfifo")(directory / "pipe")  # noqa: B009 - POSIX only, the test is skipped elsewhere
    with tarfile.open(fileobj=io.BytesIO(profiles.open(env) or b""), mode="r:*") as archive:
        assert archive.getnames() == ["Preferences"]


@_LINUX_WALK
def test_open_at__a_file_swapped_for_a_fifo_is_refused_without_hanging(tmp_path: Path) -> None:
    """Codex on #125: a regular file replaced by a FIFO after its stat must not block the
    open (nobody writes to it); it is opened non-blocking and refused by its fstat."""
    open_at = browser_profiles_module._open_at
    read = browser_profiles_module._READ
    (tmp_path / "Cookies").write_bytes(COOKIE)
    parent = os.open(tmp_path, os.O_RDONLY)
    try:
        listed = os.stat("Cookies", dir_fd=parent, follow_symlinks=False)
        (tmp_path / "Cookies").unlink()
        getattr(os, "mkfifo")(tmp_path / "Cookies")  # noqa: B009 - POSIX only

        failure: list[BaseException] = []

        def attempt() -> None:
            try:
                open_at("Cookies", read, parent, listed)
            except BaseException as error:
                failure.append(error)

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(timeout=2)
        if worker.is_alive():  # unblock the hung open so the test run can finish
            with open(tmp_path / "Cookies", "wb"):  # noqa: PTH123
                pass
            pytest.fail("opening the swapped FIFO blocked")
        assert len(failure) == 1
        assert isinstance(failure[0], ProfileUnsafeError)
    finally:
        os.close(parent)


# --- The generation compare-and-set (8A.2a, ADR-0025 §1) ---------------------------------------


@pytest.mark.ac("AC-8A.2.8")
def test_generation__each_write_moves_it_up_by_one() -> None:
    store = MemoryProfileArchiveStore()
    profiles, env = vault(store), environment()
    assert profiles.read(env) == (None, 0)
    profiles.seal(env, b"one")
    assert profiles.read(env) == (b"one", 1)
    profiles.seal(env, b"two", expected=1)
    assert profiles.read(env) == (b"two", 2)


@pytest.mark.ac("AC-8A.2.8")
def test_generation__a_write_over_a_generation_it_did_not_read_is_stale() -> None:
    store = MemoryProfileArchiveStore()
    profiles, env = vault(store), environment()
    profiles.seal(env, b"one")
    profiles.seal(env, b"newer", expected=1)  # another session wrote meanwhile
    with pytest.raises(ProfileStaleError):
        profiles.seal(env, b"stale", expected=1)
    with pytest.raises(ProfileStaleError):
        profiles.seal(env, b"stale", expected=NO_ARCHIVE)  # it found none, but one exists now
    assert profiles.read(env) == (b"newer", 2)
    assert issubclass(ProfileStaleError, ProfileFenceLostError)  # the controller's lock_lost


@pytest.mark.ac("AC-8A.2.8")
def test_checkout__never_replaces_an_archive_written_after_it_unpacked(tmp_path: Path) -> None:
    """Even with the lease fence passing, a session's re-seal replaces only the generation it
    unpacked: one written meanwhile (by a session that took the lock after this one lost it,
    on another worker) is kept, and this session's write is refused."""
    store = MemoryProfileArchiveStore()
    profiles, env = vault(store), environment()
    workspaces = ProfileWorkspaces(profiles, tmp_path)
    workspaces.create(env)
    newer = tar_of()
    checkout = workspaces.checkout(env)
    directory = checkout.__enter__()
    (directory / "Cookies").write_bytes(b"stale")
    profiles.seal(env, newer, expected=1)  # the newer session's write
    with pytest.raises(ProfileStaleError):
        checkout.__exit__(None, None, None)
    assert profiles.read(env) == (newer, 2)
    assert not any(tmp_path.iterdir())
