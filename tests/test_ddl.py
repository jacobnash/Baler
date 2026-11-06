"""ddl.run: startup DDL takes the shared advisory lock first, in one transaction."""

from contextlib import contextmanager

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


def test_every_shared_schema_goes_through_the_lock(monkeypatch):
    from baler import auth, derivation_health, faults, point_write

    seen = []
    monkeypatch.setattr(ddl, "run", lambda conn, *sql: seen.append(sql))
    for mod in (auth, faults, derivation_health, point_write):
        mod.ensure_schema(object())
    assert len(seen) == 4
    assert seen[0][1].startswith("ALTER TABLE sites")  # auth also takes scope's column
