"""
The fault-detection daemon: Cur-mode rules evaluated on every stored
reading, a periodic His-mode sweep for stale/stuck rules, and controller
alarms raised and cleared as faults. Every rule transition (fault opens or
resolves) fans out to matching webhooks off the calling thread, in its own
daemon thread, so one slow/dead URL never stalls fault evaluation for every
other point behind it.

Readings come from the reading feed (baler.reading_feed, #243), not from an
MQTT subscription of its own: mqtt_listener appends every batch it commits,
and this daemon reads the batches after its durable offset. A QoS 0
`fbf/#` subscription used to be its input, and the broker silently dropped
what it couldn't deliver as soon as the daemon fell behind (42,558
deliveries in 54 s at 10k points). Now a reading can't be dropped, the
daemon resumes where it stopped, and lag and misses are in its heartbeat
and /ops/status.

Work is proportional to what arrived, not to how many points exist:
- Cur mode: each point's matching rules are resolved once (RuleCache's
  per-point index, rebuilt when rules.json or a Brick roster changes),
  and a reading costs a dict lookup and a comparison; Postgres is only
  touched on a transition (OpenFaults).
- His mode: one query per stuck window (a GROUP BY over the window's rows)
  and one per stale rule (point_registry candidates, confirmed exactly),
  not `SELECT DISTINCT point_uri FROM point_history` plus a query per
  point per rule.
- Quality flags (baler.quality, #268): an incremental pass over what is
  new since a persisted watermark, on its own thread and connection, so
  His-mode cycles never wait for it.

The only code shared with mqtt_listener.py is ingest.topic_to_point_uri -
identity must match exactly, that's a correctness requirement, not a
style choice.
"""

import argparse
import collections
import fnmatch
import json
import os
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from baler import (
    bacnet_alarms,
    heartbeat,
    his_checks,
    json_store,
    quality,
    reading_feed,
    timeseries,
    tracing,
)
from baler.faults import (
    close_fault,
    close_orphaned_faults,
    ensure_schema,
    is_webhook_disabled,
    open_fault,
    record_webhook_failure,
    record_webhook_success,
)
from baler.ingest import topic_to_point_uri
from baler.remote_store import RemoteStore
from baler.timeseries import connect
from baler.webhooks import deliver, matches_filter

tracer = tracing.get_tracer(__name__)

BRICK_PREFIX = "PREFIX brick: <https://brickschema.org/schema/Brick#>"


def _brick_class_roster(store, brick_class: str) -> set[str]:
    rows = store.query(f"{BRICK_PREFIX} SELECT ?point WHERE {{ ?point a brick:{brick_class} }}")
    return {str(row.point) for row in rows}


