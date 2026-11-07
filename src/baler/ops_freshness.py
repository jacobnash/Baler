"""
Read-only signals for post-deploy verification (Baler#178), served by
ops_api so tools/live-check never needs database or docker access:

  GET /ops/freshness?prefix=fbf/home    ingest rate, and when each point
                                        under a topic prefix last reported
  GET /ops/gateway-errors?since=&until= the gateway's 5xx answers in a window

A point's poll interval isn't stored anywhere Baler can see (it lives in
the edge node's connection record), so it is estimated from the point's own
samples: the mean spacing over the last hour of data it sent. Windrow
publishes on every poll unless a point has a COV policy, so for ordinary
points that is the poll interval; for a COV point it's longer, which only
makes the staleness check more lenient, never noisier.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import psycopg

_PREFIX = re.compile(r"^[A-Za-z0-9_.~-]+(/[A-Za-z0-9_.~-]+)*$")
MAX_POINTS = 5000
# Points quiet for more than the raw window are found from the hourly
# rollup, back this far.
QUIET_LOOKBACK_DAYS = 7

GATEWAY_ERROR_LOG = os.environ.get("BALER_GATEWAY_ERROR_LOG") or "/gateway-logs/5xx.log"
# Only the tail is read: the log holds 5xx lines only (gateway/nginx.conf),
# so this is weeks of deploys, and a runaway file can't make a request slow.
TAIL_BYTES = 4 * 1024 * 1024
MAX_SAMPLES = 20


def check_prefix(prefix: str | None) -> str:
    p = (prefix or "").strip().strip("/")
    if not _PREFIX.match(p) or any(set(seg) == {"."} for seg in p.split("/")):
        raise ValueError("prefix is a topic prefix such as fbf/home (letters, digits, - _ . ~ and /)")
    return p


def _like(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"urn:point:{escaped}/%"


def freshness(conn: psycopg.Connection, prefix: str, scope=None) -> dict[str, Any]:
    """{prefix, generated_at, rows_last_60s, prefix_rows_last_60s, points:
    [{uri, last_ts, age_s, poll_s, samples_last_hour}]}. Every count and
    point is limited to what `scope` (the caller's sites) allows."""
    like = _like(prefix)
    allows = (lambda uri: True) if scope is None or scope.unrestricted else scope.allows

    rate = conn.execute(
        "SELECT point_uri, count(*) FROM point_history WHERE ts > now() - interval '60 seconds' GROUP BY point_uri"
    ).fetchall()
    rows_60 = sum(n for uri, n in rate if allows(uri))
    prefix_60 = sum(n for uri, n in rate if allows(uri) and uri.startswith(f"urn:point:{prefix}/"))

    # One range scan of the last hour (the same shape /ops/status uses for
    # points_seen_last_hour): newest sample, sample count and first sample.
    recent = conn.execute(
        "SELECT point_uri, max(ts), extract(epoch FROM now() - max(ts)), count(*), "
        "extract(epoch FROM max(ts) - min(ts)) "
        "FROM point_history WHERE ts > now() - interval '1 hour' AND point_uri LIKE %s ESCAPE '\\' "
        "GROUP BY point_uri",
        (like,),
    ).fetchall()
    points: dict[str, dict[str, Any]] = {}
    for uri, last, age, n, span in recent:
        if not allows(uri):
            continue
        points[uri] = {"uri": uri, "last_ts": last.astimezone(timezone.utc).isoformat(), "age_s": round(float(age), 1),
                       "poll_s": round(float(span) / (n - 1), 1) if n > 1 else None, "samples_last_hour": int(n)}

    # Points that reported in the last week but not in the last hour: these
    # are the ones that went quiet. The rollup knows them without a week of
    # raw rows; each one's exact last sample is then a primary-key lookup.
    try:
        quiet = conn.execute(
            "SELECT point_uri, sum(vcount), extract(epoch FROM max(bucket) - min(bucket)) + 3600 "
            "FROM point_history_1h WHERE bucket > now() - make_interval(days => %s) AND point_uri LIKE %s ESCAPE '\\' "
            "GROUP BY point_uri",
            (QUIET_LOOKBACK_DAYS, like),
        ).fetchall()
    except psycopg.Error:
        conn.rollback()  # no TimescaleDB rollups here: only the last hour is known
        quiet = []
    for uri, n, span in quiet:
        if uri in points or not allows(uri) or len(points) >= MAX_POINTS:
            continue
        row = conn.execute(
            "SELECT max(ts), extract(epoch FROM now() - max(ts)) FROM point_history "
            "WHERE point_uri = %s AND ts > now() - make_interval(days => %s)",
            (uri, QUIET_LOOKBACK_DAYS + 1),
        ).fetchone()
        if not row or row[0] is None:
            continue
        points[uri] = {"uri": uri, "last_ts": row[0].astimezone(timezone.utc).isoformat(), "age_s": round(float(row[1]), 1),
                       "poll_s": round(float(span) / float(n), 1) if n else None, "samples_last_hour": 0}

    ordered = sorted(points.values(), key=lambda p: p["uri"])[:MAX_POINTS]
    return {
        "prefix": prefix,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows_last_60s": int(rows_60),
        "prefix_rows_last_60s": int(prefix_60),
        "poll_s_source": "observed mean spacing over the last hour of samples",
        "points": ordered,
    }


# --------------------------------------------------------------- gateway 5xx

_ID_SEGMENT = re.compile(r"^([0-9a-f-]{8,}|\d+)$", re.IGNORECASE)


def fold_route(uri: str) -> str:
    """/cx/projects/9ddf…/report -> /cx/projects/:id/report (live-check's
    findings.route does the same), so the answer names routes, not records."""
    path = uri.split("?", 1)[0]
    return "/".join(":id" if _ID_SEGMENT.match(s) else s for s in path.split("/"))


def parse_time(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 time, e.g. 2026-11-07T22:00:00Z") from exc
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _tail_lines(path: str, limit: int = TAIL_BYTES) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        data = f.read()
    lines = data.decode("utf-8", "replace").split("\n")
    if size > limit:
        lines = lines[1:]  # the first line is cut
    return [ln for ln in lines if ln.strip()]


def gateway_errors(since: datetime | None, until: datetime | None, path: str | None = None) -> dict[str, Any]:
    """The gateway's 5xx answers between since and until (both optional),
    from the log gateway/nginx.conf writes them to. `available` is false
    when there is no such log (gateway not set up for it yet): the caller
    then can't tell, which is not the same as zero."""
    out: dict[str, Any] = {
        "available": False, "since": since.isoformat() if since else None, "until": until.isoformat() if until else None,
        "count": 0, "by_status": {}, "by_route": [], "first": None, "last": None, "samples": [],
    }
    path = path or GATEWAY_ERROR_LOG
    try:
        lines = _tail_lines(path)
    except FileNotFoundError:
        # nginx creates the file when it loads a config that names it, so
        # missing means this gateway isn't writing one (older config).
        out["detail"] = f"no gateway 5xx log at {path}: is the gateway-logs volume mounted and nginx.conf current?"
        return out
    out["available"] = True
    routes: dict[tuple[str, int], int] = {}
    for line in lines:
        try:
            rec = json.loads(line)
            ts = parse_time(rec.get("ts"), "ts")
            status = int(rec.get("status"))
        except (ValueError, TypeError, AttributeError):
            continue
        if ts is None or (since and ts < since) or (until and ts > until):
            continue
        route = f"{rec.get('method', '?')} {fold_route(str(rec.get('uri', '')))}"
        out["count"] += 1
        out["by_status"][str(status)] = out["by_status"].get(str(status), 0) + 1
        routes[(route, status)] = routes.get((route, status), 0) + 1
        iso = ts.astimezone(timezone.utc).isoformat()
        out["first"] = out["first"] or iso
        out["last"] = iso
        if len(out["samples"]) < MAX_SAMPLES:
            out["samples"].append({"ts": iso, "status": status, "route": route,
                                   "upstream_status": rec.get("upstream_status") or None})
    out["by_route"] = [{"route": r, "status": s, "count": n}
                       for (r, s), n in sorted(routes.items(), key=lambda kv: (-kv[1], kv[0]))]
    return out
