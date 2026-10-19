"""Transactional widening of three metadata tables; no approval workflow.

The caller owns the shared lock, transaction, rollback and exception sanitation.
Only exact known definitions are accepted. No database-wide schema version or
connection lifecycle is managed here.
"""
from __future__ import annotations

import re


_TABLES = ("metadata_operations", "metadata_pending", "metadata_audit")
_TEMP_NAMES = {
    "metadata_operations": "_metadata_schema_operations_new",
    "metadata_pending": "_metadata_schema_pending_new",
    "metadata_audit": "_metadata_schema_audit_new",
}
_ENUMS = {
    "metadata_operations": (
        "CHECK(operation IN ('save-v1','approve-v1'))", "CHECK(operation='save-v1')"),
    "metadata_pending": (
        "CHECK(operation IN ('save-v1','approve-v1'))", "CHECK(operation='save-v1')"),
    "metadata_audit": (
        "CHECK(action IN ('save','approve'))", "CHECK(action='save')"),
}
_TOKEN = re.compile(
    r"\s+|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|"
    r"[A-Za-z_][A-Za-z_0-9]*|[0-9]+|<>|!=|<=|>=|[(),=><+*/.;-]"
)
_HEADER = re.compile(
    r"\ACREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z_0-9]*)",
    re.IGNORECASE,
)


def _need(condition):
    if not condition:
        raise ValueError("Unsupported metadata schema.")


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _tokens(sql):
    """Ignore whitespace/case outside quotes, preserving quoted text exactly."""
    _need(type(sql) is str)
    result = []
    end = 0
    for match in _TOKEN.finditer(sql):
        _need(match.start() == end)
        end = match.end()
        token = match.group()
        if not token.isspace():
            result.append(token if token[0] in "'\"`[" else token.lower())
    _need(end == len(sql))
    if result and result[-1] == ";":
        result.pop()
    return tuple(result)


def _definition(sql, kind, name):
    tokens = list(_tokens(sql))
    _need(tokens[:2] == ["create", kind])
    if tokens[2:5] == ["if", "not", "exists"]:
        del tokens[2:5]
    _need(len(tokens) > 3)
    # SQLite ALTER TABLE RENAME quotes the destination table name. Accept only
    # this exact identifier spelling, not case-folded or rewritten literals.
    _need(tokens[2] in (name, _quote(name)))
    tokens[2] = name
    return tuple(tokens)


def _parts(tokens):
    _need(tokens[3] == "(" and tokens[-1] == ")")
    result, part, depth = [], [], 0
    for token in tokens[4:-1]:
        if token == "," and depth == 0:
            result.append(tuple(part))
            part = []
            continue
        part.append(token)
        depth += (token == "(") - (token == ")")
        _need(depth >= 0)
    _need(depth == 0 and part)
    result.append(tuple(part))
    return result


def _column_names(tokens):
    _need(tokens and all(token == "," for token in tokens[1::2]))
    names = tokens[::2]
    _need(all(re.fullmatch(r"[a-z_][a-z_0-9]*", name) for name in names))
    return tuple(names)


def _auto_indexes(sql, table):
    """Derive SQLite's implicit indexes from the supplied trusted definition."""
    result = []
    for part in _parts(_definition(sql, "table", table)):
        if part[0] == "unique":
            _need(part[1] == "(" and part[-1] == ")")
            result.append(("u", _column_names(part[2:-1])))
        elif "primary" in part:
            position = part.index("primary")
            _need(part[position:position + 2] == ("primary", "key"))
            # All three affected tables use non-INTEGER primary keys, if any.
            _need(part[1] != "integer")
            result.append(("pk", (part[0],)))
        elif "unique" in part:
            result.append(("u", (part[0],)))
    return tuple(result)


def _signature(conn, table):
    rows = conn.execute("PRAGMA main.table_xinfo(" + _quote(table) + ")").fetchall()
    _need(rows and all(len(row) == 7 and row[6] == 0 for row in rows))
    signature = tuple((row[0], row[1].lower(), row[2].upper(), *row[3:]) for row in rows)
    _need(len({row[1] for row in signature}) == len(signature))
    _need(not conn.execute("PRAGMA main.foreign_key_list(" + _quote(table) + ")").fetchall())
    return signature


def _checks(sql):
    tokens = _tokens(sql)
    expressions = []
    for position, token in enumerate(tokens):
        if token != "check":
            continue
        _need(tokens[position + 1] == "(")
        expression, depth = [], 1
        for item in tokens[position + 2:]:
            depth += (item == "(") - (item == ")")
            if depth == 0:
                break
            expression.append(item)
        _need(depth == 0 and expression)
        expressions.append(" ".join(expression))
    return expressions


