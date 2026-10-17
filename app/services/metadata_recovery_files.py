"""Linux physical namespace recovery and pinned confirmed-content verification.

The caller must prove healthy SQL diagnosis, no active/committed references before
removal, and startup RuntimeLease or periodic cleanup claim plus maintenance
artifact lease. Names, inventory and contents never establish that authority.
No WriteAttempt adoption, SQL, accounting, or directory creation is performed here.
Verification results remain provisional until the physical context exits normally.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import os
from typing import Literal

from app.metadata_models import MAX_SNAPSHOT_BYTES, ProvenanceRecord
from app.services.metadata_files import (
    MetadataFiles, _Directories, _NOFOLLOW, _fingerprint, _guard, _hex,
    _optional_stat, _reject, _snapshot, _sync,
)


MAX_INVENTORY_ENTRIES = 128


@dataclass(frozen=True, slots=True)
class ConfirmedExpectation:
    revision_id: str
    parent_revision_id: str | None
    sha256: str
    size_bytes: int
    state: str
    created_at: datetime
    actor_subject: str
    auth_source: str


@dataclass(frozen=True, slots=True)
class ConfirmedCheck:
    revision_id: str
    status: Literal['valid', 'missing', 'content_invalid']


@dataclass(frozen=True, slots=True)
class _InventoryEntry:
    name: str
    dev: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int


@dataclass(frozen=True, slots=True)
class _NamespaceInventory:
    entries: tuple[_InventoryEntry, ...]


def _recognized_name(value):
    if type(value) is not str or len(value) != 37:
        _reject()
    if value.endswith('.json'):
        _hex(value[:-5], 32)
    elif value.startswith('.') and value.endswith('.tmp'):
        _hex(value[1:-4], 32)
    else:
        _reject()
    return value


def _inventory_names(values):
    if type(values) is not tuple or len(values) > MAX_INVENTORY_ENTRIES:
        _reject()
    for value in values:
        _recognized_name(value)
    if len(set(values)) != len(values):
        _reject()
    return values


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
        self._target = None
        self._chain = None
        self._closed = False
        self._inventory_started = False
        self._orphan_attempted = False
        self._inventory = None
        self._inventory_entries = {}
        self._inventory_pins = {}
        self._inventory_fds = []

    @_guard
    def _enter(self, files, job):
        try:
            # This validates paths/identifiers only; it asserts no snapshot identity.
            target = files._target(job, '0' * 32, '0' * 64, 1)
            self._target = target
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
        self._inventory = None
        self._inventory_entries = {}
        self._inventory_pins = {}
        _close_all(self._inventory_fds, self._directories)

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

    def _scan(self):
        """Bounded, content-free observation; never follow an entry or descend."""
        self._check()
        directories = self._directories
        entries = {}
        if directories.missing is None:
            with os.scandir(directories.fd) as iterator:
                for entry in iterator:
                    self._check()
                    # Encountering entry 129 fails before inspecting or pinning it.
                    if len(entries) >= MAX_INVENTORY_ENTRIES:
                        _reject()
                    name = _recognized_name(entry.name)
                    if name in entries:
                        _reject()
                    observed = self._observe(name)
                    if observed is None or observed[1] not in (1, 2):
                        _reject()
                    entries[name] = observed
                    self._check()
        self._check()
        return entries

    def _validate_inventory(self, entries, pins):
        # Reuse the original pins: revalidation must not open another FD set.
        if self._scan() != {name: value for name, value in entries.items()
                            if value is not None}:
            _reject()
        self._validate_plan(tuple(entries), entries, pins)

    @_guard
    def inventory(self):
        """Capture once per session; retain bounded pins until consumption or exit.

        This physical snapshot establishes no SQL authority. A failed capture or
        any subsequent capture request requires a new recovery session.
        """
        if self._inventory_started or self._orphan_attempted:
            _reject()
        self._inventory_started = True
        try:
            entries = self._scan()
            pins = {}
            for name, expected in entries.items():
                self._check()
                fd = os.open(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=self._directories.fd)
                self._inventory_fds.append(fd)
                if _regular(os.fstat(fd)) != expected:
                    _reject()
                pins[name] = [fd, expected]
                self._check()
            self._validate_inventory(entries, pins)
            snapshot = _NamespaceInventory(tuple(
                _InventoryEntry(name, *value[0][0], *value[0][1:], value[1])
                for name, value in sorted(entries.items())
            ))
            self._inventory_entries = entries
            self._inventory_pins = pins
            self._inventory = snapshot
            return snapshot
        except BaseException:
            try:
                _close_all(self._inventory_fds)
            except BaseException:
                pass
            raise

    @_guard
    def verify_confirmed(
        self, inventory, expectations: tuple[ConfirmedExpectation, ...]
    ) -> tuple[ConfirmedCheck, ...]:
        """Check caller-supplied draft expectations using original inventory pins.

        No SQL authority is inferred and no snapshot is consumed. Results are
        provisional until the enclosing context exits normally, including closes.
        Approved snapshots require a future contract.
        """
        if (self._closed or self._orphan_attempted or self._inventory is None
                or inventory is not self._inventory):
            _reject()
        if type(expectations) is not tuple or len(expectations) > 32:
            _reject()
        revisions = set()
        # Validate every argument before reading any file, without coercion.
        for expected in expectations:
            if type(expected) is not ConfirmedExpectation:
                _reject()
            revision = _hex(expected.revision_id, 32)
            if revision in revisions:
                _reject()
            revisions.add(revision)
            if expected.parent_revision_id is not None:
                if _hex(expected.parent_revision_id, 32) == revision:
                    _reject()
            _hex(expected.sha256, 64)
            if (type(expected.size_bytes) is not int
                    or not 1 <= expected.size_bytes <= MAX_SNAPSHOT_BYTES
                    or type(expected.state) is not str or expected.state != 'draft'
                    or type(expected.created_at) is not datetime
                    or expected.created_at.tzinfo is None
                    or expected.created_at.utcoffset() != timedelta(0)
                    or type(expected.actor_subject) is not str
                    or type(expected.auth_source) is not str):
                _reject()
            ProvenanceRecord.model_validate({
                'actor_subject': expected.actor_subject,
                'auth_source': expected.auth_source,
                'recorded_at': expected.created_at,
                'execution_id': None,
            }, strict=True)

        entries = self._inventory_entries
        pins = self._inventory_pins
        self._validate_inventory(entries, pins)
        confirmed_names = {revision + '.json' for revision in revisions}
        groups = {}
        for name, value in entries.items():
            groups.setdefault(value[0][0], []).append(name)
        # Check the entire captured namespace, including unselected files.
        for group in groups.values():
            count = len(group)
            if (count not in (1, 2)
                    or any(entries[name][1] != count for name in group)
                    or (count == 2
                        and (sum(name.startswith('.') for name in group) != 1
                             or any(name in confirmed_names for name in group)))):
                _reject()

        results = []
        for expected in expectations:
            self._validate_inventory(entries, pins)
            name = expected.revision_id + '.json'
            status = 'missing'
            if name in entries:
                target = replace(
                    self._target, revision_id=expected.revision_id,
                    sha256=expected.sha256, size_bytes=expected.size_bytes,
                )
                fd = pins[name][0]
                data = bytearray()
                limit = MAX_SNAPSHOT_BYTES + 1
                while len(data) < limit:
                    chunk = os.pread(fd, min(65536, limit - len(data)), len(data))
                    if not chunk:
                        break
                    data.extend(chunk)
                self._validate_inventory(entries, pins)
                # Only pure content validation can produce content_invalid.
                # Read/stat/ancestry failures never enter this handler.
                try:
                    snapshot = _snapshot(bytes(data), target)
                    if (snapshot.parent_revision_id != expected.parent_revision_id
                            or snapshot.state != expected.state
                            or snapshot.created_at != expected.created_at
                            or snapshot.author.actor_subject != expected.actor_subject
                            or snapshot.author.auth_source != expected.auth_source
                            or snapshot.author.recorded_at != expected.created_at
                            or snapshot.author.execution_id is not None
                            or snapshot.approval is not None):
                        _reject()
                except ValueError:
                    status = 'content_invalid'
                else:
                    status = 'valid'
                    del snapshot
                finally:
                    del data
            self._validate_inventory(entries, pins)
            results.append(ConfirmedCheck(expected.revision_id, status))
        # Also required for an empty expectation tuple.
        self._validate_inventory(entries, pins)
        return tuple(results)

    @_guard
    def remove_orphans(self, inventory, names, *, protected_names) -> None:
        """Attempt one startup cleanup against this exact captured snapshot.

        Before calling, the trusted startup caller must compare the inventory
        against GLOBAL SQL references in a healthy committed diagnostic
        transaction under RuntimeLease, with all writers not started. Confirmed
        files, including protected names absent here, require separate caller
        verification. This module proves neither SQL authority nor quota release.

        Both name collections must be unique tuples of recognized names. Every
        attempt consumes the snapshot, including argument or physical failure;
        retry requires a new session and a new SQL diagnosis.
        """
        if self._orphan_attempted:
            _reject()
        self._orphan_attempted = True
        try:
            self._check()
            if self._inventory is None or inventory is not self._inventory:
                _reject()
            names = _inventory_names(names)
            protected_names = _inventory_names(protected_names)
            entries = self._inventory_entries
            if (any(name not in entries for name in names)
                    or set(names).intersection(protected_names)):
                _reject()
            self._delete(names, entries, self._inventory_pins, complete=True)
        finally:
            self._inventory = None
            self._inventory_entries = {}
            self._inventory_pins = {}
            _close_all(self._inventory_fds)

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
        # Do not mix exact-name cleanup with a startup inventory's retained pins.
        if self._inventory_started:
            _reject()
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

            self._delete(names, entries, pins)
        finally:
            _close_all(fds)

    def _delete(self, names, entries, pins, *, complete=False):
        """Shared pinned deletion engine; complete mode checks the whole namespace."""
        directories = self._directories

        def validate():
            if complete:
                self._validate_inventory(entries, pins)
            else:
                self._validate_plan(names, entries, pins)

        # Validate every selected hardlink group before the first unlink. Startup
        # groups may only be single files or one temp/final publication pair.
        for name in names:
            expected = entries[name]
            if expected is None:
                continue
            inode = expected[0][0]
            group = tuple(key for key in names
                          if entries[key] is not None
                          and entries[key][0][0] == inode)
            if expected[1] != len(group):
                _reject()
            if complete and (len(group) not in (1, 2)
                             or (len(group) == 2
                                 and sum(key.startswith('.') for key in group) != 1)):
                _reject()
        validate()

        for name in names:
            if entries[name] is None:
                continue
            validate()
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
            validate()

        # Required even for empty selections and safely missing directories.
        validate()
        _sync(directories)
        validate()
