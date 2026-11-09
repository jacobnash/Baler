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
    "v0007_operator_writes.py": "3554c157eb694f14695cd149fa8932fd115e5e13376ca5ab2890ce122f6e611c",
    "v0008_manual_results.py": "470a4c4b0316239b9af8b795460966213a6937e6c19bc90a72af4dcd9146b587",
    "v0009_fleet_runs.py": "06f0bd2e6cb0e5bf0d70a51003d91f16ddd76fea6dbbc3251d19b0c7a06fc010",
    "v0010_active_schedules.py": "3c5e052df21ea923cd47f295317b42a1e923a35d9ef23156dec1956e704cd4d0",
    "v0011_assets.py": "31fb56cbe5e367e6abec1ee21b314acb39a112f3a6b7323238fe1400a53fad7b",
    "v0012_documents.py": "3d9a3d0eaf77e40c41395a6559e6e248b65b1cdbe30a3d28710fea12c6f985dc",
    "v0013_warranties.py": "1a11fb5773b542e255fd2dbb5cef52a3d7350b19c5ada0eedc297c5ad18d9645",
    "v0014_pm.py": "859b16ea49b65c4b47aea0a499065fe8578dfecc671ef164218d0c0588e35c33",
    "v0015_deferred.py": "3ede2db1bd42e4abc3fe597ff9c085aa20a5bfb9f44814f80020cd96abb595f5",
    "v0016_bench.py": "11b70140627ffe1a0a13f6e045b2de871a9d6b187ebd2c76a9e9709c2e38bb1d",
    "v0017_doc_proposals.py": "e340ec86a364fce550050e3d72d41b90045b7a733b964d66715d40752cc02562",
    "v0018_spares.py": "2e9caa208ab9b3611605e30e7f713e704a1d30a541d6ba9b267a2ef1518ca96a",
    "v0019_spaces.py": "8ea63b1fe98775a210f0e10a1a74f2330ef3e360f91703dae9a5eb64ce56d246",
    "v0020_requirement_packs.py": "bf07e04a20094a50b491447d9dc6a29af42df28f5929813ad7b0749ad891e7f1",
    "v0021_pack_proposals.py": "e671a941b4d26b864101b74bbd9c36dcd3017825b201f74b4129b7dc34901425",
    "v0022_compliance_log.py": "76bb046143b96e005e4eb5d5ec272e036c0697c6442db30eb57ddc09a72bc942",
    "v0023_performance_baselines.py": "aaab2593e1d34b4c193d9a06442af11af7b5443f08c9499360b097d22291246d",
    "v0024_savings_cases.py": "ab1b3ad721ca8a9f2b87d58e957419347ae996de295fab2381afb326d16634de",
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
