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

SCHEMA = """
CREATE TABLE IF NOT EXISTS point_write_levels (
    point_uri  TEXT NOT NULL,
    level      SMALLINT NOT NULL CHECK (level BETWEEN 1 AND 16),
    value_json TEXT NOT NULL,
    who        TEXT NOT NULL,
    user_sub   TEXT,
    written_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (point_uri, level)
);
"""

LEVEL_DIS = {1: "1 (emergency)", 8: "8 (manual)", 16: "16 (default)"}
COMMAND_TTL_SECONDS = 30.0
RESULT_TIMEOUT_SECONDS = 10.0


class CommandError(RuntimeError):
    """The site refused the write, or never answered."""


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA)


def record_level(conn, point_uri: str, level: int, value, who: str, user_sub: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM point_write_levels WHERE point_uri = %s AND level = %s", (point_uri, level))
        return
    conn.execute(
        """
        INSERT INTO point_write_levels (point_uri, level, value_json, who, user_sub, written_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (point_uri, level) DO UPDATE SET
            value_json = EXCLUDED.value_json, who = EXCLUDED.who,
            user_sub = EXCLUDED.user_sub, written_at = EXCLUDED.written_at
        """,
        (point_uri, level, json.dumps(value), who, user_sub, datetime.now(timezone.utc)),
    )


def levels(conn, point_uri: str) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT level, value_json, who, written_at FROM point_write_levels WHERE point_uri = %s", (point_uri,)
    ).fetchall()
    return {int(lvl): {"val": json.loads(v), "who": who, "ts": ts} for lvl, v, who, ts in rows}


class Commander:
    """Sends point_write commands over MQTT and waits for each result.

    One client per process, connected lazily on first use and subscribed to
    every site's result topic; results are matched to callers by command id,
    so concurrent writes from different request threads don't mix."""

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

    def _on_message(self, client, userdata, msg) -> None:
        try:
            outcome = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        waiter = self._pending.get(str(outcome.get("id")))
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
                    client.loop_start()
                else:
                    client = self._factory(self._on_message)
                self._client = client
            return self._client

    def point_write(self, site: str, instance: str, topic: str, value, priority: int) -> dict:
        client = self._ensure_client()
        command_id = uuid.uuid4().hex
        event = threading.Event()
        outcome: dict = {}
        self._pending[command_id] = (event, outcome)
        try:
            client.publish(
                f"cmd/{site}/{instance}/point-write",
                json.dumps({
                    "action": "point_write", "id": command_id, "topic": topic,
                    "value": value, "priority": priority, "expires_at": time.time() + self.ttl,
                }),
                qos=1,
            )
            if not event.wait(self.timeout):
                raise CommandError(
                    f"site {site}/{instance} didn't confirm within {self.timeout:.0f}s; if it is offline the "
                    f"command expires after {self.ttl:.0f}s and is not applied"
                )
        finally:
            self._pending.pop(command_id, None)
        if not outcome.get("ok"):
            raise CommandError(f"site refused the write: {outcome.get('error', 'unknown error')}")
        return outcome.get("result") or {}
