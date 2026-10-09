"""File locking for concurrent access protection."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterator

# Platform-specific imports for file locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from claude_swap.exceptions import ClaudeSwitchError, LockError

_logger = logging.getLogger("claude-swap")


# THE BUILD CHECK ON SHARED WRITES (X3697). Several processes write the
# usage store and the engine state file: `cswap auto`, `cswap list`, the menu
# bar, and the owner proxy, which imports this package into its own process
# and keeps a draining process alive across an install. A process keeps
# running the code it loaded, so after an install the draining proxy wrote
# the store with the previous build's code beside the new one and emptied it
# (live 2026-10-08 18:29Z). Every writer of a file more than one process
# shares therefore compares the build it loaded with the build installed on
# disk now, and refuses the write on a mismatch
# (`check_loaded_build_is_installed`): the store files here, every rename
# `fsutil.replace_with_retry` publishes (credentials, `sequence.json`,
# settings, migrations, mappings, imports), the Keychain items and deletes
# in `credentials`, and the multi-file operations in `switcher`, which check
# once before their first write. Reads stay allowed: only a write can
# destroy what the other build keeps.
#
# The build's identity is a sha256 over this package's own ``*.py`` files
# (relative path and bytes), not the distribution's version or the commit in
# ``direct_url.json``: the version stays ``0.27.0b1`` across every commit,
# a directory or editable install records no commit, and the files are
# exactly the code a process runs. The commit, when the installed
# distribution records one for this same directory, only labels the build
# in messages.


@dataclass(frozen=True)
class Build:
    """One claude-swap build: ``digest`` identifies it, ``label`` names it
    for a reader (version, short digest, and the VCS commit when known)."""

    digest: str
    label: str


class StaleBuildWriteError(ClaudeSwitchError):
    """A process refused to write a shared file because the claude-swap
    build it loaded is no longer the one installed on disk.

    ``installed`` is None when the installed build could not be read at all
    (a package file or the distribution metadata vanished or was unreadable
    mid-install); ``unreadable``
    then names that error. A build nobody can read is not the loaded build,
    so the write is refused the same way."""

    def __init__(
        self,
        path: "Path | str",
        loaded: Build,
        installed: Build | None,
        unreadable: Exception | None = None,
    ) -> None:
        self.path = path
        self.loaded = loaded
        self.installed = installed
        self.unreadable = unreadable
        if installed is not None:
            what = f"build {installed.label} is installed"
        else:
            what = (
                f"the installed build cannot be read ({unreadable}), as while "
                "an install replaces the package files, so it is treated as "
                "not the loaded build"
            )
        super().__init__(
            f"refusing to write {path}: this process loaded build "
            f"{loaded.label}, {what}"
        )


_PACKAGE_DIR = Path(__file__).resolve().parent


def _source_files(package_dir: Path) -> list[Path]:
    """Every ``*.py`` file of the package, sorted, ``__pycache__`` left out."""
    found: list[Path] = []
    for root, dirs, files in os.walk(package_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        found.extend(Path(root) / name for name in files if name.endswith(".py"))
    return sorted(found)


def _source_key(package_dir: Path, files: list[Path]) -> tuple:
    """What a reinstall or an edit changes on every touched file: its path,
    inode, mtime and size. Equal keys mean the digest is still valid."""
    key = []
    for path in files:
        st = path.stat()
        key.append(
            (str(path.relative_to(package_dir)), st.st_ino, st.st_mtime_ns, st.st_size)
        )
    return tuple(key)


def _source_digest(package_dir: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(package_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _installed_commit(package_dir: Path) -> str | None:
    """The VCS commit pip recorded for the installed distribution, when that
    distribution's package directory is ``package_dir`` and it was installed
    from a VCS URL; None otherwise (a directory or editable install, or a
    distribution installed somewhere other than the code this process runs)."""
    dist = importlib.metadata.distribution("claude-swap")
    if Path(str(dist.locate_file("claude_swap"))).resolve() != package_dir:
        return None
    text = dist.read_text("direct_url.json")
    if text is None:
        return None
    vcs_info = json.loads(text).get("vcs_info")
    if not isinstance(vcs_info, dict):
        return None
    commit = vcs_info.get("commit_id")
    return commit if isinstance(commit, str) else None


def _build_label(package_dir: Path, digest: str) -> str:
    version = importlib.metadata.version("claude-swap")
    commit = _installed_commit(package_dir)
    where = f", commit {commit[:10]}" if commit else ""
    return f"{version} (source sha256 {digest[:12]}{where})"


# (source key, digest) of the package files on disk, under `_build_lock`:
# a write re-hashes only when a file's path, inode, mtime or size moved.
_installed_cache: tuple[tuple, str] | None = None
_build_lock = threading.Lock()


def installed_build_digest() -> str:
    """The digest of the build installed on disk now, re-hashed only when
    the package files' stat key changed since the last call."""
    global _installed_cache
    files = _source_files(_PACKAGE_DIR)
    key = _source_key(_PACKAGE_DIR, files)
    with _build_lock:
        if _installed_cache is not None and _installed_cache[0] == key:
            return _installed_cache[1]
    digest = _source_digest(_PACKAGE_DIR, files)
    with _build_lock:
        _installed_cache = (key, digest)
    return digest


