"""
Operator alerts and the dead-man's switch (#277).

`python -m baler.alerts` (the `alerter` compose service) evaluates what
/ops/status already knows every BALER_ALERT_INTERVAL_SECONDS and tells a
person when something breaks:

- every Platform and Jobs card that is down or degraded (a service that
  stopped answering, a daemon whose heartbeat went stale, a rule daemon
  that missed readings or fell behind the reading feed, broker drops, and
  any job card added later - backups and the restore drill report as cards
  too, #276/#196), plus the Ingest listener going quiet after data had
  flowed;
- a process that keeps restarting (service_runtime, baler.errors);
- low free space on the data volume, a TLS certificate about to expire,
  and a burst of gateway 5xx answers (the post-deploy verification signal,
  ops_freshness.gateway_errors);
- anything a later module adds with register_source().

Each condition has a key. It must hold for BALER_ALERT_FOR_SECONDS before
it alerts, then one "firing" notice goes out, a "reminder" every
BALER_ALERT_REPEAT_SECONDS while it lasts (0: none), and one "resolved"
once it has been gone for BALER_ALERT_CLEAR_SECONDS. Notices go to every
configured channel - a generic webhook (signed and SSRF-checked exactly
like fault webhooks, webhooks.deliver/validate_url) and email
(baler.mailer) - through a per-channel outbox: a failed delivery is logged
and retried with backoff on later cycles, never blocking evaluation, the
dead-man ping or /ops/status (a different process).

The engine keeps its state in memory, so it can still say "Historian
down" while Postgres is down, and writes it to alert_state (migration
0004) whenever the database answers, so a restart neither re-sends an
open alert nor forgets its clear, and /ops/status can list what is open.

Dead-man's switch: with BALER_DEADMAN_URL set (off by default) every
cycle that completed - status collected and the database reachable - GETs
that URL, at most every BALER_DEADMAN_INTERVAL_SECONDS. An outside
monitor (any "cron job / heartbeat" check) alerts when the pings stop:
the host down, Docker down, the alerter crashed or Postgres gone.
"""

from __future__ import annotations

import argparse
import functools
import os
import shutil
import socket
import ssl
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from baler import mailer, webhooks

CRITICAL, WARNING = "critical", "warning"
FIRING, REMINDER, RESOLVED, TEST = "firing", "reminder", "resolved", "test"


def _num(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        print(f"alerts: {name}={raw!r} is not a number; using {default:g}", file=sys.stderr)
        return default


def _list(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in (os.environ.get(name) or default).split(",") if p.strip()]


@dataclass
class Config:
    interval_s: float = 30.0
    for_s: float = 120.0
    clear_s: float = 120.0
    repeat_s: float = 4 * 3600.0
    instance: str = "baler"
    webhook_url: str = ""
    webhook_secret: str = ""
    email_to: list[str] = field(default_factory=list)
    allow_private: bool = False
    disk_paths: list[str] = field(default_factory=list)
    disk_min_free_pct: float = 10.0
    cert_hosts: list[str] = field(default_factory=list)
    cert_days: float = 14.0
    gateway_5xx: int = 20
    gateway_window_s: float = 900.0
    restart_count: int = 3
    restart_window_s: float = 900.0
    restart_ignore: list[str] = field(default_factory=list)
    deadman_url: str = ""
    deadman_interval_s: float = 60.0
    retry_max_age_s: float = 24 * 3600.0

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            interval_s=max(5.0, _num("BALER_ALERT_INTERVAL_SECONDS", 30)),
            for_s=_num("BALER_ALERT_FOR_SECONDS", 120),
            clear_s=_num("BALER_ALERT_CLEAR_SECONDS", 120),
            repeat_s=_num("BALER_ALERT_REPEAT_SECONDS", 4 * 3600),
            instance=(os.environ.get("BALER_ALERT_INSTANCE") or "").strip() or socket.gethostname(),
            webhook_url=(os.environ.get("BALER_ALERT_WEBHOOK_URL") or "").strip(),
            webhook_secret=os.environ.get("BALER_ALERT_WEBHOOK_SECRET") or "",
            email_to=_list("BALER_ALERT_EMAIL_TO"),
            allow_private=os.environ.get("BALER_ALLOW_PRIVATE_WEBHOOKS") == "1",
            disk_paths=_list("BALER_ALERT_DISK_PATHS"),
            disk_min_free_pct=_num("BALER_ALERT_DISK_MIN_FREE_PCT", 10),
            cert_hosts=_list("BALER_ALERT_CERT_HOSTS"),
            cert_days=_num("BALER_ALERT_CERT_DAYS", 14),
            gateway_5xx=int(_num("BALER_ALERT_GATEWAY_5XX", 20)),
            gateway_window_s=_num("BALER_ALERT_GATEWAY_WINDOW_SECONDS", 900),
            restart_count=int(_num("BALER_ALERT_RESTART_COUNT", 3)),
            restart_window_s=_num("BALER_ALERT_RESTART_WINDOW_SECONDS", 900),
            restart_ignore=_list("BALER_ALERT_RESTART_IGNORE", "baler-migrate,baler-haystack-import,baler-autotag"),
            deadman_url=(os.environ.get("BALER_DEADMAN_URL") or "").strip(),
            deadman_interval_s=_num("BALER_DEADMAN_INTERVAL_SECONDS", 60),
        )