def _validate_rows(conn, table, sql, signature):
    # CHECKs can have been bypassed on an existing connection. Validate them
    # before any rebuild, even for already widened tables.
    bad = [_quote(row[1]) + " IS NULL" for row in signature if row[3]]
    bad.extend("NOT COALESCE((" + expression + "),0)" for expression in _checks(sql))
    if bad:
        _need(conn.execute("SELECT 1 FROM " + _quote(table) + " WHERE " +
                           " OR ".join(bad) + " LIMIT 1").fetchone() is None)
    for _, columns in _auto_indexes(sql, table):
        names = ",".join(_quote(column) for column in columns)
        nonnull = " AND ".join(_quote(column) + " IS NOT NULL" for column in columns)
        _need(conn.execute("SELECT 1 FROM " + _quote(table) + " WHERE " + nonnull +
                           " GROUP BY " + names + " HAVING COUNT(*)>1 LIMIT 1").fetchone() is None)


def _validate_indexes(conn, table, sql, signature, declared):
    expected = {}
    for number, (origin, columns) in enumerate(_auto_indexes(sql, table), 1):
        name = "sqlite_autoindex_" + table + "_" + str(number)
        expected[name] = (None, 1, origin, columns)
    for name, statement, columns in declared:
        expected[name] = (_definition(statement, "index", name), 0, "c", columns)
    objects = conn.execute(
        "SELECT type,name,sql FROM main.sqlite_master "
        "WHERE tbl_name=? COLLATE NOCASE AND type IN ('index','trigger')", (table,)).fetchall()
    _need(len(objects) == len(expected))
    seen = set()
    for kind, name, statement in objects:
        key = name.lower()
        _need(kind == "index" and key in expected and key not in seen)
        seen.add(key)
        definition = expected[key][0]
        _need(statement is None if definition is None else
              _definition(statement, "index", key) == definition)
    listed = conn.execute("PRAGMA main.index_list(" + _quote(table) + ")").fetchall()
    _need(len(listed) == len(expected))
    cids = {row[1]: row[0] for row in signature}
    seen = set()
    for row in listed:
        _need(len(row) == 5)
        _, name, unique, origin, partial = row
        key = name.lower()
        _need(key in expected and key not in seen)
        seen.add(key)
        _, wanted_unique, wanted_origin, columns = expected[key]
        _need((unique, origin, partial) == (wanted_unique, wanted_origin, 0))
        actual = conn.execute("PRAGMA main.index_xinfo(" + _quote(name) + ")").fetchall()
        actual = tuple((r[0], r[1], r[2].lower() if type(r[2]) is str else r[2],
                        *r[3:]) for r in actual)
        wanted = tuple((i, cids[column], column, 0, "BINARY", 1)
                       for i, column in enumerate(columns))
        wanted += ((len(columns), -1, None, 0, "BINARY", 0),)
        _need(actual == wanted)


def _expected(ddl):
    tables, indexes = {}, {table: [] for table in _TABLES}
    for statement in ddl:
        tokens = _tokens(statement)
        if tokens[:2] == ("create", "table"):
            position = 5 if tokens[2:5] == ("if", "not", "exists") else 2
            name = tokens[position]
            if name in _TABLES:
                _need(name not in tables)
                tables[name] = statement
        elif tokens[:2] == ("create", "index"):
            position = 5 if tokens[2:5] == ("if", "not", "exists") else 2
            name = tokens[position]
            definition = _definition(statement, "index", name)
            _need(definition[3] == "on")
            table = definition[4]
            if table in indexes:
                _need(definition[5] == "(" and definition[-1] == ")")
                indexes[table].append((name, statement, _column_names(definition[6:-1])))
    _need(set(tables) == set(_TABLES))
    for table, (new, _) in _ENUMS.items():
        _need(tables[table].count(new) == 1)
    return tables, indexes


def _object(conn, name, *, temporary=False):
    catalog = "sqlite_temp_master" if temporary else "main.sqlite_master"
    return conn.execute("SELECT type,tbl_name,sql FROM " + catalog +
                        " WHERE name=? COLLATE NOCASE", (name,)).fetchone()


def _validate_dependencies(conn):
    """Reject dependencies that DROP TABLE could destroy or cascade through.

    Inspect every main and TEMP table, including unfamiliar definitions. Do not
    rely on foreign_keys being enabled: a disabled constraint is still an
    unsupported dependency. TEMP dependencies are rejected conservatively too.
    """
    for schema, catalog in (("main", "main.sqlite_master"),
                            ("temp", "sqlite_temp_master")):
        tables = conn.execute("SELECT name FROM " + catalog +
                              " WHERE type='table'").fetchall()
        for (table,) in tables:
            references = conn.execute("PRAGMA " + schema + ".foreign_key_list(" +
                                      _quote(table) + ")").fetchall()
            for reference in references:
                _need(type(reference[2]) is str and reference[2].lower() not in _TABLES)
    # TEMP triggers attached to main tables are absent from main.sqlite_master.
    for table in _TABLES:
        _need(conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='trigger' "
            "AND tbl_name=? COLLATE NOCASE LIMIT 1", (table,)).fetchone() is None)


