"""Real SQLite schema dependency tests; approval workflows remain unavailable."""
import os
import sqlite3

import pytest

from app.application import ApplicationError
from app.services.metadata_store import MetadataStore, _DDL
from test_metadata_store import (
    args, commit, error, h, principal, receipt_for, reserve, restart, save, sql,
    state, totals,
)


TARGETS = ("metadata_operations", "metadata_pending", "metadata_audit")
TEMPS = ("_metadata_schema_operations_new", "_metadata_schema_pending_new",
         "_metadata_schema_audit_new")


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def legacy_ddl():
    ddl = tuple(statement.replace("CHECK(operation IN ('save-v1','approve-v1'))",
                                  "CHECK(operation='save-v1')")
                .replace("CHECK(action IN ('save','approve'))", "CHECK(action='save')")
                for statement in _DDL)
    assert sum("CHECK(operation='save-v1')" in statement for statement in ddl) == 2
    assert sum("CHECK(action='save')" in statement for statement in ddl) == 1
    assert all("approve-v1" not in statement and "'approve'" not in statement for statement in ddl)
    return ddl


def table_ddl(ddl, table):
    return next(statement for statement in ddl
                if statement.startswith("CREATE TABLE IF NOT EXISTS " + table + " ("))


def index_ddl(ddl, table):
    return tuple(statement for statement in ddl
                 if statement.startswith("CREATE INDEX ") and " ON " + table + "(" in statement)


def convert_fixture_to_legacy(harness, *, change=None):
    """Test-only downgrade; runtime code never emits legacy DDL or does this."""
    ddl = legacy_ddl()
    with harness.svc._transaction() as conn:
        for table in TARGETS:
            temporary = "_fixture_legacy_" + table
            statement = table_ddl(ddl, table)
            statement = statement.replace("CREATE TABLE IF NOT EXISTS " + table,
                                          "CREATE TABLE " + temporary, 1)
            if change is not None:
                statement = change(table, statement)
            conn.execute(statement)
            columns = ",".join(quote(row[1]) for row in conn.execute(
                "PRAGMA table_info(" + quote(table) + ")"))
            conn.execute("INSERT INTO " + quote(temporary) + " (" + columns + ") SELECT " +
                         columns + " FROM " + quote(table))
            conn.execute("DROP TABLE " + quote(table))
            conn.execute("ALTER TABLE " + quote(temporary) + " RENAME TO " + quote(table))
            for statement in index_ddl(ddl, table):
                conn.execute(statement)