class RuleCache:
    """Reloads rules.json only when its mtime changes - a cheap stat()
    per get(), not a disk read let alone an Oxigraph query. Also resolves
    each brick_class Cur-mode rule's point roster on the same reload
    trigger (one SPARQL query per such rule), and again every
    `roster_ttl` seconds when given, so a point classified after the rules
    were loaded is picked up. store=None (the default) just means no
    brick_class rule can ever match.

    cur_rules_for(point_uri) is the per-point index (#243): the Cur-mode
    range rules that apply to a point, worked out (fnmatch per rule) the
    first time the point is seen and kept until the rules or a roster
    change. A reading then costs a dict lookup, not a glob per rule."""

    def __init__(self, path: str, store=None, roster_ttl: float | None = None):
        self.path = path
        self.store = store
        self.roster_ttl = roster_ttl
        self._mtime: float | None = None
        self._rules: list[dict] = []
        self._brick_roster: dict[str, set[str]] = {}
        self._roster_at = 0.0
        # (Cur-mode range rules, Brick roster, point -> rules index), swapped
        # as one tuple so the feed thread never mixes an old index with new
        # rules while the sweep thread reloads.
        self._cur: tuple[list[dict], dict[str, set[str]], dict[str, tuple[dict, ...]]] = ([], {}, {})
        self._lock = threading.Lock()

    def get(self) -> list[dict]:
        with self._lock:
            try:
                mtime = os.stat(self.path).st_mtime
            except FileNotFoundError:
                if self._mtime is not None or self._rules:
                    self._set([], None)
                return self._rules
            if mtime != self._mtime:
                self._set(json_store.load(self.path), mtime)
            elif self.roster_ttl and time.monotonic() - self._roster_at >= self.roster_ttl:
                self._brick_roster = self._build_brick_roster()
                self._cur = (self._cur[0], self._brick_roster, {})
            return self._rules

    def _set(self, rules: list[dict], mtime: float | None) -> None:
        self._rules, self._mtime = rules, mtime
        self._brick_roster = self._build_brick_roster()
        self._cur = ([r for r in rules if r.get("mode") == "cur" and r.get("type") == "range"], self._brick_roster, {})

    def _build_brick_roster(self) -> dict[str, set[str]]:
        self._roster_at = time.monotonic()
        roster: dict[str, set[str]] = {}
        if self.store is None:
            return roster
        for rule in self._rules:
            brick_class = rule.get("applies_to", {}).get("brick_class")
            if rule.get("mode") != "cur" or brick_class is None:
                continue
            for point_uri in _brick_class_roster(self.store, brick_class):
                roster.setdefault(point_uri, set()).add(rule["id"])
        return roster

    def rule_ids_for_point(self, point_uri: str) -> set[str]:
        return self._brick_roster.get(point_uri, set())

    def cur_rules_for(self, point_uri: str) -> tuple[dict, ...]:
        """The Cur-mode range rules for this point, as of the last get()."""
        cur_rules, roster, index = self._cur
        rules = index.get(point_uri)
        if rules is None:
            topic = reading_feed.topic_of(point_uri)
            brick = roster.get(point_uri, set())
            rules = tuple(r for r in cur_rules if _cur_rule_applies(r, topic, brick))
            index[point_uri] = rules
        return rules


def _cur_rule_applies(rule: dict, topic: str, brick_rule_ids: set[str]) -> bool:
    applies_to = rule["applies_to"]
    if "brick_class" in applies_to:
        return rule["id"] in brick_rule_ids
    return fnmatch.fnmatch(topic, applies_to["topic_glob"])


def _fire_webhooks(webhooks_path: str, event: str, rule_id: str, point_uri: str, status: str, detail: dict | None) -> None:
    """One daemon thread per matching webhook - delivery must never block
    the caller (the MQTT callback thread, or the His-mode sweep loop).
    ponytail: no pool cap on these threads - add a bounded
    ThreadPoolExecutor if a fault storm ever spawns too many at once."""
    webhooks = json_store.load(webhooks_path)
    payload = {"event": event, "rule_id": rule_id, "point_uri": point_uri, "status": status, "detail": detail}
    for webhook in webhooks:
        if not matches_filter(webhook, rule_id):
            continue
        threading.Thread(target=_deliver_and_record, args=(webhook, payload), daemon=True).start()


def _deliver_and_record(webhook: dict, payload: dict) -> None:
    conn = connect()
    try:
        if is_webhook_disabled(conn, webhook["id"]):
            return
        # Deployment-level escape hatch, not per-webhook config - a webhook
        # registered through fault_api.py can't opt itself out of the SSRF
        # check (that would let a compromised/malicious registration bypass
        # the whole point of it). Only ever set true for local dev/test.
        allow_private = os.environ.get("BALER_ALLOW_PRIVATE_WEBHOOKS") == "1"
        success, error = deliver(webhook["url"], webhook["secret"], payload, allow_private=allow_private)
        with tracer.start_as_current_span("fault_detector.webhook_delivery") as span:
            span.set_attribute("webhook_id", webhook["id"])
            span.set_attribute("success", success)
            if success:
                record_webhook_success(conn, webhook["id"])
            else:
                span.set_attribute("error", error or "")
                disabled_now = record_webhook_failure(conn, webhook["id"], error or "unknown error")
                span.set_attribute("disabled", disabled_now)
    finally:
        conn.close()