def _copy(conn, table, temporary, statement, signature):
    match = _HEADER.match(statement)
    _need(match is not None and match.group(1).lower() == table)
    replacement = statement[:match.start(1)] + _quote(temporary) + statement[match.end(1):]
    conn.execute(replacement)
    _need(_signature(conn, temporary) == signature)
    columns = ",".join(_quote(row[1]) for row in signature)
    conn.execute("INSERT INTO " + _quote(temporary) + " (" + columns + ") SELECT " +
                 columns + " FROM " + _quote(table))
    original_count = conn.execute("SELECT COUNT(*) FROM " + _quote(table)).fetchone()[0]
    copied_count = conn.execute("SELECT COUNT(*) FROM " + _quote(temporary)).fetchone()[0]
    _need(original_count == copied_count)
    # Include storage classes: EXCEPT alone equates INTEGER and REAL values.
    projection = ",".join(item for row in signature
                          for item in (_quote(row[1]), "typeof(" + _quote(row[1]) + ")"))
    for left, right in ((table, temporary), (temporary, table)):
        _need(conn.execute("SELECT " + projection + " FROM " + _quote(left) +
                           " EXCEPT SELECT " + projection + " FROM " + _quote(right) +
                           " LIMIT 1").fetchone() is None)
    conn.execute("DROP TABLE " + _quote(table))
    conn.execute("ALTER TABLE " + _quote(temporary) + " RENAME TO " + _quote(table))


def ensure_metadata_schema(conn, expected_ddl):
    """Preflight all targets, then create/widen inside the caller's transaction.

    No exceptions are intercepted here, including control exceptions. Failed
    validation or DDL must roll back through MetadataStore._transaction().
    """
    _need(conn.in_transaction is True)
    tables, indexes = _expected(expected_ddl)
    _validate_dependencies(conn)
    # Fixed replacement names must be unused, even if no rebuild is necessary.
    for name in (*_TABLES, *_TEMP_NAMES.values()):
        _need(_object(conn, name, temporary=True) is None)
    for name in _TEMP_NAMES.values():
        _need(_object(conn, name) is None)
    plans = []
    for table in _TABLES:
        statement = tables[table]
        new_enum, old_enum = _ENUMS[table]
        legacy = statement.replace(new_enum, old_enum)
        obj = _object(conn, table)
        # An index name belonging to another table must never be adopted by
        # CREATE INDEX IF NOT EXISTS, nor removed during a rebuild.
        for name, index_sql, _ in indexes[table]:
            _need(_object(conn, name, temporary=True) is None)
            existing = _object(conn, name)
            if existing is not None:
                _need(obj is not None and existing[0] == "index"
                      and existing[1].lower() == table
                      and _definition(existing[2], "index", name) ==
                      _definition(index_sql, "index", name))
        if obj is None:
            plans.append((table, "fresh", None))
            continue
        _need(obj[0] == "table" and obj[1].lower() == table)
        actual = _definition(obj[2], "table", table)
        if actual == _definition(statement, "table", table):
            mode, accepted = "current", statement
        else:
            _need(actual == _definition(legacy, "table", table))
            mode, accepted = "legacy", legacy
        signature = _signature(conn, table)
        _validate_indexes(conn, table, accepted, signature, indexes[table])
        _validate_rows(conn, table, accepted, signature)
        plans.append((table, mode, signature))
    # All three schemas, indexes, triggers and existing checks passed before
    # the first CREATE, INSERT, DROP or ALTER statement.
    for table, mode, signature in plans:
        statement = tables[table]
        if mode == "legacy":
            _copy(conn, table, _TEMP_NAMES[table], statement, signature)
        elif mode == "fresh":
            conn.execute(statement)
        else:
            continue
        for _, index_sql, _ in indexes[table]:
            conn.execute(index_sql)
        obj = _object(conn, table)
        _need(obj is not None and _definition(obj[2], "table", table) ==
              _definition(statement, "table", table))
        final_signature = _signature(conn, table)
        if signature is not None:
            _need(final_signature == signature)
        _validate_indexes(conn, table, statement, final_signature, indexes[table])
        _validate_rows(conn, table, statement, final_signature)
