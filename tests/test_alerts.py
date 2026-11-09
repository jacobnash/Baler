"""baler.alerts without a live stack: conditions, the once-on-start /
once-on-clear state machine, the retrying outbox, the dead-man ping, and a
full Alerter cycle with every dependency faked (#277)."""

from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

from baler import alerts, mailer
from baler.alerts import Condition, Config, Engine

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def cfg(**kw) -> Config:
    base = {"for_s": 60, "clear_s": 60, "repeat_s": 3600}
    base.update(kw)
    return Config(**base)


def status(**states) -> dict:
    """A collect_status()-shaped document with the given card states."""
    doc: dict = {"building": {"last_ingest_at": T0.isoformat()}, "platform": [], "jobs": [], "tools": []}
    for cid, state in states.items():
        layer = "jobs" if cid in ("ingest_listener", "fault_detector", "derivation_engine", "backups") else "platform"
        doc[layer].append({"id": cid, "label": cid.replace("_", " ").title(), "state": state, "detail": f"{cid} {state}"})
    return doc


# ----------------------------------------------------------------- conditions


def test_status_conditions_cover_down_degraded_and_ingest_stopped():
    doc = status(historian="ok", data_api="down", fault_detector="degraded", ingest_listener="quiet", backups="off")
    doc["tools"].append({"id": "charts", "label": "Charts", "state": "down"})
    got = {c.key: c for c in alerts.status_conditions(doc)}
    assert set(got) == {"component:data_api", "component:fault_detector", "component:ingest_listener"}
    assert got["component:data_api"].severity == alerts.CRITICAL
    assert got["component:fault_detector"].severity == alerts.WARNING
    assert got["component:ingest_listener"].title.endswith("stopped")


def test_quiet_ingest_before_any_data_is_not_an_alert():
    doc = status(ingest_listener="quiet")
    doc["building"]["last_ingest_at"] = None
    assert alerts.status_conditions(doc) == []


def test_job_cards_added_later_alert_without_changes_here():
    """Backups / restore drill (#276, #196) report as Jobs cards: picked up generically."""
    doc = status()
    doc["jobs"].append({"id": "restore_drill", "label": "Restore drill", "state": "degraded", "detail": "FAILED"})
    assert [c.key for c in alerts.status_conditions(doc)] == ["component:restore_drill"]


def test_restart_loop():
    c = cfg(restart_count=3, restart_window_s=900, restart_ignore=["baler-migrate"])
    starts = {
        "baler-fault-detector": [T0 - timedelta(minutes=m) for m in (1, 4, 9)],
        "baler-ops-api": [T0 - timedelta(minutes=m) for m in (1, 40, 90)],
        "baler-migrate": [T0 - timedelta(minutes=m) for m in (1, 2, 3)],
    }
    got = alerts.restart_conditions(starts, T0, c)
    assert [x.key for x in got] == ["restarts:baler-fault-detector"]
    assert alerts.restart_conditions(starts, T0, cfg(restart_count=0)) == []


Usage = namedtuple("Usage", "total used free")


def test_disk_low():
    c = cfg(disk_paths=["/pgdata", "/missing"], disk_min_free_pct=10)

    def usage(path):
        if path == "/missing":
            raise FileNotFoundError(path)
        return Usage(100 * 10**9, 95 * 10**9, 5 * 10**9)

    got = {x.key: x for x in alerts.disk_conditions(c, usage)}
    assert got["disk:/pgdata"].severity == alerts.CRITICAL and "5.0% free" in got["disk:/pgdata"].detail
    assert got["disk:/missing"].severity == alerts.WARNING
    assert alerts.disk_conditions(c, lambda p: Usage(100, 50, 50)) == []


def test_cert_expiry():
    c = cfg(cert_hosts=["a.example", "b.example:8443", "c.example"], cert_days=14)
    expiry = {"a.example": T0 + timedelta(days=60), "b.example": T0 + timedelta(days=2)}

    def probe(host, port):
        if host == "c.example":
            raise OSError("certificate has expired")
        assert port == (8443 if host == "b.example" else 443)
        return expiry[host]

    got = {x.key: x for x in alerts.cert_conditions(c, T0, probe)}
    assert set(got) == {"cert:b.example:8443", "cert:c.example"}
    assert got["cert:b.example:8443"].severity == alerts.CRITICAL