class OpenFaults:
    """Which (rule_id, point_uri) pairs have an open fault, kept in memory
    so a reading (or sweep result) that changes nothing costs nothing:
    close_fault on every in-range reading was an UPDATE round trip per
    reading per matching rule - ~1,200/s at 10k points, the bulk of
    Postgres's statements (Baler#190) - to resolve faults that almost never
    exist. Shared by the Cur-mode thread and the His-mode sweep, which
    refreshes it from Postgres every cycle, so a fault opened or closed
    elsewhere (a rule deleted through fault_api) is picked up within one
    sweep; a stale "open" entry only costs one no-op UPDATE."""

    def __init__(self):
        self._lock = threading.Lock()
        self._open: set[tuple[str, str]] = set()
        self._opened_during_refresh: set[tuple[str, str]] = set()

    def refresh(self, conn) -> None:
        with self._lock:
            self._opened_during_refresh = set()
        rows = {(r, p) for r, p in conn.execute("SELECT rule_id, point_uri FROM faults WHERE ended_at IS NULL")}
        with self._lock:
            # an open that committed after the SELECT's snapshot must survive
            self._open = rows | self._opened_during_refresh

    def is_open(self, rule_id: str, point_uri: str) -> bool:
        with self._lock:
            return (rule_id, point_uri) in self._open

    def opened(self, rule_id: str, point_uri: str) -> None:
        with self._lock:
            self._open.add((rule_id, point_uri))
            self._opened_during_refresh.add((rule_id, point_uri))

    def closed(self, rule_id: str, point_uri: str) -> None:
        with self._lock:
            self._open.discard((rule_id, point_uri))
            self._opened_during_refresh.discard((rule_id, point_uri))


def _open_if_new(fault_conn, open_faults: OpenFaults | None, rule: dict, point_uri: str, now, detail) -> int | None:
    """open_fault, skipped when it's known to be open already (it'd be a
    no-op: ON CONFLICT DO NOTHING)."""
    if open_faults is not None and open_faults.is_open(rule["id"], point_uri):
        return None
    fault_id = open_fault(fault_conn, rule["id"], point_uri, now, detail=detail, severity=rule.get("severity", "warning"))
    if open_faults is not None:
        open_faults.opened(rule["id"], point_uri)  # new, or already open in the DB: open either way
    return fault_id


def _close_if_open(fault_conn, open_faults: OpenFaults | None, rule_id: str, point_uri: str, now) -> int | None:
    """close_fault, skipped when nothing is known to be open."""
    if open_faults is not None and not open_faults.is_open(rule_id, point_uri):
        return None
    fault_id = close_fault(fault_conn, rule_id, point_uri, now)
    if open_faults is not None:
        open_faults.closed(rule_id, point_uri)
    return fault_id