def database_state(harness):
    with harness.jobs._lock:
        conn = harness.jobs._get_conn()
        schema = tuple(tuple(row) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
        tables = {row[1]: tuple(tuple(record) for record in conn.execute(
            "SELECT * FROM " + quote(row[1]) + " ORDER BY rowid"))
                  for row in schema if row[0] == "table" and not row[1].startswith("sqlite_")}
        return schema, tables


def index_state(harness):
    with harness.jobs._lock:
        conn = harness.jobs._get_conn()
        return {table: tuple(sorted(
            (row[1], row[2], row[3], row[4], tuple(tuple(item) for item in conn.execute(
                "PRAGMA index_xinfo(" + quote(row[1]) + ")")))
            for row in conn.execute("PRAGMA index_list(" + quote(table) + ")")))
                for table in TARGETS}


def seed(harness, actor, pending_state="active"):
    first = args()
    first_receipt = save(harness, actor, first)
    save(harness, actor, args(harness.svc.get_head(actor, harness.job.job_id)))
    pending = reserve(harness, actor, args(harness.svc.get_head(actor, harness.job.job_id))).pending
    if pending_state == "cleanup":
        with harness.svc._transaction() as conn:
            harness.svc._mark_cleanup_locked(conn, pending.pending_id)
    return first, first_receipt, pending


def test_fresh_database_uses_widened_checks_and_no_approval_rows(h):
    with h.jobs._lock:
        conn = h.jobs._get_conn()
        for table in TARGETS:
            actual = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            expected = ("CHECK(action IN ('save','approve'))" if table == "metadata_audit" else
                        "CHECK(operation IN ('save-v1','approve-v1'))")
            assert expected in actual
            assert conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    assert not any(hasattr(h.svc, name) for name in ("approve", "approve_metadata", "reserve_approval"))


@pytest.mark.parametrize("pending_state", ["active", "cleanup"])
def test_upgrade_preserves_actual_history_receipts_pending_and_indexes(h, principal, pending_state):
    first, receipt, pending = seed(h, principal, pending_state)
    convert_fixture_to_legacy(h)
    before, charged, indexes = state(h), totals(h), index_state(h)
    unrelated = tuple(row for row in database_state(h)[0] if row[2] not in TARGETS)
    restart(h)
    assert state(h) == before
    assert totals(h) == charged
    assert index_state(h) == indexes
    assert tuple(row for row in database_state(h)[0] if row[2] not in TARGETS) == unrelated
    admission = h.svc.preflight(principal, h.job.job_id,
                                key_hash=first["key_hash"], request_hash=first["request_hash"])
    assert admission.receipt == receipt
    assert admission.head.revision_id == pending.expected_head
    with h.svc._transaction() as conn:
        actual = h.svc._pending(conn, pending.pending_id)
        assert actual.state == pending_state
        assert actual.charged_bytes == pending.charged_bytes
        assert actual.charged_slots == pending.charged_slots
        assert actual.approval_bytes == pending.approval_bytes
        assert actual.approval_slots == pending.approval_slots
    with error(409, "metadata_write_pending"):
        reserve(h, principal, args(admission.head))


def test_reopen_current_schema_does_not_rebuild_or_copy(h, principal, monkeypatch):
    seed(h, principal)
    convert_fixture_to_legacy(h)
    h.svc = MetadataStore(h.jobs)
    before = database_state(h)
    original = h.jobs._get_conn
    statements = []

    class Observe:
        def __getattr__(self, name):
            return getattr(original(), name)

        def execute(self, statement, parameters=()):
            statements.append(statement)
            return original().execute(statement, parameters)

    monkeypatch.setattr(h.jobs, "_get_conn", lambda: proxy)
    proxy = Observe()
    MetadataStore(h.jobs)
    assert not any(statement.startswith(("DROP TABLE", "ALTER TABLE", "INSERT INTO"))
                   for statement in statements)
    assert not any(name in statement for name in TEMPS for statement in statements
                   if statement.startswith("CREATE TABLE"))
    assert database_state(h) == before


@pytest.mark.parametrize("phase", ["copy", "drop", "rename"])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_owned_transaction_rolls_back_all_three_tables_on_mid_migration_fault(
        h, principal, monkeypatch, phase, failure):
    seed(h, principal)
    convert_fixture_to_legacy(h)
    before = database_state(h)
    connection = h.jobs._get_conn()
    fired = []
    prefixes = {
        "copy": 'INSERT INTO "_metadata_schema_pending_new"',
        "drop": 'DROP TABLE "metadata_pending"',
        "rename": 'ALTER TABLE "_metadata_schema_audit_new"',
    }

    class Fault:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, statement, parameters=()):
            result = connection.execute(statement, parameters)
            if statement.startswith(prefixes[phase]) and not fired:
                fired.append(statement)
                raise failure("SENSITIVE_MARKER")
            return result

    with monkeypatch.context() as patch:
        patch.setattr(h.jobs, "_get_conn", lambda: proxy)
        proxy = Fault()
        if failure is RuntimeError:
            with error(503, "metadata_unavailable"):
                MetadataStore(h.jobs)
        else:
            with pytest.raises(failure):
                MetadataStore(h.jobs)
    assert len(fired) == 1
    assert connection.in_transaction is False
    assert database_state(h) == before
    assert not any(row[1] in TEMPS for row in database_state(h)[0])
    h.svc = MetadataStore(h.jobs)
    assert state(h) == {name: list(rows) for name, rows in before[1].items()
                        if name in state(h)}


