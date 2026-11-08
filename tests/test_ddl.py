"""ddl.run: DDL takes the shared advisory lock first, in one transaction."""

from contextlib import contextmanager

import pytest

from baler import ddl


class FakeConn:
    def __init__(self):
        self.log = []

    @contextmanager
    def transaction(self):
        self.log.append("BEGIN")
        yield
        self.log.append("COMMIT")

    def execute(self, sql, params=None):
        self.log.append((sql, params))


def test_lock_then_statements_in_one_transaction():
    conn = FakeConn()
    ddl.run(conn, "CREATE TABLE a ()", "CREATE TABLE b ()")
    assert conn.log == [
        "BEGIN",
        ("SELECT pg_advisory_xact_lock(%s)", (ddl.LOCK_KEY,)),
        ("CREATE TABLE a ()", None),
        ("CREATE TABLE b ()", None),
        "COMMIT",
    ]


def test_every_module_schema_is_the_migrations(monkeypatch):
    """Each module's ensure_schema is baler.schema.ensure now (#194): no
    module runs its own DDL any more."""
    from baler import (
        auth,
        broker_accounts,
        conventions,
        derivation_health,
        equip_inference,
        faults,
        haystack_pull_state,
        heartbeat,
        mapping_proposals,
        memory,
        point_write,
        quality,
        record_edit,
        schema,
        scope,
        sources,
        sql_pull_state,
    )
    from baler.commissioning import db as cx_db

    seen = []
    monkeypatch.setattr(schema, "ensure", seen.append)
    monkeypatch.setattr(ddl, "run", lambda *a: pytest.fail("a module ran its own DDL"))
    mods = (auth, broker_accounts, conventions, derivation_health, faults, haystack_pull_state, heartbeat, memory,
            point_write, quality, record_edit, scope, sources, sql_pull_state, cx_db)
    for i, mod in enumerate(mods):
        mod.ensure_schema(i)
    mapping_proposals.PgProposals(len(mods)).ensure_schema()
    equip_inference.PgRelationships(len(mods) + 1).ensure_schema()
    assert seen == list(range(len(mods) + 2))