def evaluate_range_rule(rule: dict, value) -> bool:
    """True means faulted. Non-numeric values never satisfy a range rule -
    reported as not-faulted, not an error; a read failure (value is None
    or a string) is a different concern from a real value being out of
    bounds."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return value < rule["min"] or value > rule["max"]


def _matching_cur_rules(rule_cache, topic: str, point_uri: str) -> list[dict] | tuple[dict, ...]:
    if hasattr(rule_cache, "cur_rules_for"):
        return rule_cache.cur_rules_for(point_uri)
    # any other source of rules (a test double): the same match, per reading
    brick = rule_cache.rule_ids_for_point(point_uri) if hasattr(rule_cache, "rule_ids_for_point") else set()
    return [r for r in rule_cache.get() if r.get("mode") == "cur" and r.get("type") == "range"
            and _cur_rule_applies(r, topic, brick)]


def _evaluate_cur(fault_conn, rule: dict, point_uri: str, value, now, webhooks_path: str,
                  open_faults: OpenFaults | None, fire: Callable[..., None]) -> None:
    faulted = evaluate_range_rule(rule, value)
    if faulted:
        fault_id = _open_if_new(fault_conn, open_faults, rule, point_uri, now,
                                {"value": value, "min": rule["min"], "max": rule["max"]})
        if fault_id is not None:
            fire(webhooks_path, "fault.opened", rule["id"], point_uri, "open", {"value": value})
    else:
        fault_id = _close_if_open(fault_conn, open_faults, rule["id"], point_uri, now)
        if fault_id is not None:
            fire(webhooks_path, "fault.resolved", rule["id"], point_uri, "resolved", {"value": value})


def handle_cur_reading(fault_conn, rule_cache: RuleCache, webhooks_path: str, topic: str, value,
                       open_faults: OpenFaults | None = None) -> None:
    point_uri = str(topic_to_point_uri(topic))
    now = datetime.now(timezone.utc)
    rule_cache.get()  # reload if rules.json changed

    for rule in _matching_cur_rules(rule_cache, topic, point_uri):
        with tracer.start_as_current_span("fault_detector.evaluate_cur_rule") as span:
            span.set_attribute("rule_id", rule["id"])
            span.set_attribute("point_uri", point_uri)
            _evaluate_cur(fault_conn, rule, point_uri, value, now, webhooks_path, open_faults, _fire_webhooks)


def handle_feed_batch(fault_conn, rule_cache: RuleCache, webhooks_path: str, batch: reading_feed.Batch,
                      open_faults: OpenFaults | None = None) -> dict:
    """One committed listener batch from the reading feed: its alarms, then
    every good reading against its point's Cur-mode rules, in ts order -
    the same verdicts, transitions and webhooks as handling each reading's
    MQTT message used to give, at a dict lookup per reading (no span per
    reading either: one per batch). Returns counts for the heartbeat."""
    with tracer.start_as_current_span("fault_detector.feed_batch") as span:
        span.set_attribute("seq", batch.seq)
        span.set_attribute("readings", len(batch.readings))
        for topic, event, ts in batch.alarms:
            def fire(event_name, rule_id, point_uri, status, detail):
                _fire_webhooks(webhooks_path, event_name, rule_id, point_uri, status, detail)

            bacnet_alarms.handle_alarm(fault_conn, topic, event, ts, fire=fire)
        rule_cache.get()  # once per batch: reload if rules.json changed
        now = datetime.now(timezone.utc)
        evaluated = 0
        for point_uri, _ts, value, flag in batch.readings:
            rules = rule_cache.cur_rules_for(point_uri)
            # A reading the device itself flags (#183) neither opens nor
            # resolves a fault; see make_on_message.
            if not rules or not quality.is_good(flag):
                continue
            for rule in rules:
                _evaluate_cur(fault_conn, rule, point_uri, value, now, webhooks_path, open_faults, _fire_webhooks)
                evaluated += 1
        span.set_attribute("evaluations", evaluated)
    return {"readings": len(batch.readings), "evaluations": evaluated, "alarms": len(batch.alarms)}


def make_on_message(fault_conn, rule_cache: RuleCache, webhooks_path: str, open_faults: OpenFaults | None = None):
    """The same handling for one MQTT message (alarm or reading). main()
    reads the reading feed instead (handle_feed_batch); this stays for
    callers that hold a message."""
    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        value = payload.get("value")
        # A controller's own alarm, forwarded by Windrow (Baler#168): raised
        # and cleared as a fault here, never evaluated as a reading.
        if bacnet_alarms.is_alarm(msg.topic, value):
            def fire(event, rule_id, point_uri, status, detail):
                _fire_webhooks(webhooks_path, event, rule_id, point_uri, status, detail)

            with tracer.start_as_current_span("fault_detector.bacnet_alarm") as span:
                span.set_attribute("topic", msg.topic)
                outcome = bacnet_alarms.handle_alarm(fault_conn, msg.topic, value, payload.get("ts"), fire=fire)
                span.set_attribute("outcome", outcome or "unchanged")
            return
        # A reading the device itself flags (fault, overridden, out of
        # service - #183) says nothing about the equipment: it neither opens
        # nor resolves a fault. The open/closed state stays as it was until
        # a good reading arrives.
        if not quality.is_good(quality.from_envelope(payload.get("quality"))):
            return
        handle_cur_reading(fault_conn, rule_cache, webhooks_path, msg.topic, value, open_faults)

    return on_message


def _evaluate_stuck(ts_conn, point_uri: str, stuck_seconds: float, now: datetime) -> bool:
    """One point's stuck verdict: the reference the set-based sweep
    (_STUCK_SQL) gives for every point at once."""
    window_start = now - timedelta(seconds=stuck_seconds)
    n, distinct_values = ts_conn.execute(
        f"""
        SELECT COUNT(*), COUNT(DISTINCT COALESCE(value::text, value_text, value_bool::text))
        FROM point_history WHERE point_uri = %s AND ts >= %s AND {quality.GOOD_SQL}
        """,
        (point_uri, window_start),
    ).fetchone()
    return his_checks.stuck_verdict(n, distinct_values)


def _evaluate_stale(ts_conn, point_uri: str, max_age_seconds: float, now: datetime) -> bool:
    """One point's stale verdict: the reference _stale_verdicts gives."""
    (max_ts,) = ts_conn.execute("SELECT MAX(ts) FROM point_history WHERE point_uri = %s", (point_uri,)).fetchone()
    return his_checks.is_stale(max_ts, now, max_age_seconds)