# ------------------------------------------------------------------ conditions


@dataclass(frozen=True)
class Condition:
    key: str
    severity: str
    title: str
    detail: str = ""


def status_conditions(doc: dict[str, Any]) -> list[Condition]:
    """From a collect_status() document: every Platform/Jobs card that is
    down (critical) or degraded (warning), and the Ingest listener quiet
    once data has flowed. "off" (a job this install doesn't run) and
    "quiet" with no data ever are not problems; Tools (charts, traces) are
    conveniences, not alerts."""
    out = []
    had_data = bool((doc.get("building") or {}).get("last_ingest_at"))
    for card in [*(doc.get("platform") or []), *(doc.get("jobs") or [])]:
        state = card.get("state")
        if state == "down":
            sev = CRITICAL
        elif state == "degraded" or (state == "quiet" and card.get("id") == "ingest_listener" and had_data):
            sev = WARNING
        else:
            continue
        verb = "stopped" if state == "quiet" else state
        out.append(Condition(f"component:{card['id']}", sev, f"{card.get('label') or card['id']} {verb}",
                             str(card.get("detail") or "")))
    return out


def restart_conditions(starts: dict[str, list[datetime]], now: datetime, cfg: Config) -> list[Condition]:
    """A process that started restart_count times or more within
    restart_window_s (compose restarts a crashing container)."""
    if cfg.restart_count <= 0:
        return []
    since = now - timedelta(seconds=cfg.restart_window_s)
    out = []
    for service, times in sorted(starts.items()):
        if service in cfg.restart_ignore:
            continue
        n = sum(1 for t in times if t.astimezone(timezone.utc) >= since)
        if n >= cfg.restart_count:
            out.append(Condition(f"restarts:{service}", CRITICAL, f"{service} is restart-looping",
                                 f"started {n} times in the last {cfg.restart_window_s / 60:.0f} min"))
    return out


def disk_conditions(cfg: Config, usage: Callable[[str], Any] = shutil.disk_usage) -> list[Condition]:
    out = []
    for path in cfg.disk_paths:
        try:
            u = usage(path)
        except OSError as exc:
            out.append(Condition(f"disk:{path}", WARNING, f"can't read free space on {path}", str(exc)))
            continue
        free_pct = 100.0 * u.free / u.total if u.total else 100.0
        if free_pct < cfg.disk_min_free_pct:
            out.append(Condition(f"disk:{path}", CRITICAL, f"disk low on {path}",
                                 f"{free_pct:.1f}% free ({u.free / 1e9:.1f} GB of {u.total / 1e9:.1f} GB); "
                                 f"alert below {cfg.disk_min_free_pct:g}%"))
    return out


