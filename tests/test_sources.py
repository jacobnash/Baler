"""Source registry and runner (Baler#20)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from baler import source_runner, sources


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", "test-gateway-secret")


def _src(**kw):
    base = {"id": "s1", "kind": "haystack", "name": "SkySpark", "enabled": True,
            "settings": {"url": "https://x/api/demo", "user": "u"}, "_secrets": {"password": "pw"},
            "created_at": None, "updated_at": None, "last_run_at": None, "last_ok_at": None,
            "last_error": None, "last_result": None}
    base.update(kw)
    return base


def test_secrets_are_encrypted_and_never_public():
    token = sources._seal({"password": "hunter2"})
    assert "hunter2" not in token and sources._open(token) == {"password": "hunter2"}
    pub = sources.public(_src())
    assert pub["secrets_set"] == ["password"] and "pw" not in str(pub)


def test_settings_are_validated_and_split():
    plain, secret = sources._split("haystack", {"url": "https://x", "tick_seconds": "60", "password": "p"})
    assert plain == {"url": "https://x", "tick_seconds": 60.0} and secret == {"password": "p"}
    with pytest.raises(ValueError, match="unknown settings"):
        sources._split("haystack", {"nope": 1})
    with pytest.raises(ValueError, match="expected a float"):
        sources._split("openmeteo", {"latitude": "north"})
    assert sources.missing_required("haystack", {"url": "x"}, set()) == ["user"]
    assert sources.missing_required("sql", {"query": "q"}, set()) == ["dsn"]  # a required secret


def test_health():
    now = datetime.now(timezone.utc)
    assert sources.public(_src(enabled=False))["health"] == "disabled"
    assert sources.public(_src(settings={"url": "x"}))["health"] == "misconfigured"
    assert sources.public(_src())["health"] == "pending"
    assert sources.public(_src(last_run_at=now, last_ok_at=now))["health"] == "ok"
    failing = _src(last_run_at=now, last_ok_at=now - timedelta(hours=1), last_error="ConnectionError: refused")
    assert sources.public(failing)["health"] == "failing"


def test_effective_settings_defaults_then_settings_then_secrets():
    s = sources.effective_settings(_src())
    assert s["point_filter"] == "point" and s["tick_seconds"] == 300.0 and s["password"] == "pw" and s["user"] == "u"


def test_runner_records_failures_and_runs_on_its_own_interval(monkeypatch):
    calls = []

    def flaky(settings, store, conn):
        calls.append(settings["url"])
        raise ConnectionError("refused")

    monkeypatch.setitem(source_runner.RUNNERS, "haystack", flaky)
    recorded = []
    monkeypatch.setattr(sources, "list_sources", lambda conn, enabled_only=False: [_src(settings={**_src()["settings"], "tick_seconds": 60})])
    monkeypatch.setattr(sources, "record_run", lambda conn, sid, ok, result=None, error=None: recorded.append((sid, ok, error)))
    due: dict = {}
    assert source_runner.tick(None, None, due, 1000.0) == ["s1"]
    assert recorded == [("s1", False, "ConnectionError: refused")]  # failing, with why - not silent
    assert source_runner.tick(None, None, due, 1030.0) == []  # not due yet
    assert source_runner.tick(None, None, due, 1061.0) == ["s1"]
    assert calls == ["https://x/api/demo"] * 2


def test_runner_refuses_a_source_missing_settings():
    ok, _, error = source_runner.run_source(_src(settings={"url": "x"}), None, None)
    assert not ok and "missing required settings: user" in error


@pytest.mark.integration
def test_registry_round_trip_in_postgres():
    from baler.timeseries import connect

    conn = connect()
    sources.ensure_schema(conn)
    src = sources.create_source(conn, "openmeteo", "Test weather", {"latitude": 40.7, "longitude": -74.0})
    try:
        assert sources.public(src)["health"] == "disabled"
        with pytest.raises(ValueError, match="can't enable: missing"):
            sources.update_source(conn, src["id"], settings={"latitude": None}, enabled=True)
        src = sources.update_source(conn, src["id"], enabled=True)
        assert src["enabled"] and sources.public(src)["health"] == "pending"
        sources.record_run(conn, src["id"], False, error="boom")
        assert sources.public(sources.get_source(conn, src["id"]))["health"] == "failing"
        sources.record_run(conn, src["id"], True, {"temp": 1})
        assert sources.public(sources.get_source(conn, src["id"]))["health"] == "ok"
    finally:
        sources.delete_source(conn, src["id"])