# One stuck window's verdict inputs for every point that reported in it, in
# one GROUP BY: good samples, and whether they hold exactly one distinct
# value (COUNT(DISTINCT key) = 1 is: some non-null key, and min = max - a
# hash aggregate instead of a sort). Same key as his_checks._key.
_STUCK_SQL = f"""
SELECT point_uri, COUNT(*),
       CASE WHEN COUNT(k) = 0 THEN 0 WHEN MIN(k) = MAX(k) THEN 1 ELSE 2 END
FROM (SELECT point_uri, COALESCE(value::text, value_text, value_bool::text) AS k
      FROM point_history WHERE ts >= %s AND {quality.GOOD_SQL}{{where}}) w
GROUP BY point_uri
"""


def _glob_prefix(glob: str) -> str:
    """The literal start of a glob, before its first wildcard."""
    for i, ch in enumerate(glob):
        if ch in "*?[":
            return glob[:i]
    return glob


def _prefix_filter(prefixes: list[str]) -> tuple[str, list]:
    """SQL narrowing point_uri to these literal prefixes, or no filter if
    any prefix is empty. starts_with, not a range: under a linguistic
    collation `p <= x < p || max char` isn't every x starting with p."""
    if not prefixes or any(not p for p in prefixes):
        return "", []
    unique = sorted(set(prefixes))
    return " AND (" + " OR ".join("starts_with(point_uri, %s)" for _ in unique) + ")", unique


class _Matcher:
    """Which point URIs a His-mode rule applies to, memoized per glob, so
    a sweep pays fnmatch once per point per glob for the life of the
    process, not once per point per rule per sweep."""

    def __init__(self, store):
        self.store = store
        self._globs: dict[str, dict[str, bool]] = {}
        self._rosters: dict[str, set[str]] = {}

    def roster(self, brick_class: str) -> set[str]:
        """Per sweep (reset()): one SPARQL query per Brick class."""
        if brick_class not in self._rosters:
            self._rosters[brick_class] = _brick_class_roster(self.store, brick_class)
        return self._rosters[brick_class]

    def reset(self) -> None:
        self._rosters = {}

    def usable(self, rule: dict) -> bool:
        return "brick_class" not in rule["applies_to"] or self.store is not None

    def prefix(self, rule: dict) -> str:
        """A literal URI prefix every matching point has ('' = any)."""
        glob = rule["applies_to"].get("topic_glob")
        return f"urn:point:{_glob_prefix(glob)}" if glob is not None else ""

    def matches(self, rule: dict, point_uri: str) -> bool:
        applies_to = rule["applies_to"]
        if "brick_class" in applies_to:
            return point_uri in self.roster(applies_to["brick_class"])
        glob = f"urn:point:{applies_to['topic_glob']}"
        memo = self._globs.setdefault(glob, {})
        hit = memo.get(point_uri)
        if hit is None:
            hit = memo[point_uri] = fnmatch.fnmatch(point_uri, glob)
        return hit