def test_gateway_5xx_burst():
    c = cfg(gateway_5xx=5, gateway_window_s=900)
    burst = {"available": True, "count": 7, "by_route": [{"route": "GET /ops/status", "status": 502, "count": 7}]}
    got = alerts.gateway_conditions(c, T0, lambda since, until: burst)
    assert [x.key for x in got] == ["gateway:5xx"] and "GET /ops/status 502 x7" in got[0].detail
    assert alerts.gateway_conditions(c, T0, lambda s, u: {**burst, "count": 4}) == []
    assert alerts.gateway_conditions(c, T0, lambda s, u: {"available": False}) == []
    assert alerts.gateway_conditions(cfg(gateway_5xx=0), T0, lambda s, u: burst) == []


# --------------------------------------------------------------------- engine


DOWN = Condition("component:data_api", alerts.CRITICAL, "Data API down", "HTTP 502")


def test_one_alert_then_one_clear():
    """The acceptance shape: stop a service -> exactly one alert, start it -> exactly one clear."""
    eng = Engine(cfg())
    sent = []
    for minute in range(10):
        sent += eng.step([DOWN], T0 + timedelta(minutes=minute))
    for minute in range(10, 20):
        sent += eng.step([], T0 + timedelta(minutes=minute))
    assert [n.event for n in sent] == [alerts.FIRING, alerts.RESOLVED]
    assert sent[0].alert.opened_at == T0 + timedelta(minutes=1)
    assert eng.alerts == {}


def test_blip_shorter_than_for_seconds_never_alerts():
    eng = Engine(cfg(for_s=120))
    assert eng.step([DOWN], T0) == []
    assert eng.step([DOWN], T0 + timedelta(seconds=60)) == []
    assert eng.step([], T0 + timedelta(seconds=90)) == []
    assert eng.alerts == {}


def test_flap_inside_clear_window_does_not_resend():
    eng = Engine(cfg(for_s=0, clear_s=120))
    assert [n.event for n in eng.step([DOWN], T0)] == [alerts.FIRING]
    assert eng.step([], T0 + timedelta(seconds=30)) == []
    assert eng.step([DOWN], T0 + timedelta(seconds=60)) == []
    assert eng.step([], T0 + timedelta(seconds=90)) == []
    assert [n.event for n in eng.step([], T0 + timedelta(seconds=211))] == [alerts.RESOLVED]


def test_reminders_on_the_repeat_interval():
    eng = Engine(cfg(for_s=0, repeat_s=3600))
    events = []
    for minute in range(0, 181, 10):
        events += [n.event for n in eng.step([DOWN], T0 + timedelta(minutes=minute))]
    assert events == [alerts.FIRING, alerts.REMINDER, alerts.REMINDER, alerts.REMINDER]
    eng2 = Engine(cfg(for_s=0, repeat_s=0))
    assert [n.event for m in range(0, 600, 10) for n in eng2.step([DOWN], T0 + timedelta(minutes=m))] == [alerts.FIRING]


def test_a_failed_source_neither_clears_nor_fires():
    eng = Engine(cfg(for_s=0, clear_s=0))
    eng.step([DOWN], T0)
    assert eng.step([], T0 + timedelta(minutes=5), stale_prefixes=("component:",)) == []
    assert "component:data_api" in eng.alerts


# --------------------------------------------------------------------- outbox


def test_outbox_retries_a_failed_channel_without_resending_the_good_one():
    calls = {"webhook": 0, "email": 0}
    fail = {"email": 2}
    logs: list[str] = []

    def sender(name):
        def send(n):
            calls[name] += 1
            if fail.get(name, 0) > 0:
                fail[name] -= 1
                raise mailer.MailError("connection refused")
        return send

    box = alerts.Outbox({"webhook": sender("webhook"), "email": sender("email")}, log=logs.append)
    eng = Engine(cfg(for_s=0))
    box.add(eng.step([DOWN], T0))
    assert box.flush(T0) == 1 and calls == {"webhook": 1, "email": 1}
    assert box.flush(T0 + timedelta(seconds=10)) == 0 and calls["email"] == 1  # backing off
    assert box.flush(T0 + timedelta(seconds=31)) == 0 and calls["email"] == 2
    assert box.flush(T0 + timedelta(seconds=200)) == 1 and calls == {"webhook": 1, "email": 3}
    assert box.pending == []
    assert sum("failed" in line for line in logs) == 2