def cert_not_after(host: str, port: int, timeout: float = 5.0) -> datetime:
    """The expiry of the certificate `host:port` presents. Verified like
    any client would, so an already expired or untrusted one raises."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock, \
            ctx.wrap_socket(sock, server_hostname=host) as tls:
        cert = tls.getpeercert() or {}
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(str(cert["notAfter"])), timezone.utc)


def cert_conditions(cfg: Config, now: datetime, probe: Callable[[str, int], datetime] = cert_not_after) -> list[Condition]:
    out = []
    for item in cfg.cert_hosts:
        host, _, port = item.partition(":")
        key = f"cert:{item}"
        try:
            expires = probe(host, int(port or 443))
        except Exception as exc:  # noqa: BLE001 - expired/untrusted/unreachable all mean "look at it"
            out.append(Condition(key, CRITICAL, f"TLS certificate check failed for {item}", str(exc)))
            continue
        days = (expires - now).total_seconds() / 86400
        if days < cfg.cert_days:
            out.append(Condition(key, CRITICAL if days < 3 else WARNING, f"TLS certificate for {item} expires soon",
                                 f"expires {expires.date().isoformat()} ({days:.1f} days)"))
    return out


def gateway_conditions(cfg: Config, now: datetime, read: Callable[..., dict] | None = None) -> list[Condition]:
    """A burst of gateway 5xx answers in the last gateway_window_s - the
    same log post-deploy verification reads (GET /ops/gateway-errors)."""
    if cfg.gateway_5xx <= 0:
        return []
    if read is None:
        from baler import ops_freshness

        read = ops_freshness.gateway_errors
    got = read(now - timedelta(seconds=cfg.gateway_window_s), None)
    if not got.get("available") or got.get("count", 0) < cfg.gateway_5xx:
        return []
    top = ", ".join(f"{r['route']} {r['status']} x{r['count']}" for r in (got.get("by_route") or [])[:3])
    return [Condition("gateway:5xx", WARNING, "gateway answering with server errors",
                      f"{got['count']} 5xx answers in the last {cfg.gateway_window_s / 60:.0f} min: {top}")]


# A hook for conditions this module doesn't know about: register_source(fn)
# with fn(now) -> list[Condition]. A source that raises is logged and
# skipped for that cycle (its open alerts stay open, they don't clear).
_SOURCES: list[tuple[str, Callable[[datetime], list[Condition]]]] = []


def register_source(name: str, fn: Callable[[datetime], list[Condition]]) -> None:
    _SOURCES.append((name, fn))


# ---------------------------------------------------------------------- engine


@dataclass
class Alert:
    key: str
    state: str  # "pending" (seen, not yet held for for_s) or "firing"
    severity: str
    title: str
    detail: str
    first_seen_at: datetime
    last_seen_at: datetime
    opened_at: datetime | None = None
    last_sent_at: datetime | None = None
    absent_since: datetime | None = None

    def public(self) -> dict[str, Any]:
        def iso(t):
            return t.isoformat() if t else None

        return {"key": self.key, "state": self.state, "severity": self.severity, "title": self.title,
                "detail": self.detail, "opened_at": iso(self.opened_at), "last_seen_at": iso(self.last_seen_at),
                "last_sent_at": iso(self.last_sent_at)}


@dataclass(frozen=True)
class Notice:
    event: str  # firing / reminder / resolved / test
    alert: Alert
    at: datetime


class Engine:
    """The alert state machine, pure: step() takes this cycle's conditions
    and returns the notices to send. `stale_keys`: keys whose source failed
    this cycle - their alerts neither clear nor count as absent."""

    def __init__(self, cfg: Config, alerts: dict[str, Alert] | None = None):
        self.cfg = cfg
        self.alerts: dict[str, Alert] = dict(alerts or {})

    def step(self, conditions: list[Condition], now: datetime, stale_prefixes: tuple[str, ...] = ()) -> list[Notice]:
        notices: list[Notice] = []
        seen = set()
        for c in conditions:
            if c.key in seen:
                continue
            seen.add(c.key)
            a = self.alerts.get(c.key)
            if a is None:
                a = self.alerts[c.key] = Alert(c.key, "pending", c.severity, c.title, c.detail, now, now)
            a.severity, a.title, a.detail, a.last_seen_at, a.absent_since = c.severity, c.title, c.detail, now, None
            if a.state == "pending" and (now - a.first_seen_at).total_seconds() >= self.cfg.for_s:
                a.state, a.opened_at, a.last_sent_at = "firing", now, now
                notices.append(Notice(FIRING, replace(a), now))
            elif (a.state == "firing" and self.cfg.repeat_s > 0 and a.last_sent_at is not None
                  and (now - a.last_sent_at).total_seconds() >= self.cfg.repeat_s):
                a.last_sent_at = now
                notices.append(Notice(REMINDER, replace(a), now))
        for key in list(self.alerts):
            if key in seen or key.startswith(stale_prefixes):
                continue
            a = self.alerts[key]
            if a.state == "pending":
                del self.alerts[key]  # never held long enough to alert: nothing to clear
                continue
            a.absent_since = a.absent_since or now
            if (now - a.absent_since).total_seconds() >= self.cfg.clear_s:
                del self.alerts[key]
                notices.append(Notice(RESOLVED, replace(a), now))
        return notices

    def firing(self) -> list[Alert]:
        return [a for a in self.alerts.values() if a.state == "firing"]


# ---------------------------------------------------------------------- outbox


def notice_payload(n: Notice, instance: str) -> dict[str, Any]:
    a = n.alert
    word = {FIRING: "FIRING", REMINDER: "STILL FIRING", RESOLVED: "RESOLVED", TEST: "TEST"}[n.event]
    text = f"[{instance}] {word}: {a.title}" + (f" - {a.detail}" if a.detail and n.event != RESOLVED else "")
    return {
        "source": "baler", "instance": instance, "event": n.event, "key": a.key, "severity": a.severity,
        "title": a.title, "detail": a.detail, "opened_at": a.opened_at.isoformat() if a.opened_at else None,
        "at": n.at.isoformat(), "text": text,
    }


def email_text(n: Notice, instance: str) -> tuple[str, str]:
    p = notice_payload(n, instance)
    lines = [p["text"], "", f"Alert:    {p['key']} ({p['severity']})", f"Event:    {p['event']} at {p['at']}"]
    if p["opened_at"]:
        lines.append(f"Opened:   {p['opened_at']}")
    if p["detail"]:
        lines.append(f"Detail:   {p['detail']}")
    lines += ["", "See Ops in Baler for the full status. This message was sent by baler.alerts."]
    return p["text"], "\n".join(lines) + "\n"


@dataclass
class Delivery:
    channel: str
    notice: Notice
    attempts: int = 0
    next_at: datetime | None = None
    last_error: str | None = None


class Outbox:
    """Undelivered notices, per channel. One attempt per delivery per
    cycle, then backoff (30 s doubling, at most 30 min); dropped, with a
    log line, once older than retry_max_age_s."""

    def __init__(self, senders: dict[str, Callable[[Notice], None]], max_age_s: float = 24 * 3600.0,
                 log: Callable[[str], None] = print):
        self.senders = senders
        self.max_age_s = max_age_s
        self.pending: list[Delivery] = []
        self.log = log

    def add(self, notices: list[Notice]) -> None:
        for n in notices:
            for ch in self.senders:
                self.pending.append(Delivery(ch, n))

    def flush(self, now: datetime) -> int:
        """Try what's due; returns how many were delivered."""
        done = 0
        keep: list[Delivery] = []
        for d in self.pending:
            if d.next_at and d.next_at > now:
                keep.append(d)
                continue
            try:
                self.senders[d.channel](d.notice)
                done += 1
                continue
            except Exception as exc:  # noqa: BLE001 - every failure is logged and retried
                d.attempts += 1
                d.last_error = str(exc)
            age = (now - d.notice.at).total_seconds()
            if age >= self.max_age_s:
                self.log(f"alerts: giving up on {d.channel} {d.notice.event} {d.notice.alert.key} after "
                         f"{d.attempts} attempts: {d.last_error}")
                continue
            d.next_at = now + timedelta(seconds=min(30 * 2 ** (d.attempts - 1), 1800))
            self.log(f"alerts: {d.channel} delivery of {d.notice.event} {d.notice.alert.key} failed "
                     f"(attempt {d.attempts}, retry at {d.next_at.isoformat()}): {d.last_error}")
            keep.append(d)
        self.pending = keep
        return done