def _stuck_counts(ts_conn, window_start: datetime, prefixes: list[str]) -> dict[str, tuple[int, int]]:
    where, params = _prefix_filter(prefixes)
    rows = ts_conn.execute(_STUCK_SQL.format(where=where), [window_start, *params]).fetchall()
    return {uri: (n, d) for uri, n, d in rows}


def _stale_verdicts(ts_conn, matcher: _Matcher, rule: dict, now: datetime) -> dict[str, bool]:
    """{point: stale?} for the rule's points that could be stale, exactly:
    point_registry's coarse last_ts (never newer than the truth, at most
    REGISTRY_GRANULARITY_S older) picks the candidates, and their real
    newest ts (an index probe each) decides. Every other point of the rule
    reported within max_age, so it isn't stale."""
    max_age = rule["max_age_seconds"]
    cutoff = now - timedelta(seconds=max_age - timeseries.REGISTRY_GRANULARITY_S)
    where, params = _prefix_filter([matcher.prefix(rule)])
    rows = ts_conn.execute(f"SELECT point_uri FROM point_registry WHERE last_ts < %s{where}", [cutoff, *params]).fetchall()
    candidates = [u for (u,) in rows if matcher.matches(rule, u)]
    newest = timeseries.probe_points(ts_conn, candidates, "ts", [], [], newest=True) if candidates else {}
    return {u: his_checks.is_stale(newest[u][0] if u in newest else None, now, max_age) for u in candidates}


def _open_points(fault_conn, rule_id: str) -> list[str]:
    return [u for (u,) in fault_conn.execute(
        "SELECT point_uri FROM faults WHERE rule_id = %s AND ended_at IS NULL", (rule_id,)).fetchall()]


def _registered(ts_conn, point_uris: list[str]) -> set[str]:
    if not point_uris:
        return set()
    return {u for (u,) in ts_conn.execute(
        "SELECT point_uri FROM point_registry WHERE point_uri = ANY(%s)", (point_uris,)).fetchall()}


def evaluate_his_rules(
    ts_conn, fault_conn, rules: list[dict], webhooks_path: str, now: datetime | None = None, store=None,
    open_faults: OpenFaults | None = None, matcher: _Matcher | None = None,
) -> dict:
    """Importable/callable synchronously - tests call this directly
    instead of waiting on a real sleep loop. Each His-mode rule applies to
    the points with history (point_registry) matching its topic_glob, or,
    for a brick_class rule, in that Brick class (a point classified into
    the class but that's never reported has no history to evaluate).
    store=None means no brick_class rule can ever match (same degrade as
    RuleCache).

    Set-based (#243), the same verdicts as asking each point in turn:
    - stuck: one GROUP BY per distinct stuck_seconds over the window's
      good samples (his_checks.stuck_verdict on its counts); a point with
      no samples in the window isn't stuck;
    - stale: _stale_verdicts.
    Faults open for points found faulted; an open fault of the rule on a
    point with history that the rule still applies to and that isn't
    faulted now resolves. Returns counts for the heartbeat."""
    now = now or datetime.now(timezone.utc)
    matcher = matcher or _Matcher(store)
    matcher.reset()
    his = [r for r in rules if r.get("mode") == "his" and r.get("type") in ("stuck", "stale") and matcher.usable(r)]
    stats = {"rules": len(his), "points_faulted": 0, "stuck_rows_points": 0, "stale_candidates": 0}

    windows: dict[float, list[dict]] = {}
    for rule in his:
        if rule["type"] == "stuck":
            windows.setdefault(rule["stuck_seconds"], []).append(rule)
    counts = {w: _stuck_counts(ts_conn, now - timedelta(seconds=w), [matcher.prefix(r) for r in rs])
              for w, rs in windows.items()}
    stats["stuck_rows_points"] = sum(len(c) for c in counts.values())

    for rule in his:
        with tracer.start_as_current_span("fault_detector.evaluate_his_rule") as span:
            span.set_attribute("rule_id", rule["id"])
            if rule["type"] == "stuck":
                faulted = {u for u, (n, d) in counts[rule["stuck_seconds"]].items()
                           if his_checks.stuck_verdict(n, d) and matcher.matches(rule, u)}
            else:
                verdicts = _stale_verdicts(ts_conn, matcher, rule, now)
                stats["stale_candidates"] += len(verdicts)
                faulted = {u for u, stale in verdicts.items() if stale}
            span.set_attribute("faulted", len(faulted))
            stats["points_faulted"] += len(faulted)

            for point_uri in sorted(faulted):
                fault_id = _open_if_new(fault_conn, open_faults, rule, point_uri, now, None)
                if fault_id is not None:
                    _fire_webhooks(webhooks_path, "fault.opened", rule["id"], point_uri, "open", None)

            still_open = [u for u in _open_points(fault_conn, rule["id"]) if u not in faulted and matcher.matches(rule, u)]
            for point_uri in sorted(_registered(ts_conn, still_open)):
                fault_id = _close_if_open(fault_conn, open_faults, rule["id"], point_uri, now)
                if fault_id is not None:
                    _fire_webhooks(webhooks_path, "fault.resolved", rule["id"], point_uri, "resolved", None)
    return stats