@pytest.mark.parametrize("damage", [
    "column", "trigger", "index", "constraint", "type", "foreign_key", "literal_case",
])
def test_unknown_target_schema_fails_before_any_table_rebuild(h, principal, monkeypatch, damage):
    seed(h, principal)

    def change(table, statement):
        if table != "metadata_audit":
            return statement
        if damage == "constraint":
            return statement.replace("CHECK(action='save')", "CHECK(action IN ('save','other'))")
        if damage == "type":
            return statement.replace("created_at TEXT NOT NULL", "created_at BLOB NOT NULL")
        if damage == "literal_case":
            return statement.replace("CHECK(action='save')", "CHECK(action IN ('save','SAVE'))")
        return statement

    convert_fixture_to_legacy(h, change=change)
    if damage == "column":
        sql(h, "ALTER TABLE metadata_audit ADD COLUMN unexpected TEXT")
    elif damage == "foreign_key":
        sql(h, "ALTER TABLE metadata_audit ADD COLUMN unexpected TEXT REFERENCES jobs(job_id)")
    elif damage == "trigger":
        sql(h, "CREATE TRIGGER custom_audit AFTER INSERT ON metadata_audit BEGIN SELECT 1; END")
    elif damage == "index":
        sql(h, "CREATE INDEX custom_audit ON metadata_audit(actor)")
    before = database_state(h)
    connection = h.jobs._get_conn()
    mutations = []

    class Observe:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, statement, parameters=()):
            if statement.startswith(("CREATE TABLE", "INSERT INTO", "DROP TABLE", "ALTER TABLE")):
                mutations.append(statement)
            return connection.execute(statement, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(h.jobs, "_get_conn", lambda: proxy)
        proxy = Observe()
        with error(503, "metadata_unavailable"):
            MetadataStore(h.jobs)
    assert mutations == []
    assert database_state(h) == before


@pytest.mark.parametrize("temporary", [False, True])
def test_fixed_replacement_name_collision_is_preserved(h, principal, temporary):
    seed(h, principal)
    convert_fixture_to_legacy(h)
    connection = h.jobs._get_conn()
    connection.execute("CREATE " + ("TEMP " if temporary else "") +
                       "TABLE " + TEMPS[-1] + " (sentinel BLOB)")
    connection.execute("INSERT INTO " + TEMPS[-1] + " VALUES (?)", (b"SENSITIVE_MARKER",))
    connection.commit()
    before = database_state(h)
    with error(503, "metadata_unavailable"):
        MetadataStore(h.jobs)
    assert database_state(h) == before
    assert connection.execute("SELECT sentinel FROM " + TEMPS[-1]).fetchone()[0] == b"SENSITIVE_MARKER"


def test_unrelated_objects_are_untouched_and_known_index_name_cannot_be_hijacked(h, principal):
    seed(h, principal)
    convert_fixture_to_legacy(h)
    sql(h, "CREATE TABLE unrelated (value TEXT)")
    sql(h, "INSERT INTO unrelated VALUES ('keep')")
    sql(h, "CREATE INDEX unrelated_index ON unrelated(value)")
    sql(h, "CREATE TRIGGER unrelated_trigger AFTER INSERT ON unrelated BEGIN SELECT 1; END")
    before = database_state(h)
    h.svc = MetadataStore(h.jobs)
    after = database_state(h)
    assert after[1] == before[1]
    assert tuple(row for row in after[0] if row[2] == "unrelated") == tuple(
        row for row in before[0] if row[2] == "unrelated")
    sql(h, "DROP INDEX metadata_audit_job")
    sql(h, "CREATE INDEX metadata_audit_job ON unrelated(value)")
    before = database_state(h)
    with error(503, "metadata_unavailable"):
        MetadataStore(h.jobs)
    assert database_state(h) == before


@pytest.mark.parametrize("legacy", [False, True])
def test_existing_invalid_enum_data_is_rejected_atomically(h, principal, legacy):
    seed(h, principal)
    if legacy:
        convert_fixture_to_legacy(h)
    connection = h.jobs._get_conn()
    # Fixture-only corruption. The production helper never changes PRAGMAs.
    connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        connection.execute("UPDATE metadata_audit SET action='unknown'")
        connection.commit()
    finally:
        connection.execute("PRAGMA ignore_check_constraints=OFF")
    before = database_state(h)
    with error(503, "metadata_unavailable"):
        MetadataStore(h.jobs)
    assert database_state(h) == before


def test_migration_normalizes_unquoted_case_and_whitespace_only(h, principal):
    seed(h, principal)

    def change(table, statement):
        return statement.replace("CREATE TABLE IF NOT EXISTS", "create  table  if not exists").replace(
            " TEXT ", " text ").replace("CHECK(", "check ( ")

    convert_fixture_to_legacy(h, change=change)
    before = state(h)
    h.svc = MetadataStore(h.jobs)
    assert state(h) == before


@pytest.mark.parametrize("table,column,new_value,invalid", [
    ("metadata_operations", "operation", "approve-v1", "future-v1"),
    ("metadata_pending", "operation", "approve-v1", "future-v1"),
    ("metadata_audit", "action", "approve", "future"),
])
def test_widened_enums_and_unique_constraints_are_enforced(h, principal, table, column, new_value, invalid):
    seed(h, principal)
    convert_fixture_to_legacy(h)
    h.svc = MetadataStore(h.jobs)
    before = database_state(h)
    # Test-only approval enum writes are rolled back; no runtime approval path.
    with h.jobs._lock:
        connection = h.jobs._get_conn()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("UPDATE " + table + " SET " + column + "=?", (new_value,))
            assert connection.execute("SELECT DISTINCT " + column + " FROM " + table).fetchall() == [(new_value,)]
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE " + table + " SET " + column + "=?", (invalid,))
            columns = ",".join(quote(row[1]) for row in connection.execute(
                "PRAGMA table_info(" + quote(table) + ")"))
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO " + table + " (" + columns + ") SELECT " +
                                   columns + " FROM " + table + " LIMIT 1")
        finally:
            connection.rollback()
    assert database_state(h) == before


