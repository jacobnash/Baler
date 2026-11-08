"""
Per-(derivation, target) failure isolation - same shape as faults.py's
webhook_health, keyed differently. A derivation's fn returning cleanly, even
with a value that's semantically wrong, is a success here: that's the
derivation author's data-quality problem, not something this module judges.
Only an uncaught exception anywhere in evaluating one target - the sandboxed
fn call, a read_range/write failure, a malformed SPARQL row - counts as a
failure. Past disable_threshold consecutive failures, that one
(derivation_id, target_uri) pair is skipped by derivation_engine.py until a
human clears it via enable_target().
"""

import psycopg

from baler import schema
from baler.schemas import TargetHealth

# Schema (derivation_target_health): baler/migrations/, from 0001 (#194).
# Change it with a new migration there, not here.


def ensure_schema(conn: psycopg.Connection) -> None:
    schema.ensure(conn)


def record_target_failure(conn: psycopg.Connection, derivation_id: str, target_uri: str, error: str, disable_threshold: int = 5) -> bool:
    """Returns True if this failure just crossed the threshold and disabled
    the target - no auto re-enable; a human re-enables via enable_target()."""
    row = conn.execute(
        """
        INSERT INTO derivation_target_health (derivation_id, target_uri, consecutive_failures, last_error)
        VALUES (%s, %s, 1, %s)
        ON CONFLICT (derivation_id, target_uri) DO UPDATE SET
            consecutive_failures = derivation_target_health.consecutive_failures + 1,
            last_error = EXCLUDED.last_error
        RETURNING consecutive_failures
        """,
        (derivation_id, target_uri, error),
    ).fetchone()
    failures = row[0]  # type: ignore[index]  # INSERT...ON CONFLICT DO UPDATE...RETURNING always returns exactly one row; mypy/psycopg's typing can't express that SQL guarantee
    if failures >= disable_threshold:
        conn.execute(
            "UPDATE derivation_target_health SET disabled = TRUE WHERE derivation_id = %s AND target_uri = %s",
            (derivation_id, target_uri),
        )
        return True
    return False


def record_target_success(conn: psycopg.Connection, derivation_id: str, target_uri: str) -> None:
    conn.execute(
        """
        INSERT INTO derivation_target_health (derivation_id, target_uri, consecutive_failures)
        VALUES (%s, %s, 0)
        ON CONFLICT (derivation_id, target_uri) DO UPDATE SET consecutive_failures = 0
        """,
        (derivation_id, target_uri),
    )


def is_target_disabled(conn: psycopg.Connection, derivation_id: str, target_uri: str) -> bool:
    row = conn.execute(
        "SELECT disabled FROM derivation_target_health WHERE derivation_id = %s AND target_uri = %s",
        (derivation_id, target_uri),
    ).fetchone()
    return bool(row[0]) if row else False


def enable_target(conn: psycopg.Connection, derivation_id: str, target_uri: str) -> TargetHealth | None:
    """None if this (derivation_id, target_uri) has no recorded health row
    at all (never failed or succeeded yet, or a typo'd target) - a plain
    UPDATE...WHERE, not INSERT...ON CONFLICT, so unlike open_fault/
    ack_fault/snooze_fault this one genuinely can match zero rows."""
    row = conn.execute(
        """
        UPDATE derivation_target_health SET disabled = FALSE, consecutive_failures = 0
        WHERE derivation_id = %s AND target_uri = %s
        RETURNING target_uri, consecutive_failures, disabled, last_error
        """,
        (derivation_id, target_uri),
    ).fetchone()
    if row is None:
        return None
    return {"target_uri": row[0], "consecutive_failures": row[1], "disabled": row[2], "last_error": row[3]}


def list_targets(conn: psycopg.Connection, derivation_id: str) -> list[TargetHealth]:
    rows = conn.execute(
        "SELECT target_uri, consecutive_failures, disabled, last_error FROM derivation_target_health WHERE derivation_id = %s ORDER BY target_uri",
        (derivation_id,),
    ).fetchall()
    return [
        {"target_uri": r[0], "consecutive_failures": r[1], "disabled": r[2], "last_error": r[3]}
        for r in rows
    ]