def make_senders(cfg: Config) -> dict[str, Callable[[Notice], None]]:
    senders: dict[str, Callable[[Notice], None]] = {}
    if cfg.webhook_url:
        def send_webhook(n: Notice) -> None:
            ok, err = webhooks.deliver(cfg.webhook_url, cfg.webhook_secret, notice_payload(n, cfg.instance),
                                       max_attempts=1, allow_private=cfg.allow_private)
            if not ok:
                raise RuntimeError(err or "webhook delivery failed")

        senders["webhook"] = send_webhook
    if cfg.email_to and mailer.configured():
        def send_email(n: Notice) -> None:
            subject, body = email_text(n, cfg.instance)
            mailer.send(cfg.email_to, subject, body)

        senders["email"] = send_email
    return senders


# --------------------------------------------------------------- dead-man ping


class DeadMan:
    def __init__(self, url: str, interval_s: float, allow_private: bool = False,
                 get: Callable[..., Any] = requests.get, log: Callable[[str], None] = print):
        self.url, self.interval_s, self.allow_private = url, interval_s, allow_private
        self.get, self.log = get, log
        self.last_ok: datetime | None = None

    def maybe_ping(self, now: datetime) -> bool:
        """Ping if due. Returns whether a ping was sent successfully."""
        if not self.url or (self.last_ok and (now - self.last_ok).total_seconds() < self.interval_s):
            return False
        try:
            webhooks.validate_url(self.url, allow_private=self.allow_private)
            resp = self.get(self.url, timeout=10)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - logged, tried again next cycle
            self.log(f"alerts: dead-man ping failed: {exc}")
            return False
        self.last_ok = now
        return True


