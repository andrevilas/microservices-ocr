"""Owned physical metadata files for Linux; no SQL, recovery, quotas or approval.

The trusted caller must reserve capacity before I/O and hold ONE external artifact
lease through commit or cleanup. Reconcile commit before cleanup; never abort an
attempt whose index commit is confirmed. Cleanup proves absence of this attempt's
owned files, not global absence of a colliding destination. Directory descriptors
and ancestry checks reject observed switches; they do not defeat arbitrary
privileged filesystem rewrites. There is no Windows durability fallback.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from functools import wraps

from app.application import ApplicationError
from app.metadata_models import MAX_SNAPSHOT_BYTES, MetadataSnapshot, snapshot_bytes


_SUPPORTED = (
    sys.platform.startswith("linux")
    and all(getattr(os, name, 0) for name in
            ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"))
    and all(fn in os.supports_dir_fd for fn in
            (os.open, os.stat, os.mkdir, os.link, os.unlink))
    and os.stat in os.supports_follow_symlinks
    and os.link in os.supports_follow_symlinks
)
_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
              | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _unavailable():
    error = ApplicationError(503, "metadata_unavailable", "Stored metadata is unavailable.")
    try:
        raise error from None
    finally:
        error.__cause__ = None
        error.__context__ = None


def _guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            pass
        _unavailable()
    return guarded


def _reject():
    raise ValueError("Unavailable metadata.")


def _hex(value, length):
    if type(value) is not str or re.fullmatch(
        rf"[0-9a-f]{{{length}}}", value, flags=re.ASCII
    ) is None:
        _reject()
    return value


def _absolute(value):
    text = os.fspath(value)
    if type(text) is not str or not text.startswith("/") or "\x00" in text:
        _reject()
    if text == "/":
        return ()
    parts = tuple(text.split("/")[1:])
    if any(part in ("", ".", "..") for part in parts):
        _reject()
    return parts


def _identity(info, *, directory=False):
    if getattr(info, "st_file_attributes", 0) & 0x400:
        _reject()
    predicate = stat.S_ISDIR if directory else stat.S_ISREG
    if not predicate(info.st_mode):
        _reject()
    return info.st_dev, info.st_ino


def _fingerprint(info):
    return (_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _optional_stat(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class _Target:
    parts: tuple[str, ...]
    job_id: str
    parent_job_id: str | None
    revision_id: str
    sha256: str
    size_bytes: int

    @property
    def filename(self):
        return self.revision_id + ".json"


def _snapshot(data, target):
    if (type(data) is not bytes or len(data) != target.size_bytes
            or hashlib.sha256(data).hexdigest() != target.sha256):
        _reject()
    parsed = MetadataSnapshot.model_validate_json(data, strict=True)
    if (snapshot_bytes(parsed) != data
            or parsed.revision_id != target.revision_id
            or parsed.job_id != target.job_id
            or parsed.document_id != target.job_id
            or parsed.parent_job_id != target.parent_job_id):
        _reject()
    return parsed


class _Directories:
    def __init__(self):
        self.fds = []
        self.names = []
        self.identities = []
        self.missing = None

    @property
    def fd(self):
        return self.fds[-1]

    def check(self):
        if not self.fds:
            _reject()
        for index, fd in enumerate(self.fds):
            expected = self.identities[index]
            if _identity(os.fstat(fd), directory=True) != expected:
                _reject()
            info = (os.stat("/", follow_symlinks=False) if index == 0 else
                    os.stat(self.names[index], dir_fd=self.fds[index - 1],
                            follow_symlinks=False))
            if _identity(info, directory=True) != expected:
                _reject()
        if self.missing is not None and _optional_stat(self.fd, self.missing) is not None:
            _reject()

    def _remember(self, fd, name):
        # Record the descriptor before any fallible inspection, so close sees it.
        self.fds.append(fd)
        self.names.append(name)
        self.identities.append(_identity(os.fstat(fd), directory=True))
        self.check()

    def _child(self, name, *, create=False, allow_missing=False):
        self.check()
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=self.fd)
        except FileNotFoundError:
            self.check()
            if _optional_stat(self.fd, name) is not None:
                _reject()
            if not create:
                if not allow_missing:
                    _reject()
                self.missing = name
                self.check()
                return False
            self.check()
            os.mkdir(name, mode=0o700, dir_fd=self.fd)
            self.check()
            os.fsync(self.fd)
            self.check()
            fd = os.open(name, _DIR_FLAGS, dir_fd=self.fd)
        self._remember(fd, name)
        return True

    def open(self, target, *, create=False, allow_missing=False):
        if not _SUPPORTED:
            _reject()
        self._remember(os.open("/", _DIR_FLAGS), None)
        for name in target.parts:
            self._child(name)
        for name in ("results", "metadata"):
            if not self._child(name, create=create, allow_missing=allow_missing):
                return False
        return True

    def close(self):
        failed = False
        while self.fds:
            fd = self.fds.pop()
            try:
                os.close(fd)
            except Exception:
                failed = True
        if failed:
            _reject()


def _entry(directories, name, inode, size=None):
    directories.check()
    info = os.stat(name, dir_fd=directories.fd, follow_symlinks=False)
    if _identity(info) != inode or (size is not None and info.st_size != size):
        _reject()
    return info


def _remove(directories, name, inode):
    directories.check()
    info = _optional_stat(directories.fd, name)
    if info is None:
        return
    # An uncertain creation cannot be adopted, even if its contents match.
    if inode is None or _identity(info) != inode:
        _reject()
    _entry(directories, name, inode)
    try:
        os.unlink(name, dir_fd=directories.fd)
    except Exception:
        pass
    # This also handles unlink taking effect and then raising.
    directories.check()
    if _optional_stat(directories.fd, name) is not None:
        _reject()


def _sync(directories):
    directories.check()
    os.fsync(directories.fd)
    directories.check()


class MetadataFiles:
    """Physical files only. All calls require the caller's external artifact lease."""

    @_guard
    def __init__(self, root):
        self._root = _absolute(root)

    def _target(self, job, revision_id, sha256, size_bytes):
        job_id = _hex(job.job_id, 32)
        parent = job.parent_job_id
        if parent is not None:
            parent = _hex(parent, 32)
        parts = _absolute(job.working_dir)
        if parts not in (self._root + (job_id,),
                         self._root + ("admitted-jobs", job_id)):
            _reject()
        revision_id = _hex(revision_id, 32)
        sha256 = _hex(sha256, 64)
        if type(size_bytes) is not int or not 1 <= size_bytes <= MAX_SNAPSHOT_BYTES:
            _reject()
        return _Target(parts, job_id, parent, revision_id, sha256, size_bytes)

    @_guard
    def prepare(self, job, *, pending_id, revision_id, sha256, size_bytes):
        """Validate primitives and derive names only; perform no filesystem I/O."""
        target = self._target(job, revision_id, sha256, size_bytes)
        return WriteAttempt(target, _hex(pending_id, 32))

    @_guard
    def read(self, job, *, revision_id, sha256, size_bytes):
        target = self._target(job, revision_id, sha256, size_bytes)
        directories = _Directories()
        fd = None
        try:
            directories.open(target)
            before = os.stat(target.filename, dir_fd=directories.fd,
                             follow_symlinks=False)
            fingerprint = _fingerprint(before)
            if before.st_size != target.size_bytes:
                _reject()
            directories.check()
            fd = os.open(target.filename, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directories.fd)
            if _fingerprint(os.fstat(fd)) != fingerprint:
                _reject()
            data = bytearray()
            remaining = target.size_bytes + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                data.extend(chunk)
                remaining -= len(chunk)
            parsed = _snapshot(bytes(data), target)
            directories.check()
            if (_fingerprint(os.fstat(fd)) != fingerprint
                    or _fingerprint(os.stat(target.filename, dir_fd=directories.fd,
                                            follow_symlinks=False)) != fingerprint):
                _reject()
            directories.check()
            return parsed
        finally:
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                directories.close()