def test_save_commit_and_replay_still_work_after_migration(h, principal):
    first, receipt, pending = seed(h, principal)
    convert_fixture_to_legacy(h)
    restart(h)
    assert commit(h, principal, pending, receipt_for(pending)).revision_id == pending.revision_id
    latest = save(h, principal, args(h.svc.get_head(principal, h.job.job_id)))
    replay = h.svc.preflight(principal, h.job.job_id,
                             key_hash=first["key_hash"], request_hash=first["request_hash"])
    assert replay.receipt == receipt
    assert replay.head.revision_id == latest.revision_id


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "fstatvfs"),
                    reason="Physical metadata coordination requires POSIX")
def test_physical_storage_write_and_replay_after_pending_migration(case):
    from types import SimpleNamespace
    from app.services.metadata_storage import MetadataStorage
    from test_metadata_storage import make_write, principal as storage_principal, write

    case.store = MetadataStore(case.jobs)
    case.storage = MetadataStorage(case.store, case.service)
    case.principal = storage_principal()
    attempt = make_write(case)
    harness = SimpleNamespace(svc=case.store, jobs=case.jobs)
    convert_fixture_to_legacy(harness)
    case.store = MetadataStore(case.jobs)
    case.storage = MetadataStorage(case.store, case.service)
    assert write(case, attempt) == attempt.receipt
    assert (case.directory / (attempt.model.revision_id + ".json")).read_bytes() == attempt.data
    replay = case.store.preflight(case.principal, case.job.job_id,
                                  key_hash=attempt.key, request_hash=attempt.request)
    assert replay.receipt == attempt.receipt
    assert case.store._active_writes == set()


# Register the existing real JobStore/artifact-lease fixture for the POSIX test.
from test_metadata_files import case