# ----------------------------------------------------------------- persistence


def load_state(conn) -> dict[str, Alert]:
    rows = conn.execute(
        "SELECT key, state, severity, title, detail, first_seen_at, last_seen_at, opened_at, last_sent_at, "
        "absent_since FROM alert_state").fetchall()
    return {r[0]: Alert(r[0], r[1], r[2], r[3], r[4] or "", r[5], r[6], r[7], r[8], r[9]) for r in rows}


def save_state(conn, alerts: dict[str, Alert]) -> None:
    with conn.transaction():
        for a in alerts.values():
            conn.execute(
                "INSERT INTO alert_state (key, state, severity, title, detail, first_seen_at, last_seen_at, opened_at, "
                "last_sent_at, absent_since, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
                "ON CONFLICT (key) DO UPDATE SET state=EXCLUDED.state, severity=EXCLUDED.severity, "
                "title=EXCLUDED.title, detail=EXCLUDED.detail, last_seen_at=EXCLUDED.last_seen_at, "
                "opened_at=EXCLUDED.opened_at, last_sent_at=EXCLUDED.last_sent_at, "
                "absent_since=EXCLUDED.absent_since, updated_at=now()",
                (a.key, a.state, a.severity, a.title, a.detail, a.first_seen_at, a.last_seen_at, a.opened_at,
                 a.last_sent_at, a.absent_since))
        conn.execute("DELETE FROM alert_state WHERE NOT (key = ANY(%s))", (list(alerts),))


def open_alerts(conn) -> list[dict[str, Any]]:
    """Firing alerts for /ops/status; [] without the table."""
    try:
        return [a.public() for a in sorted(load_state(conn).values(), key=lambda a: a.opened_at or a.first_seen_at)
                if a.state == "firing"]
    except Exception:  # noqa: BLE001 - migration 0004 not applied yet
        return []


# ------------------------------------------------------------------------ loop