def test_outbox_gives_up_after_max_age():
    logs: list[str] = []

    def boom(n):
        raise RuntimeError("down")

    box = alerts.Outbox({"webhook": boom}, max_age_s=3600, log=logs.append)
    box.add(Engine(cfg(for_s=0)).step([DOWN], T0))
    box.flush(T0)
    box.flush(T0 + timedelta(hours=2))
    assert box.pending == [] and "giving up" in logs[-1]


def test_webhook_sender_uses_ssrf_checked_delivery(monkeypatch):
    seen = {}

    def deliver(url, secret, payload, **kw):
        seen.update(url=url, secret=secret, payload=payload, **kw)
        return False, "webhook host 'x' resolves to a non-public address (10.0.0.1) - not allowed"

    monkeypatch.setattr(alerts.webhooks, "deliver", deliver)
    senders = alerts.make_senders(cfg(webhook_url="https://hooks.example/x", webhook_secret="s", instance="site-a"))
    assert list(senders) == ["webhook"]
    n = Engine(cfg(for_s=0)).step([DOWN], T0)[0]
    with pytest.raises(RuntimeError, match="non-public"):
        senders["webhook"](n)
    assert seen["allow_private"] is False and seen["max_attempts"] == 1
    assert seen["payload"]["event"] == "firing" and seen["payload"]["text"].startswith("[site-a] FIRING: Data API down")


def test_email_sender_only_when_mail_is_configured(monkeypatch):
    c = cfg(email_to=["ops@example.org"])
    monkeypatch.delenv("BALER_SMTP_HOST", raising=False)
    assert alerts.make_senders(c) == {}
    monkeypatch.setenv("BALER_SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("BALER_SMTP_FROM", "baler@example.org")
    sent = []
    monkeypatch.setattr(alerts.mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))
    senders = alerts.make_senders(c)
    senders["email"](Engine(cfg(for_s=0)).step([DOWN], T0)[0])
    assert sent[0][0] == ["ops@example.org"] and "FIRING: Data API down" in sent[0][1] and "HTTP 502" in sent[0][2]


# ------------------------------------------------------------------ dead-man


class Resp:
    def __init__(self, status=200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


def test_deadman_off_by_default_and_rate_limited(monkeypatch):
    monkeypatch.setattr(alerts.webhooks, "validate_url", lambda url, allow_private=False: None)
    gets = []
    off = alerts.DeadMan("", 60, get=lambda url, timeout: gets.append(url) or Resp())
    assert off.maybe_ping(T0) is False and gets == []
    dm = alerts.DeadMan("https://hc.example/ping/abc", 60, get=lambda url, timeout: gets.append(url) or Resp())
    assert dm.maybe_ping(T0) is True
    assert dm.maybe_ping(T0 + timedelta(seconds=30)) is False
    assert dm.maybe_ping(T0 + timedelta(seconds=61)) is True
    assert gets == ["https://hc.example/ping/abc"] * 2


def test_deadman_refuses_private_targets():
    logs: list[str] = []
    dm = alerts.DeadMan("http://127.0.0.1:9/ping", 60, get=lambda url, timeout: Resp(), log=logs.append)
    assert dm.maybe_ping(T0) is False and "must use https" in logs[0]


# ------------------------------------------------------------------- Alerter


class FakeConn:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})
        self.closed = False

    def close(self):
        self.closed = True

    def rollback(self):
        pass


def make_alerter(monkeypatch, docs, conn_factory, **cfgkw):
    store: dict = {}
    monkeypatch.setattr(alerts, "load_state", lambda conn: dict(store))
    monkeypatch.setattr(alerts, "save_state", lambda conn, a: (store.clear(), store.update(a)))
    from baler import errors
    monkeypatch.setattr(errors, "recent_starts", lambda conn: {})
    sent: list = []
    pings: list = []
    monkeypatch.setattr(alerts.webhooks, "validate_url", lambda url, allow_private=False: None)
    dm = alerts.DeadMan("https://hc.example/p", 0, get=lambda url, timeout: pings.append(url) or Resp())
    a = alerts.Alerter(cfg(for_s=0, clear_s=0, **cfgkw), connect=conn_factory, collect=lambda: docs[0],
                       senders={"webhook": lambda n: sent.append((n.event, n.alert.key))}, deadman=dm,
                       log=lambda s: None)
    return a, sent, pings, store


