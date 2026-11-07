"""
Haystack pointWrite for Baler: the 16-level priority array we keep, and
the command that carries a write to the Windrow instance owning the point.

Routing: Windrow advertises `windrowSite`/`windrowInstance` on each
connection's equipment record (its `{prefix}/equip/tags` message). A write
to a point goes to `cmd/<site>/<instance>/point-write` - a topic the broker
ACL scopes to that one site - as Windrow's `point_write` action, and we wait
for the matching `{"id": ...}` on `cmd/<site>/<instance>/result`. So a
successful pointWrite means the controller accepted the BACnet
WriteProperty, not merely that a message was queued.

Every command carries `expires_at`: if the site is offline, the broker
queues it (persistent session), and Windrow refuses it once that time has
passed rather than applying a stale setpoint later.

The array here is Baler's record of what *it* commanded (value, who, when),
which is what Haystack's pointWrite read returns. The controller's own
array can hold other writers' levels too.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from baler import ddl

SCHEMA = """
CREATE TABLE IF NOT EXISTS point_write_levels (
    point_uri  TEXT NOT NULL,
    level      SMALLINT NOT NULL,
    value_json TEXT NOT NULL,
    who        TEXT NOT NULL,
    user_sub   TEXT,
    written_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (point_uri, level)
);
-- Level 17 is Haystack's default (BACnet relinquish-default); the original
-- table capped levels at 16. expires_at: when a timed level-8 override ends.
ALTER TABLE point_write_levels DROP CONSTRAINT IF EXISTS point_write_levels_level_check;
ALTER TABLE point_write_levels ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
"""

# Haystack's level names, as haxall labels them (WriteRec.levelDis).
LEVEL_DIS = {1: "1 (emergency)", 8: "8 (manual)", 17: "def"}
LEVELS = range(1, 18)
COMMAND_TTL_SECONDS = 30.0
RESULT_TIMEOUT_SECONDS = 10.0


class CommandError(RuntimeError):
    """The site refused the write, or never answered."""


def ensure_schema(conn) -> None:
    ddl.run(conn, SCHEMA)


def record_level(conn, point_uri: str, level: int, value, who: str, user_sub: str | None,
                 expires_at: datetime | None = None) -> None:
    if value is None:
        conn.execute("DELETE FROM point_write_levels WHERE point_uri = %s AND level = %s", (point_uri, level))
        return
    conn.execute(
        """
        INSERT INTO point_write_levels (point_uri, level, value_json, who, user_sub, written_at, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (point_uri, level) DO UPDATE SET
            value_json = EXCLUDED.value_json, who = EXCLUDED.who,
            user_sub = EXCLUDED.user_sub, written_at = EXCLUDED.written_at,
            expires_at = EXCLUDED.expires_at
        """,
        (point_uri, level, json.dumps(value), who, user_sub, datetime.now(timezone.utc), expires_at),
    )


def levels(conn, point_uri: str) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT level, value_json, who, written_at, expires_at FROM point_write_levels WHERE point_uri = %s",
        (point_uri,),
    ).fetchall()
    return {int(lvl): {"val": json.loads(v), "who": who, "ts": ts, "expires_at": exp} for lvl, v, who, ts, exp in rows}


def expired(conn, now: datetime) -> list[tuple[str, int, str]]:
    """(point_uri, level, who) of timed overrides whose duration is up."""
    rows = conn.execute(
        "SELECT point_uri, level, who FROM point_write_levels WHERE expires_at IS NOT NULL AND expires_at <= %s",
        (now,),
    ).fetchall()
    return [(u, int(lvl), who) for u, lvl, who in rows]


class Commander:
    """Sends commands to Windrow instances over MQTT and waits for each result.

    One client per process, connected lazily on first use and subscribed to
    every site's result topic; results are matched to callers by command id,
    so concurrent writes from different request threads don't mix. It also
    follows every instance's retained `cmd/<site>/<instance>/status`
    heartbeat (Windrow publishes it, with an offline last will), which is
    how Baler knows which edge nodes exist and whether they're up."""

    def __init__(self, client_factory=None, host: str | None = None, port: int | None = None,
                 timeout: float = RESULT_TIMEOUT_SECONDS, ttl: float = COMMAND_TTL_SECONDS):
        self._factory = client_factory
        self._host = host or os.environ.get("MQTT_HOST", "localhost")
        self._port = port or int(os.environ.get("MQTT_PORT", "1883"))
        self.timeout = timeout
        self.ttl = ttl
        self._client = None
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self._status: dict[tuple[str, str], dict] = {}

    def _on_message(self, client, userdata, msg) -> None:
        try:
            outcome = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
        except (ValueError, UnicodeDecodeError):
            return
        parts = msg.topic.split("/")
        if len(parts) == 4 and parts[3] == "status":
            if not msg.payload:  # a cleared retained status: the node is gone
                self._status.pop((parts[1], parts[2]), None)
            elif isinstance(outcome, dict):
                self._status[(parts[1], parts[2])] = {**outcome, "received_at": time.time()}
            return
        # request_id is the correlation id; older Windrows only echo `id`.
        waiter = self._pending.get(str(outcome.get("request_id"))) or self._pending.get(str(outcome.get("id")))
        if waiter:
            waiter[1].update(outcome)
            waiter[0].set()

    def _ensure_client(self):
        with self._lock:
            if self._client is None:
                if self._factory is None:
                    from baler import mqtt_util

                    client = mqtt_util.make_client()
                    client.on_message = self._on_message
                    client.connect(self._host, self._port)
                    client.subscribe("cmd/+/+/result", qos=1)
                    client.subscribe("cmd/+/+/status", qos=1)
                    client.loop_start()
                else:
                    client = self._factory(self._on_message)
                self._client = client
            return self._client

    def statuses(self, settle: float = 0.5) -> dict[tuple[str, str], dict]:
        """(site, instance) -> its latest status message. Retained statuses
        arrive just after subscribing, so the first call waits `settle`."""
        first = self._client is None
        self._ensure_client()
        if first and settle:
            time.sleep(settle)
        return dict(self._status)

    def command(self, site: str, instance: str, action: str, body: dict | None = None,
                timeout: float | None = None, suffix: str | None = None) -> object:
        """Run one Windrow command-channel action and return its result.
        Raises CommandError if the site refuses it or doesn't answer."""
        client = self._ensure_client()
        command_id = uuid.uuid4().hex
        event = threading.Event()
        outcome: dict = {}
        self._pending[command_id] = (event, outcome)
        wait = self.timeout if timeout is None else timeout
        try:
            client.publish(
                f"cmd/{site}/{instance}/{suffix or action.replace('_', '-')}",
                # A targeted action's `id` names the device/connection, so the
                # correlation id travels as request_id; an action without a
                # target also carries it as `id` for Windrows that predate
                # request_id (point writes on a not-yet-updated site).
                json.dumps({"id": command_id, **(body or {}), "action": action, "request_id": command_id,
                            "expires_at": time.time() + self.ttl}),
                qos=1,
            )
            if not event.wait(wait):
                raise CommandError(
                    f"site {site}/{instance} didn't answer within {wait:.0f}s; if it is offline the "
                    f"command expires after {self.ttl:.0f}s and is not applied"
                )
        finally:
            self._pending.pop(command_id, None)
        if not outcome.get("ok"):
            raise CommandError(f"site refused {action}: {outcome.get('error', 'unknown error')}")
        return outcome.get("result")

    def point_write(self, site: str, instance: str, topic: str, value, priority: int) -> dict:
        try:
            result = self.command(site, instance, "point_write", {"topic": topic, "value": value, "priority": priority},
                                  suffix="point-write")
            return result if isinstance(result, dict) else {}
        except CommandError as exc:
            msg = str(exc).replace("site refused point_write:", "site refused the write:")
            raise CommandError(msg.replace("didn't answer", "didn't confirm")) from None