class WriteAttempt:
    """One publication attempt, retained by the caller before publish is invoked.

    Cleanup explicitly aborts owned files and must never follow a confirmed index
    commit. Unknown creation/link outcomes remain charged until absence is proven.
    """

    def __init__(self, target, pending_id):
        self._target = target
        self._temp = "." + pending_id + ".tmp"
        self._started = False
        self._closed = False
        self._temp_fd = None
        self._temp_pending = False
        self._temp_inode = None
        self._final_pending = False
        self._final_inode = None
        self._ancestor_chain = None
        self._needs_sync = False

    @_guard
    def publish(self, canonical_bytes: bytes) -> None:
        if self._started or self._closed:
            _reject()
        self._started = True
        target = self._target
        _snapshot(canonical_bytes, target)
        directories = _Directories()
        try:
            directories.open(target, create=True)
            directories.check()
            if _optional_stat(directories.fd, self._temp) is not None:
                _reject()
            directories.check()
            self._ancestor_chain = tuple(zip(directories.names, directories.identities))
            # Set the phase BEFORE open: it may create and then throw without an FD.
            self._temp_pending = True
            self._needs_sync = True
            self._temp_fd = os.open(
                self._temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600, dir_fd=directories.fd,
            )
            self._temp_inode = _identity(os.fstat(self._temp_fd))
            view = memoryview(canonical_bytes)
            offset = 0
            while offset < len(view):
                _entry(directories, self._temp, self._temp_inode)
                count = os.write(self._temp_fd, view[offset:])
                if type(count) is not int or not 0 < count <= len(view) - offset:
                    _reject()
                offset += count
            info = os.fstat(self._temp_fd)
            if _identity(info) != self._temp_inode or info.st_size != target.size_bytes:
                _reject()
            _entry(directories, self._temp, self._temp_inode, target.size_bytes)
            os.fsync(self._temp_fd)
            directories.check()
            if _optional_stat(directories.fd, target.filename) is not None:
                _reject()
            _entry(directories, self._temp, self._temp_inode, target.size_bytes)
            # A throwing link leaves an uncertain destination; never adopt it.
            self._final_pending = True
            self._needs_sync = True
            os.link(self._temp, target.filename, src_dir_fd=directories.fd,
                    dst_dir_fd=directories.fd, follow_symlinks=False)
            self._final_inode = self._temp_inode
            _entry(directories, target.filename, self._final_inode, target.size_bytes)
            _remove(directories, self._temp, self._temp_inode)
            self._temp_pending = False
            _sync(directories)
            _entry(directories, target.filename, self._final_inode, target.size_bytes)
            self._needs_sync = False
        finally:
            try:
                fd = self._temp_fd
                self._temp_fd = None
                if fd is not None:
                    os.close(fd)
            finally:
                directories.close()

    @_guard
    def cleanup(self) -> None:
        self._closed = True
        if not (self._temp_pending or self._final_pending or self._needs_sync):
            return
        if self._ancestor_chain is None:
            _reject()
        directories = _Directories()
        try:
            directories.open(self._target)
            if tuple(zip(directories.names, directories.identities)) != self._ancestor_chain:
                _reject()
            failed = False
            for flag, name, inode in (
                ("_temp_pending", self._temp, self._temp_inode),
                ("_final_pending", self._target.filename, self._final_inode),
            ):
                if not getattr(self, flag):
                    continue
                self._needs_sync = True
                try:
                    _remove(directories, name, inode)
                    setattr(self, flag, False)
                except Exception:
                    failed = True
            # Sync even when one removal failed; retain every unresolved flag.
            _sync(directories)
            self._needs_sync = False
            if failed:
                _reject()
        finally:
            directories.close()
