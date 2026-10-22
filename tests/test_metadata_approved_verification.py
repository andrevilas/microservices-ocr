"""C5 physical prerequisite on real Linux files under the artifact lease.

Trusted SQL diagnosis, parent-draft/hash verification and recovery authority are
fixture assumptions. Results remain provisional until normal context close.
"""
from dataclasses import replace
from datetime import timedelta
import hashlib
import os

import pytest

from app.metadata_models import ApprovalRecord, MetadataSnapshot, snapshot_bytes
from app.services import metadata_files as files
from app.services.metadata_recovery_files import ConfirmedCheck, ConfirmedExpectation
from test_metadata_files import MARKER, case, final, unavailable
from test_metadata_confirmed_verification import expectation, seed, preserved, io_watch
from test_metadata_recovery_files import recovery, closed
from test_metadata_recovery_inventory import descriptors


pytestmark = pytest.mark.skipif(not files._SUPPORTED, reason='Linux dirfd required')


def approved(c):
    reviewed = 'd' * 32 if c.model.revision_id != 'd' * 32 else 'e' * 32
    approval = ApprovalRecord(
        actor_subject=c.model.author.actor_subject,
        auth_source=c.model.author.auth_source,
        approved_at=c.model.created_at,
        reviewed_revision_id=reviewed, reviewed_sha256='a' * 64,
    )
    model = MetadataSnapshot.model_validate({
        **c.model.model_dump(mode='python', by_alias=True),
        'parent_revision_id': reviewed, 'state': 'approved', 'approval': approval,
    }, strict=True)
    data = snapshot_bytes(model)
    expected = expectation(c, data, state='approved', parent_revision_id=reviewed,
                           reviewed_sha256=approval.reviewed_sha256)
    return model, data, expected


def test_valid_and_missing_approved_reuse_pins_without_mutation(case, monkeypatch):
    model, data, expected = approved(case)
    assert MetadataSnapshot.model_validate_json(data, strict=True) == model
    seed(case, data)
    before = preserved([final(case)])
    missing_id = 'f' * 32 if expected.revision_id != 'f' * 32 else 'e' * 32
    missing = replace(expected, revision_id=missing_id)
    with descriptors(monkeypatch) as tracked, io_watch(monkeypatch) as io:
        with recovery(case).open(case.job) as session:
            inventory = session.inventory()
            pins = tuple(session._inventory_fds)
            opened = len(tracked.opened)
            for _ in range(2):
                result = session.verify_confirmed(inventory, (missing, expected))
                assert result == (ConfirmedCheck(missing_id, 'missing'),
                                  ConfirmedCheck(expected.revision_id, 'valid'))
                assert session._inventory is inventory
                assert tuple(session._inventory_fds) == pins
                assert len(tracked.opened) == opened
            assert io.reads and io.unlinks == io.syncs == []
        assert result[1].status == 'valid' and not tracked.live
    closed(pins)
    assert preserved([final(case)]) == before


def test_missing_approved_creates_nothing(case, monkeypatch):
    _, _, expected = approved(case)
    before = tuple(sorted(case.job.working_dir.rglob('*')))
    with io_watch(monkeypatch) as io:
        with recovery(case).open(case.job) as session:
            result = session.verify_confirmed(session.inventory(), (expected,))
        assert result == (ConfirmedCheck(expected.revision_id, 'missing'),)
        assert io.reads == io.unlinks == io.syncs == []
    assert tuple(sorted(case.job.working_dir.rglob('*'))) == before
    assert not case.directory.parent.exists()


