"""
0003 operator alerts and process health (#277).

- alert_state: the alert evaluator's (baler.alerts) open alerts, one row per
  condition key, so a restarted evaluator neither re-sends an alert it
  already sent nor forgets to send the clear, and so /ops/status can list
  what is currently alerting. The evaluator holds the authoritative copy in
  memory (it must still alert while Postgres itself is down) and writes this
  table whenever the database answers; a cleared alert's row is deleted.
- service_runtime: per process name, how often it started (recent_starts,
  the last few start times, for restart-loop detection) and how many
  unhandled exceptions it raised (baler.errors), with the last one's type
  and message. Written best effort by every process that calls
  tracing.init_tracing.

Self-contained: both tables are new and nothing else references them.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 3
NAME = "operator alerts and process health"

ALERT_STATE = """
CREATE TABLE IF NOT EXISTS alert_state (
    key           TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    severity      TEXT NOT NULL,
    title         TEXT NOT NULL,
    detail        TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL,
    opened_at     TIMESTAMPTZ,
    last_seen_at  TIMESTAMPTZ NOT NULL,
    last_sent_at  TIMESTAMPTZ,
    absent_since  TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)"""

SERVICE_RUNTIME = """
CREATE TABLE IF NOT EXISTS service_runtime (
    service        TEXT PRIMARY KEY,
    starts         BIGINT NOT NULL DEFAULT 0,
    recent_starts  TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
    errors         BIGINT NOT NULL DEFAULT 0,
    last_error_at  TIMESTAMPTZ,
    last_error     TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)"""


def plan(conn) -> list[Step]:
    return [
        Step(ALERT_STATE, "alert_state: the alert evaluator's open alerts"),
        Step(SERVICE_RUNTIME, "service_runtime: process starts and unhandled exception counts"),
    ]
