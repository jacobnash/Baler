"""
The fault-detection daemon: a third, independent MQTT subscriber (see the
plan - no hook into ingest.py, no coupling to the ingest hot path this
session already spent real effort speeding up) for Cur-mode rules, plus a
periodic sweep of point_history for His-mode rules. Every rule transition
(fault opens or resolves) fans out to matching webhooks off the calling
thread, in its own daemon thread, so one slow/dead URL never stalls fault
evaluation for every other point behind it.

The only code shared with mqtt_listener.py is ingest.topic_to_point_uri -
identity must match exactly, that's a correctness requirement, not a
style choice. on_message's envelope parsing is deliberately duplicated
(same shape as connection_manager.py's _poll_point vs bridge.py's
poll_forever precedent) - after parsing, one writes, one evaluates.
"""

import argparse
import fnmatch
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from baler import json_store, mqtt_util, tracing
from baler.faults import (
    close_fault,
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
    per message, not a per-message disk read let alone an Oxigraph query.
    Also resolves each brick_class Cur-mode rule's point roster on the same
    reload trigger (one SPARQL query per such rule per rules.json change,
    not per message) - store=None (the default) just means no brick_class
    rule can ever match, same as before this existed."""

    def __init__(self, path: str, store=None):
        self.path = path
        self.store = store
        self._mtime: float | None = None
        self._rules: list[dict] = []
        self._brick_roster: dict[str, set[str]] = {}

    def get(self) -> list[dict]:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            self._rules = []
            self._mtime = None
            self._brick_roster = {}
            return self._rules
        if mtime != self._mtime:
            self._rules = json_store.load(self.path)
            self._mtime = mtime
            self._brick_roster = self._build_brick_roster()
        return self._rules

    def _build_brick_roster(self) -> dict[str, set[str]]:
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


def evaluate_range_rule(rule: dict, value) -> bool:
    """True means faulted. Non-numeric values never satisfy a range rule -
    reported as not-faulted, not an error; a read failure (value is None
    or a string) is a different concern from a real value being out of
    bounds."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return value < rule["min"] or value > rule["max"]


def handle_cur_reading(fault_conn, rule_cache: RuleCache, webhooks_path: str, topic: str, value) -> None:
    point_uri = str(topic_to_point_uri(topic))
    now = datetime.now(timezone.utc)

    for rule in rule_cache.get():
        if rule.get("mode") != "cur" or rule.get("type") != "range":
            continue
        applies_to = rule["applies_to"]
        if "brick_class" in applies_to:
            if rule["id"] not in rule_cache.rule_ids_for_point(point_uri):
                continue
        elif not fnmatch.fnmatch(topic, applies_to["topic_glob"]):
            continue

        with tracer.start_as_current_span("fault_detector.evaluate_cur_rule") as span:
            span.set_attribute("rule_id", rule["id"])
            span.set_attribute("point_uri", point_uri)
            faulted = evaluate_range_rule(rule, value)
            span.set_attribute("faulted", faulted)

            if faulted:
                fault_id = open_fault(
                    fault_conn, rule["id"], point_uri, now,
                    detail={"value": value, "min": rule["min"], "max": rule["max"]},
                    severity=rule.get("severity", "warning"),
                )
                if fault_id is not None:
                    _fire_webhooks(webhooks_path, "fault.opened", rule["id"], point_uri, "open", {"value": value})
            else:
                fault_id = close_fault(fault_conn, rule["id"], point_uri, now)
                if fault_id is not None:
                    _fire_webhooks(webhooks_path, "fault.resolved", rule["id"], point_uri, "resolved", {"value": value})


def make_on_message(fault_conn, rule_cache: RuleCache, webhooks_path: str):
    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        handle_cur_reading(fault_conn, rule_cache, webhooks_path, msg.topic, payload.get("value"))

    return on_message


def _evaluate_stuck(ts_conn, point_uri: str, stuck_seconds: float, now: datetime) -> bool:
    window_start = now - timedelta(seconds=stuck_seconds)
    n, distinct_values = ts_conn.execute(
        """
        SELECT COUNT(*), COUNT(DISTINCT COALESCE(value::text, value_text, value_bool::text))
        FROM point_history WHERE point_uri = %s AND ts >= %s
        """,
        (point_uri, window_start),
    ).fetchone()
    return n > 0 and distinct_values == 1


def _evaluate_stale(ts_conn, point_uri: str, max_age_seconds: float, now: datetime) -> bool:
    (max_ts,) = ts_conn.execute("SELECT MAX(ts) FROM point_history WHERE point_uri = %s", (point_uri,)).fetchone()
    if max_ts is None:
        return False
    return (now - max_ts).total_seconds() >= max_age_seconds


def evaluate_his_rules(
    ts_conn, fault_conn, rules: list[dict], webhooks_path: str, now: datetime | None = None, store=None
) -> None:
    """Importable/callable synchronously - tests call this directly
    instead of waiting on a real sleep loop. Each His-mode rule's roster
    is just the distinct point_uris in point_history matching its
    topic_glob (or, for a brick_class rule, intersected with that Brick
    class's members - a point classified into the class but that's never
    reported has no history to evaluate) - the time-series table doubles
    as its own roster, no separate roster table needed. store=None means
    no brick_class rule can ever match (same degrade as RuleCache)."""
    now = now or datetime.now(timezone.utc)
    reported_uris = None  # fetched at most once, only if a brick_class rule needs it

    for rule in rules:
        if rule.get("mode") != "his":
            continue

        applies_to = rule["applies_to"]
        if "brick_class" in applies_to:
            if store is None:
                continue
            if reported_uris is None:
                reported_uris = {row[0] for row in ts_conn.execute("SELECT DISTINCT point_uri FROM point_history").fetchall()}
            candidate_uris = list(reported_uris & _brick_class_roster(store, applies_to["brick_class"]))
        else:
            point_glob = f"urn:point:{applies_to['topic_glob']}"
            candidate_uris = [
                row[0]
                for row in ts_conn.execute("SELECT DISTINCT point_uri FROM point_history").fetchall()
                if fnmatch.fnmatch(row[0], point_glob)
            ]

        for point_uri in candidate_uris:
            with tracer.start_as_current_span("fault_detector.evaluate_his_rule") as span:
                span.set_attribute("rule_id", rule["id"])
                span.set_attribute("point_uri", point_uri)

                if rule["type"] == "stuck":
                    faulted = _evaluate_stuck(ts_conn, point_uri, rule["stuck_seconds"], now)
                elif rule["type"] == "stale":
                    faulted = _evaluate_stale(ts_conn, point_uri, rule["max_age_seconds"], now)
                else:
                    continue
                span.set_attribute("faulted", faulted)

                if faulted:
                    fault_id = open_fault(fault_conn, rule["id"], point_uri, now, severity=rule.get("severity", "warning"))
                    if fault_id is not None:
                        _fire_webhooks(webhooks_path, "fault.opened", rule["id"], point_uri, "open", None)
                else:
                    fault_id = close_fault(fault_conn, rule["id"], point_uri, now)
                    if fault_id is not None:
                        _fire_webhooks(webhooks_path, "fault.resolved", rule["id"], point_uri, "resolved", None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--topic-pattern", default="fbf/#")
    parser.add_argument("--rules-file", default=json_store.data_file("rules.json"))
    parser.add_argument("--webhooks-file", default=json_store.data_file("webhooks.json"))
    parser.add_argument("--his-interval", type=float, default=30.0)
    args = parser.parse_args()

    tracing.init_tracing("baler-fault-detector")

    # Separate connections for the MQTT background thread (Cur-mode) and
    # the main thread (His-mode sweep) - psycopg connections aren't safe
    # for concurrent use from multiple threads, same reasoning as
    # connection_manager.py's one-session-per-worker rule on the Windrow side.
    fault_conn_cur = connect()
    ensure_schema(fault_conn_cur)
    fault_conn_his = connect()
    ts_conn = connect()
    store = RemoteStore()  # same instance backs both Cur-mode roster resolution and the His-mode sweep below

    rule_cache = RuleCache(args.rules_file, store=store)

    client = mqtt_util.make_client()
    client.on_message = make_on_message(fault_conn_cur, rule_cache, args.webhooks_file)
    client.connect(args.mqtt_host, args.mqtt_port)
    client.subscribe(args.topic_pattern)
    client.loop_start()  # background thread - main thread stays free for the His-mode sweep

    print(f"fault detector listening on {args.topic_pattern}, His-mode sweep every {args.his_interval}s")
    try:
        while True:
            evaluate_his_rules(ts_conn, fault_conn_his, rule_cache.get(), args.webhooks_file, store=store)
            time.sleep(args.his_interval)
    finally:
        client.loop_stop()


if __name__ == "__main__":
    main()