def test_alerter_cycle_alerts_clears_and_pings(monkeypatch):
    docs = [status(historian="ok", data_api="down")]
    a, sent, pings, store = make_alerter(monkeypatch, docs, FakeConn)
    r = a.cycle(T0)
    assert r["notices"] == 1 and r["delivered"] == 1 and r["pinged"] is True
    assert "component:data_api" in store
    a.cycle(T0 + timedelta(seconds=30))
    docs[0] = status(historian="ok", data_api="ok")
    a.cycle(T0 + timedelta(seconds=60))
    assert sent == [("firing", "component:data_api"), ("resolved", "component:data_api")]
    assert store == {} and len(pings) == 3


def test_alerter_keeps_alerting_without_the_database_but_stops_pinging(monkeypatch):
    def no_db():
        raise OSError("connection refused")

    docs = [status(historian="down")]
    a, sent, pings, _store = make_alerter(monkeypatch, docs, no_db)
    r = a.cycle(T0)
    assert sent == [("firing", "component:historian")] and r["pinged"] is False and pings == []


def test_alerter_restart_does_not_resend_open_alerts(monkeypatch):
    docs = [status(data_api="down")]
    a, sent, _pings, _store = make_alerter(monkeypatch, docs, FakeConn)
    a.cycle(T0)
    b = alerts.Alerter(a.cfg, connect=FakeConn, collect=lambda: docs[0], senders=a.outbox.senders,
                       deadman=a.deadman, log=lambda s: None)
    b.cycle(T0 + timedelta(seconds=30))
    assert sent == [("firing", "component:data_api")]


def test_alerter_delivery_failure_does_not_block_the_cycle(monkeypatch):
    docs = [status(data_api="down")]
    a, _sent, _pings, _store = make_alerter(monkeypatch, docs, FakeConn)

    def boom(n):
        raise RuntimeError("timeout")

    a.outbox.senders = {"webhook": boom}
    r = a.cycle(T0)
    assert r["delivered"] == 0 and r["pending_deliveries"] == 1 and r["pinged"] is True


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("BALER_ALERT_EMAIL_TO", "a@example.org, b@example.org")
    monkeypatch.setenv("BALER_ALERT_FOR_SECONDS", "nope")
    monkeypatch.setenv("BALER_ALERT_INSTANCE", "nash")
    c = Config.from_env()
    assert c.email_to == ["a@example.org", "b@example.org"] and c.for_s == 120 and c.instance == "nash"
    assert c.deadman_url == "" and "baler-migrate" in c.restart_ignore


def test_registered_sources_feed_the_engine_and_a_broken_one_is_skipped(monkeypatch):
    docs = [status()]
    a, sent, _pings, _store = make_alerter(monkeypatch, docs, FakeConn)
    monkeypatch.setattr(alerts, "_SOURCES", [])
    alerts.register_source("myjob", lambda now: [Condition("myjob:late", alerts.WARNING, "My job is late")])

    def broken(now):
        raise RuntimeError("nope")

    alerts.register_source("broken", broken)
    a.cycle(T0)
    assert sent == [("firing", "myjob:late")]


@pytest.mark.integration
def test_alert_state_and_service_runtime_sql_against_postgres():
    """Migration 0003's tables with the real statements. Runs inside one
    transaction that is rolled back, so the stack's own alerter (which
    rewrites alert_state every cycle) never sees or deletes these rows."""
    from baler import errors, timeseries

    alert = alerts.Alert("test:sql", "firing", alerts.WARNING, "t", "d", T0, T0, T0, T0, None)
    with timeseries.connect() as conn, conn.transaction(force_rollback=True):
        alerts.save_state(conn, {"test:sql": alert})
        got = alerts.load_state(conn)
        assert got["test:sql"].opened_at == T0 and got["test:sql"].state == "firing"
        assert [a["key"] for a in alerts.open_alerts(conn)] == ["test:sql"]
        for _ in range(3):
            conn.execute(errors.START_SQL, ("test-errors-sql", T0, T0))
        conn.execute(errors.ERROR_SQL, ("test-errors-sql", T0, "ValueError: x"))
        row = next(r for r in errors.summary(conn) if r["service"] == "test-errors-sql")
        assert row["starts"] == 3 and row["errors"] == 1 and row["last_error"] == "ValueError: x"
        assert len(errors.recent_starts(conn)["test-errors-sql"]) == 3