def _loaded_build() -> Build:
    digest = installed_build_digest()
    return Build(digest=digest, label=_build_label(_PACKAGE_DIR, digest))


# The build this process loaded: the package files as they were when this
# module was imported, which is when the package itself was imported.
LOADED_BUILD = _loaded_build()

# Files this process has already named in its one refusal WARNING.
_refusal_warned: set[str] = set()


def check_loaded_build_is_installed(path: "Path | str") -> None:
    """Refuse a write to the shared target ``path`` (a file, or a named
    Keychain item) from a process whose loaded build is no longer the
    installed one.

    Raises :class:`StaleBuildWriteError` on a mismatch, after logging ONE
    WARNING per target per process naming both builds; the target is not
    touched. A package file that vanishes or cannot be read mid-check (an
    install in progress) is the same refusal with ``installed`` None: at
    that instant no build can be shown to be the loaded one.
    """
    loaded = LOADED_BUILD
    try:
        installed_digest = installed_build_digest()
        if installed_digest == loaded.digest:
            return
        installed: Build | None = Build(
            digest=installed_digest,
            label=_build_label(_PACKAGE_DIR, installed_digest),
        )
        unreadable: Exception | None = None
    except (OSError, importlib.metadata.PackageNotFoundError) as e:
        # The package files (OSError) or the distribution's metadata
        # (PackageNotFoundError) vanished under an install in progress.
        installed, unreadable = None, e
    exc = StaleBuildWriteError(path, loaded, installed, unreadable)
    with _build_lock:
        first = str(path) not in _refusal_warned
        _refusal_warned.add(str(path))
    if first:
        _logger.warning("%s", exc)
    raise exc

#: How long `add`, `add-token`, `remove`, `import` and `export` wait for the
#: account store lock (the switcher's ``lock_file``). Every other holder
#: (switch, swap, move, the usage-refresh persists, the consume gate) holds it
#: for local I/O or one bounded profile fetch, so a wait this long times out
#: only on a stuck holder, and a user's `add` never fails because a switch was
#: running.
STORE_LOCK_WAIT_S = 60.0


def lock_holders(lock_path: Path) -> list[str]:
    """Name the processes holding a ``flock`` on ``lock_path``.

    Each entry reads ``pid N (command name)``. The answer comes from
    ``/proc/locks``, so it exists on Linux only; elsewhere, or when the lock
    file or ``/proc/locks`` cannot be read, the list is empty and the caller
    says the holder is unidentified. Waiters (the ``->`` rows) are not
    holders and are left out.
    """
    if not sys.platform.startswith("linux"):
        return []
    try:
        st = lock_path.stat()
        rows = Path("/proc/locks").read_text().splitlines()
    except OSError:
        return []
    inode = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    holders = []
    for row in rows:
        fields = row.split()
        if len(fields) < 6 or fields[1] != "FLOCK" or fields[5] != inode:
            continue
        pid = fields[4]
        try:
            name = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            name = "exited"
        holders.append(f"pid {pid} ({name})")
    return holders


class FileLock:
    """Cross-process file lock using platform-specific APIs."""

    def __init__(self, lock_path: Path, timeout: float = 10.0):
        self.lock_path = lock_path
        self.timeout = timeout
        self._lock_file: IO | None = None
        self._locked = False

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire exclusive lock with timeout.

        Args:
            timeout: Maximum seconds to wait for lock. Defaults to the
                timeout given at construction.

        Returns:
            True if lock acquired, False if timeout.
        """
        if timeout is None:
            timeout = self.timeout
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.lock_path, "w")

        start = time.monotonic()
        deadline = start + timeout
        while True:
            try:
                if sys.platform == "win32":
                    # Windows: use msvcrt for file locking
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    # POSIX: use fcntl for file locking
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() - start > timeout:
                    self._lock_file.close()
                    self._lock_file = None
                    return False
                # Clamped to the remaining budget, so `timeout` bounds the
                # call: the deadline check above cannot fire while a full
                # sleep is still running past it.
                time.sleep(max(0.0, min(0.1, deadline - time.monotonic())))

    def release(self) -> None:
        """Release the lock."""
        if self._lock_file and self._locked:
            if sys.platform == "win32":
                # Windows: unlock using msvcrt
                try:
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass  # File may already be unlocked
            else:
                # POSIX: unlock using fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
            self._locked = False

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise LockError("Failed to acquire lock - another instance may be running")
        return self

    def __exit__(self, *args) -> None:
        self.release()

    @contextlib.contextmanager
    def held_for(self, action: str) -> Iterator[FileLock]:
        """Hold the lock for ``action``, waiting up to ``self.timeout``.

        A timeout raises ``LockError`` before ``action`` has changed anything,
        and the message says so, naming the lock file and the process holding
        it, so whoever ran ``action`` can tell a slow holder from a stuck one.
        """
        if not self.acquire():
            holders = lock_holders(self.lock_path)
            held_by = ", ".join(holders) if holders else "an unidentified process"
            raise LockError(
                f"{action}: waited {self.timeout:g}s for the account store lock "
                f"{self.lock_path}, held by {held_by}; nothing was changed. "
                f"Retry once it is released."
            )
        try:
            yield self
        finally:
            self.release()
