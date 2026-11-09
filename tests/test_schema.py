"""baler.schema without a database: the migration list, and that released
migrations stay frozen (#194). The runner itself is tested against
TimescaleDB in test_schema_upgrade.py."""

import hashlib
import logging
import os
import re

import pytest

from baler import migrations, schema

MIGRATIONS_DIR = os.path.dirname(migrations.__file__)

# sha256 of every released migration file. A migration that has shipped
# must mean the same thing on every database forever, so it is never
# edited: change the schema with a new migration instead. (A comment-only
# fix is the one reason to update a hash here.)
RELEASED = {
    "v0001_baseline.py": "1d2d0e4ae1765256e289d1faa354932cc3f1b0f6b194935121534a92dc772ae5",
    "v0002_reading_feed.py": "69e91be0315b236e7e6dde2b7b89aeabb041f6b798cacd8617594d47d80b2a79",
    "v0003_quality_watermark.py": "023829bf0df44304f1c1e376af7a20e73cfd69c4350ccfee665922dcfdb1ce8a",
    "v0004_alerts.py": "79c2a620550d18df9f2317e0659efb56149a3e47e16428f5a33b077d262dd198",
    "v0005_password_reset_mfa.py": "7d7381e49a7a79ce7638ea6bc670e41a91cf85637f27a39d446bece891f01312",
    "v0006_override_tests.py": "e5127763ffc742a03ae61b643b5b6acd347120d5a880b7cd0aedd7e2d5383d28",
}


def test_every_migration_file_is_listed_in_order():
    files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if re.fullmatch(r"v\d{4}_\w+\.py", f))
    listed = [m.__name__.rsplit(".", 1)[1] + ".py" for m in migrations.ALL]
    assert listed == files
    for m, f in zip(migrations.ALL, files):
        assert m.VERSION == int(f[1:5]) and m.NAME and callable(m.plan)
    assert [m.VERSION for m in schema.migrations()] == list(range(1, len(files) + 1))


@pytest.mark.parametrize("name", sorted(RELEASED))
def test_released_migrations_are_frozen(name):
    with open(os.path.join(MIGRATIONS_DIR, name), "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    assert digest == RELEASED[name], (f"{name} changed after release: add a new migration instead "
                                      "(docs-site/pages/schema-migrations.mdx)")


def test_gaps_are_refused(monkeypatch):
    class Skips:
        VERSION, NAME = len(migrations.ALL) + 2, "skips a number"

    monkeypatch.setattr(migrations, "ALL", (*migrations.ALL, Skips))
    with pytest.raises(RuntimeError, match="numbered 1..N"):
        schema.migrations()


class _Info:
    host, port, dbname = "h", 5432, "db"


class _Conn:
    info = _Info()


def test_ensure_leaves_a_newer_database_alone(monkeypatch, caplog):
    """After a rollback the database may be ahead of the code: no upgrade,
    a warning, and the process carries on."""
    schema._ready.discard(schema._key(_Conn()))
    have = {v: {} for v in range(1, schema.head() + 2)}
    monkeypatch.setattr(schema, "applied", lambda c: have)
    monkeypatch.setattr(schema, "upgrade", lambda *a, **k: pytest.fail("upgraded a newer database"))
    with caplog.at_level(logging.WARNING, logger="baler.schema"):
        schema.ensure(_Conn())
    assert "this code knows only up to" in caplog.text
    schema._ready.discard(schema._key(_Conn()))


def test_ensure_upgrades_once_when_behind(monkeypatch):
    schema._ready.discard(schema._key(_Conn()))
    calls = []
    monkeypatch.setattr(schema, "applied", lambda c: {})
    monkeypatch.setattr(schema, "upgrade", lambda c, **k: calls.append(c))
    conn = _Conn()
    schema.ensure(conn)
    schema.ensure(conn)
    assert calls == [conn]
    schema._ready.discard(schema._key(conn))