class Alerter:
    """One cycle: collect conditions, step the engine, persist, deliver,
    ping. Every dependency is injectable for the tests."""

    def __init__(self, cfg: Config, *, connect: Callable[[], Any] | None = None,
                 collect: Callable[[], dict] | None = None, senders: dict | None = None,
                 deadman: DeadMan | None = None, log: Callable[[str], None] = print):
        self.cfg, self.log = cfg, log
        self.engine = Engine(cfg)
        self.outbox = Outbox(make_senders(cfg) if senders is None else senders, cfg.retry_max_age_s, log)
        self.deadman = deadman or DeadMan(cfg.deadman_url, cfg.deadman_interval_s, cfg.allow_private, log=log)
        self._connect = connect or _default_connect
        self._collect = collect or _default_collect
        self._loaded = False

    def _gather(self, now: datetime, conn) -> tuple[list[Condition], tuple[str, ...]]:
        conds: list[Condition] = []
        stale: list[str] = []

        def run(prefix: str, fn: Callable[[], list[Condition]]) -> None:
            try:
                conds.extend(fn())
            except Exception as exc:  # noqa: BLE001 - one broken source mustn't stop the rest
                self.log(f"alerts: source {prefix} failed: {exc!r}")
                stale.append(prefix)

        run("component:", lambda: status_conditions(self._collect()))
        if conn is not None:
            from baler import errors

            run("restarts:", lambda: restart_conditions(errors.recent_starts(conn), now, self.cfg))
        else:
            stale.append("restarts:")
        run("disk:", lambda: disk_conditions(self.cfg))
        run("cert:", lambda: cert_conditions(self.cfg, now))
        run("gateway:", lambda: gateway_conditions(self.cfg, now))
        for name, fn in _SOURCES:
            run(f"{name}:", functools.partial(fn, now))
        return conds, tuple(stale)

    def cycle(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        conn = None
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001 - the historian card says so too
            self.log(f"alerts: database unreachable: {exc!r}")
        try:
            if conn is not None and not self._loaded:
                try:
                    stored = load_state(conn)
                    for key, a in stored.items():
                        self.engine.alerts.setdefault(key, a)
                    self._loaded = True
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    self.log(f"alerts: couldn't load alert_state: {exc!r}")
            conds, stale = self._gather(now, conn)
            notices = self.engine.step(conds, now, stale)
            for n in notices:
                self.log(f"alerts: {n.event} {n.alert.key}: {n.alert.title}"
                         + (f" - {n.alert.detail}" if n.alert.detail else ""))
            self.outbox.add(notices)
            saved = False
            if conn is not None:
                try:
                    save_state(conn, self.engine.alerts)
                    saved = True
                except Exception as exc:  # noqa: BLE001
                    self.log(f"alerts: couldn't save alert_state: {exc!r}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"alerts: closing the database connection: {exc!r}")
        delivered = self.outbox.flush(now)
        pinged = self.deadman.maybe_ping(now) if saved else False
        return {"conditions": len(conds), "notices": len(notices), "delivered": delivered,
                "pending_deliveries": len(self.outbox.pending), "pinged": pinged, "db": saved}

    def send_test(self) -> int:
        now = datetime.now(timezone.utc)
        a = Alert("test", "firing", WARNING, "test alert from baler.alerts", "if you can read this, delivery works",
                  now, now, now, now)
        self.outbox.add([Notice(TEST, a, now)])
        return self.outbox.flush(now)


def _default_connect():
    import psycopg

    from baler import timeseries

    return psycopg.connect(timeseries.DEFAULT_DSN, connect_timeout=3, autocommit=True)


def _default_collect() -> dict:
    from baler import ops_status

    return ops_status.collect_status(None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Baler operator alerts and dead-man's switch (#277)")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--test", action="store_true", help="send a test notice to every configured channel and exit")
    args = parser.parse_args(argv)

    from baler import tracing

    tracing.init_tracing("baler-alerter")
    cfg = Config.from_env()
    alerter = Alerter(cfg)
    channels = ", ".join(alerter.outbox.senders) or "none (alerts only show in Ops)"
    print(f"alerter: channels {channels}; dead-man {'on' if cfg.deadman_url else 'off'}; "
          f"every {cfg.interval_s:g}s, for {cfg.for_s:g}s, clear {cfg.clear_s:g}s, repeat {cfg.repeat_s:g}s", flush=True)
    if cfg.email_to and not mailer.configured():
        print("alerter: BALER_ALERT_EMAIL_TO is set but email isn't (BALER_SMTP_HOST/BALER_SMTP_FROM)", flush=True)
    if args.test:
        sent = alerter.send_test()
        print(f"alerter: test notice delivered to {sent} of {len(alerter.outbox.senders)} channel(s)", flush=True)
        return 0 if sent == len(alerter.outbox.senders) and sent else 1
    while True:
        try:
            result = alerter.cycle()
            if args.once:
                print(f"alerter: {result}", flush=True)
                return 0
        except Exception as exc:  # noqa: BLE001 - never let the alerter die on one bad cycle
            print(f"alerter: cycle failed: {exc!r}", flush=True)
            if args.once:
                return 1
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    sys.exit(main())