def run_feed(rule_cache: RuleCache, webhooks_path: str, open_faults: OpenFaults, status: heartbeat.Status,
             stop: threading.Event | None = None) -> None:
    """The Cur-mode thread: the reading feed, batch by batch, on its own
    connections (psycopg connections aren't for concurrent use)."""
    fault_conn, feed_conn = connect(), connect()
    consumer = reading_feed.FeedConsumer(feed_conn, "fault_detector")
    consumer.start()
    last_log = [time.monotonic(), 0]

    def handle(batch):
        t0 = time.monotonic()
        status.add("cur_evaluations", handle_feed_batch(fault_conn, rule_cache, webhooks_path, batch, open_faults)["evaluations"])
        # cumulative seconds evaluating: its rate is how busy Cur mode keeps
        # this thread (the scale ladder's limiter evidence, #273)
        status.add("cur_busy_s", time.monotonic() - t0)

    def report(stats):
        status.update("feed", stats.as_dict())
        status.beat(feed_conn)
        now = time.monotonic()
        if now - last_log[0] >= 60:
            n = stats.readings - last_log[1]
            print(f"fault detector: {n / (now - last_log[0]):.0f} readings/s from the feed, lag "
                  f"{stats.lag_readings} reading(s) / {stats.lag_s:.1f} s, missed {stats.missed_readings}", flush=True)
            last_log[:] = [now, stats.readings]

    consumer.run(handle, stop=stop, report=report)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-file", default=json_store.data_file("rules.json"))
    parser.add_argument("--webhooks-file", default=json_store.data_file("webhooks.json"))
    parser.add_argument("--his-interval", type=float, default=30.0)
    parser.add_argument("--roster-refresh", type=float, default=300.0,
                        help="seconds between re-resolving brick_class rules' point rosters (new classifications)")
    parser.add_argument("--quality-interval", type=float, default=300.0,
                        help="seconds between quality sweeps (range, spike, unit flags - baler.quality); 0 turns them off")
    args = parser.parse_args()

    tracing.init_tracing("baler-fault-detector")

    fault_conn_main = connect()
    ensure_schema(fault_conn_main)
    heartbeat.ensure_schema(fault_conn_main)
    ts_conn = connect()
    quality.ensure_schema(ts_conn)
    store = RemoteStore()  # same instance backs both Cur-mode roster resolution and the His-mode sweep below

    rule_cache = RuleCache(args.rules_file, store=store, roster_ttl=args.roster_refresh)

    # Faults whose rule is gone can never resolve (#58: rules lost in
    # September left 76 open, masking derivation inputs). Only when the
    # rules file exists - a missing file isn't "every rule was deleted".
    if os.path.exists(args.rules_file):
        known = {r["id"] for r in json_store.load(args.rules_file)}
        closed = close_orphaned_faults(fault_conn_main, known, datetime.now(timezone.utc))
        if closed:
            print(f"fault detector: resolved {closed} open fault(s) whose rule no longer exists")

    open_faults = OpenFaults()
    open_faults.refresh(fault_conn_main)
    status = heartbeat.Status("fault_detector", interval_seconds=args.his_interval, feed={}, his={}, cur_evaluations=0,
                              cur_busy_s=0.0)

    def feed_thread():
        try:
            run_feed(rule_cache, args.webhooks_file, open_faults, status)
        except Exception:  # noqa: BLE001 - exit; the feed offset makes the restart lossless
            traceback.print_exc()
            os._exit(1)

    threading.Thread(target=feed_thread, name="reading-feed", daemon=True).start()

    if args.quality_interval:
        def quality_thread():
            try:
                run_quality(store, args.quality_interval, status)
            except Exception:  # noqa: BLE001 - exit; the watermark and feed offset make the restart lossless
                traceback.print_exc()
                os._exit(1)

        threading.Thread(target=quality_thread, name="quality-sweep", daemon=True).start()

    print(f"fault detector reading the reading feed, His-mode sweep every {args.his_interval}s", flush=True)
    matcher = _Matcher(store)
    starts: collections.deque[float] = collections.deque(maxlen=HIS_GAP_CYCLES + 1)
    over = 0
    while True:
        t0 = time.monotonic()
        starts.append(t0)
        open_faults.refresh(fault_conn_main)
        stats = evaluate_his_rules(ts_conn, fault_conn_main, rule_cache.get(), args.webhooks_file, store=store,
                                   open_faults=open_faults, matcher=matcher)
        cycle = time.monotonic() - t0
        if cycle > args.his_interval:
            over += 1
            print(f"fault detector: His sweep took {cycle:.1f} s, longer than its {args.his_interval:.0f} s interval",
                  flush=True)
        # gap_s_max: the longest start-to-start gap over the last cycles -
        # the interval His rules actually kept (#268), whatever else ran.
        gaps = [b - a for a, b in zip(starts, list(starts)[1:])]
        status.update("his", {**stats, "sweep_s": round(cycle, 3), "interval_s": args.his_interval,
                              "gap_s_max": round(max(gaps), 3) if gaps else None, "over_interval": over})
        status.beat(fault_conn_main)
        time.sleep(max(0.0, args.his_interval - cycle))