@pytest.mark.parametrize('damage', [
    'parent', 'reviewed_hash', 'actor', 'auth', 'approved_at',
    'author_actor', 'author_auth', 'author_recorded_at', 'execution',
    'created_at', 'draft',
])
def test_approved_semantic_mismatches_with_matching_outer_integrity(case, monkeypatch, damage):
    model, _, expected = approved(case)
    document = model.model_dump(mode='python', by_alias=True)
    later = model.created_at + timedelta(seconds=1)
    if damage == 'parent':
        parent = 'b' * 32 if model.revision_id != 'b' * 32 else 'c' * 32
        document['parent_revision_id'] = parent
        document['approval']['reviewed_revision_id'] = parent
    elif damage in ('reviewed_hash', 'actor', 'auth', 'approved_at'):
        key, value = {
            'reviewed_hash': ('reviewed_sha256', 'b' * 64),
            'actor': ('actor_subject', 'user:2'),
            'auth': ('auth_source', 'oauth'),
            'approved_at': ('approved_at', later),
        }[damage]
        document['approval'][key] = value
    elif damage in ('author_actor', 'author_auth', 'author_recorded_at', 'execution'):
        key, value = {
            'author_actor': ('actor_subject', 'user:2'),
            'author_auth': ('auth_source', 'oauth'),
            'author_recorded_at': ('recorded_at', later),
            'execution': ('execution_id', 'c' * 32),
        }[damage]
        document['author'][key] = value
    elif damage == 'created_at':
        document['created_at'] = later
    else:
        document.update(state='draft', approval=None)
    changed = MetadataSnapshot.model_validate(document, strict=True)
    data = snapshot_bytes(changed)
    assert MetadataSnapshot.model_validate_json(data, strict=True) == changed
    expected = replace(expected, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    seed(case, data)
    before = preserved([final(case)])
    with descriptors(monkeypatch) as tracked, io_watch(monkeypatch) as io:
        with recovery(case).open(case.job) as session:
            result = session.verify_confirmed(session.inventory(), (expected,))
        assert result == (ConfirmedCheck(expected.revision_id, 'content_invalid'),)
        assert io.reads and io.unlinks == io.syncs == [] and not tracked.live
    assert preserved([final(case)]) == before


@pytest.mark.parametrize('invalid', [
    'service', 'legacy', 'missing_hash', 'uppercase_hash', 'short_hash',
    'hash_type', 'missing_parent', 'self_parent', 'zero_user',
])
def test_all_approved_expectations_validate_before_any_pread(case, monkeypatch, invalid):
    _, data, good = approved(case)
    seed(case, data)
    revision = 'f' * 32 if good.revision_id != 'f' * 32 else 'e' * 32
    changes = {
        'service': {'actor_subject': 'service:' + 'a' * 32},
        'legacy': {'auth_source': 'legacy'},
        'missing_hash': {'reviewed_sha256': None},
        'uppercase_hash': {'reviewed_sha256': 'A' * 64},
        'short_hash': {'reviewed_sha256': 'a' * 63},
        'hash_type': {'reviewed_sha256': True},
        'missing_parent': {'parent_revision_id': None},
        'self_parent': {'parent_revision_id': revision},
        'zero_user': {'actor_subject': 'user:0'},
    }[invalid]
    bad = replace(good, revision_id=revision, **changes)
    before = preserved([final(case)])
    with descriptors(monkeypatch) as tracked, io_watch(monkeypatch) as io:
        with recovery(case).open(case.job) as session:
            inventory = session.inventory()
            unavailable(lambda: session.verify_confirmed(inventory, (good, bad)))
        assert io.reads == io.unlinks == io.syncs == [] and not tracked.live
    assert preserved([final(case)]) == before


def test_eight_positional_draft_arguments_and_nonnull_reviewed_hash_rejection(case, monkeypatch):
    c = case
    seed(c)
    expected = ConfirmedExpectation(
        c.model.revision_id, c.model.parent_revision_id, c.digest, len(c.data),
        c.model.state, c.model.created_at,
        c.model.author.actor_subject, c.model.author.auth_source,
    )
    assert expected == expectation(c) and expected.reviewed_sha256 is None
    before = preserved([final(c)])
    with io_watch(monkeypatch) as io:
        with recovery(c).open(c.job) as session:
            inventory = session.inventory()
            assert session.verify_confirmed(inventory, (expected,)) == (
                ConfirmedCheck(expected.revision_id, 'valid'),)
            io.reads.clear()
            invalid = replace(expected, reviewed_sha256='a' * 64)
            unavailable(lambda: session.verify_confirmed(inventory, (invalid,)))
        assert io.reads == io.unlinks == io.syncs == []
    assert preserved([final(c)]) == before


@pytest.mark.parametrize('error_type', [OSError, ValueError, KeyboardInterrupt, SystemExit])
def test_approved_pread_failures_are_fatal_and_preserve_control_exception_identity(case, monkeypatch, error_type):
    _, data, expected = approved(case)
    seed(case, data)
    before = preserved([final(case)])
    error, returned = error_type(MARKER), []
    with descriptors(monkeypatch) as tracked, io_watch(monkeypatch) as io:
        original = os.pread

        def fault(*args):
            original(*args)
            raise error

        def operation():
            with recovery(case).open(case.job) as session:
                inventory = session.inventory()
                with monkeypatch.context() as patch:
                    patch.setattr(files.os, 'pread', fault)
                    returned.append(session.verify_confirmed(inventory, (expected,)))

        if error_type in (KeyboardInterrupt, SystemExit):
            with pytest.raises(error_type) as caught:
                operation()
            assert caught.value is error
        else:
            unavailable(operation)
        assert returned == [] and io.reads and not tracked.live
        assert io.unlinks == io.syncs == []
    assert preserved([final(case)]) == before
