"""Linux physical recovery under a caller-held recovery authority and artifact lease.

The caller must prove healthy SQL diagnosis, no active/committed references, and
startup RuntimeLease or periodic cleanup claim plus maintenance artifact lease.
Names and file contents never establish that authority. No WriteAttempt adoption,
SQL, accounting, inventory, or directory creation is performed here.
"""
from contextlib import contextmanager
import os

from app.metadata_models import MAX_SNAPSHOT_BYTES
from app.services.metadata_files import (
    MetadataFiles, _Directories, _NOFOLLOW, _fingerprint, _guard, _hex,
    _optional_stat, _reject, _sync,
)


def _close_all(fds, directories=None):
    failed = False
    while fds:
        try:
            os.close(fds.pop())
        except Exception:
            failed = True
    if directories is not None:
        try:
            directories.close()
        except Exception:
            failed = True
    if failed:
        _reject()


def _regular(info):
    fingerprint = _fingerprint(info)
    if (type(info.st_size) is not int
            or not 0 <= info.st_size <= MAX_SNAPSHOT_BYTES
            or type(info.st_nlink) is not int or info.st_nlink < 1):
        _reject()
    return fingerprint, info.st_nlink


class MetadataRecoveryFiles:
    """Physical namespace authority is supplied by the trusted caller, never SQL here."""

    @_guard
    def __init__(self, root):
        self._files = MetadataFiles(root)

    @contextmanager
    def open(self, job):
        session = _RecoveryDirectory()
        session._enter(self._files, job)
        try:
            yield session
        except BaseException:
            # Preserve the exact caller exception, including process-control ones.
            try:
                session._close()
            except BaseException:
                pass
            raise
        else:
            session._finish()


class _RecoveryDirectory:
    def __init__(self):
        self._directories = _Directories()
        self._chain = None
        self._closed = False

    @_guard
    def _enter(self, files, job):
        try:
            # This validates paths/identifiers only; it asserts no snapshot identity.
            target = files._target(job, '0' * 32, '0' * 64, 1)
            self._directories.open(target, allow_missing=True)
            self._chain = tuple(zip(self._directories.names,
                                    self._directories.identities))
            self._check()
        except BaseException:
            try:
                self._close()
            except BaseException:
                pass
            raise

    def _check(self):
        if self._closed:
            _reject()
        directories = self._directories
        if tuple(zip(directories.names, directories.identities)) != self._chain:
            _reject()
        directories.check()

    def _close(self):
        self._closed = True
        _close_all([], self._directories)

    @_guard
    def _finish(self):
        try:
            self._check()
        finally:
            self._close()

    def _observe(self, name):
        self._check()
        info = _optional_stat(self._directories.fd, name)
        result = None if info is None else _regular(info)
        self._check()
        return result

    def _validate_plan(self, names, entries, pins):
        self._check()
        for name in names:
            if self._observe(name) != entries[name]:
                _reject()
        for fd, expected in pins.values():
            info = os.fstat(fd)
            # Unlinked but pinned regular inodes legitimately have zero links.
            if (type(info.st_nlink) is not int
                    or (_fingerprint(info), info.st_nlink) != expected):
                _reject()
        self._check()

    @_guard
    def remove_pending(self, pending_id, revision_id) -> None:
        """Remove only the two SQL-authorized uncommitted names; prove durable absence.

        Partial files and regular-name collisions are covered by that authority.
        A failed/partial cleanup remains charged until a fresh diagnosis and retry
        proves absence. No content is read or trusted.
        """
        # Validate both before any additional filesystem operation in this method.
        names = ('.' + _hex(pending_id, 32) + '.tmp',
                 _hex(revision_id, 32) + '.json')
        self._check()
        directories = self._directories
        if directories.missing is not None:
            _sync(directories)
            self._check()
            return

        fds = []
        try:
            entries = {name: self._observe(name) for name in names}
            pins = {}
            for name in names:
                expected = entries[name]
                if expected is None:
                    continue
                self._check()
                fd = os.open(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directories.fd)
                fds.append(fd)
                if _regular(os.fstat(fd)) != expected:
                    _reject()
                pins[name] = [fd, expected]
                self._check()

            # Validate the entire deletion plan before the first unlink. No foreign
            # hardlink may survive deletion of an authorized inode's names.
            for expected in entries.values():
                if expected is not None:
                    inode = expected[0][0]
                    count = sum(value is not None and value[0][0] == inode
                                for value in entries.values())
                    if expected[1] != count:
                        _reject()
            self._validate_plan(names, entries, pins)

            for name in names:
                if entries[name] is None:
                    continue
                self._validate_plan(names, entries, pins)
                old_fingerprint, old_links = entries[name]
                try:
                    os.unlink(name, dir_fd=directories.fd)
                except Exception:
                    # An effect-then-raise is acceptable only after exact absence.
                    pass
                self._check()
                if _optional_stat(directories.fd, name) is not None:
                    _reject()
                self._check()
                entries[name] = None

                # Our unlink changes ctime and nlink for every pin of this inode.
                # Do not refresh size/mtime/identity or accept an arbitrary delta.
                updated = None
                for key, pin in pins.items():
                    if pin[1][0][0] != old_fingerprint[0]:
                        continue
                    info = os.fstat(pin[0])
                    fingerprint = _fingerprint(info)
                    if (fingerprint[:3] != old_fingerprint[:3]
                            or type(info.st_nlink) is not int
                            or info.st_nlink != old_links - 1):
                        _reject()
                    value = (fingerprint, info.st_nlink)
                    if updated is not None and updated != value:
                        _reject()
                    updated = value
                    pin[1] = value
                    if entries[key] is not None:
                        entries[key] = value
                self._validate_plan(names, entries, pins)

            # Required even when both names were already absent on this attempt.
            self._validate_plan(names, entries, pins)
            _sync(directories)
            self._validate_plan(names, entries, pins)
        finally:
            _close_all(fds)