# His cycles gap_s_max looks back over: 10 minutes at the default 30 s.
HIS_GAP_CYCLES = 20
# How often the quality thread drains the reading feed for late rows, and
# refreshes its heartbeat figures, between passes.
QUALITY_WATCH_S = 5.0


def run_quality(store, interval: float, status: heartbeat.Status, stop: threading.Event | None = None) -> None:
    """The quality thread (#268): baler.quality's incremental sweep on its
    own connection, so His-mode rules keep their interval while it runs.
    A pass every `interval` seconds (back to back while it's catching
    up), and the late-row watcher in between. Its figures are the
    heartbeat's `quality`, written by the other threads' beats."""
    conn = connect()
    sweeper = quality.Sweeper(conn, store, interval=interval)
    sweeper.start()
    last = -interval
    while stop is None or not stop.is_set():
        if time.monotonic() - last >= interval or sweeper.behind():
            last = time.monotonic()
            sweeper.watch()  # anything late so far is recorded before the pass moves the watermark
            with tracer.start_as_current_span("fault_detector.quality_sweep") as span:
                counts = sweeper.sweep()
                for k, v in counts.items():
                    if isinstance(v, (int, float, str)):
                        span.set_attribute(k, v)
            if counts["sweep_s"] > interval:
                print(f"fault detector: quality sweep took {counts['sweep_s']:.1f} s, longer than its "
                      f"{interval:.0f} s interval ({counts['rows']} rows, watermark {sweeper.lag_s():.0f} s behind)",
                      flush=True)
        sweeper.watch()
        status.update("quality", sweeper.status())
        if stop is not None:
            stop.wait(QUALITY_WATCH_S)
        else:
            time.sleep(QUALITY_WATCH_S)

if __name__ == "__main__":
    main()
