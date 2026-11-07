"""
The one seam between commissioning logic and where point history lives
(the idea PR #5 got right). Checks only ever need "latest sample per
point" and "samples over a window", so that is the whole interface:
PostgresHistory reads point_history through timeseries' shared batch
helpers (no private copies), InMemoryHistory makes every check
unit-testable with hand-built series.

`latest` is the newest sample that carries a value: a bridge that
publishes null while it can't reach the device (Baler#83/#85) must not
make a dead point look like it is reporting. `latest_raw` is the newest
row of any kind, so the existence check can tell "silent" from "answering
with empty readings". Both take `before` (latest as of a past moment).

`window` reads at most `per_point_limit` samples per point, oldest
first. A caller that would otherwise judge a silent prefix of a long
window passes a set as `truncated` and gets the ids of every point that
had more (Baler#88).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from baler import timeseries

Sample = tuple[datetime, object]

# How far back `latest` looks for a sample with a value. Unbounded, a point
# with nothing but empty rows would walk its whole history on every check;
# a real value older than this still shows up through latest_raw.
VALUE_LOOKBACK = timedelta(days=31)


class History(Protocol):
    def latest(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]: ...

    def latest_raw(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]: ...

    def window(self, point_uris: list[str], start: datetime, end: datetime,
               truncated: set[str] | None = None) -> dict[str, list[Sample]]: ...


class PostgresHistory:
    def __init__(self, conn, per_point_limit: int = 5000):
        self.conn = conn
        self.per_point_limit = per_point_limit

    def latest(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]:
        since = (before or datetime.now().astimezone()) - VALUE_LOOKBACK
        return timeseries.read_latest_many(self.conn, list(point_uris), before=before, since=since, with_value=True)

    def latest_raw(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]:
        return timeseries.read_latest_many(self.conn, list(point_uris), before=before)

    def window(self, point_uris: list[str], start: datetime, end: datetime,
               truncated: set[str] | None = None) -> dict[str, list[Sample]]:
        raw = timeseries.read_range_many(self.conn, list(point_uris), start, end, self.per_point_limit)
        if truncated is not None:
            truncated.update(uri for uri, (_rows, cut) in raw.items() if cut)
        return {uri: rows for uri, (rows, _cut) in raw.items()}


class InMemoryHistory:
    """`per_point_limit` mirrors PostgresHistory's truncation (oldest
    first) so tests can exercise it; None means unlimited."""

    def __init__(self, series: dict[str, list[Sample]] | None = None, per_point_limit: int | None = None):
        self.series: dict[str, list[Sample]] = {k: sorted(v, key=lambda s: s[0]) for k, v in (series or {}).items()}
        self.per_point_limit = per_point_limit

    def add(self, point_uri: str, ts: datetime, value) -> None:
        self.series.setdefault(point_uri, []).append((ts, value))
        self.series[point_uri].sort(key=lambda s: s[0])

    def _upto(self, uri: str, before: datetime | None) -> list[Sample]:
        return [s for s in self.series.get(uri, []) if before is None or s[0] <= before]

    def latest(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]:
        out = {}
        for u in point_uris:
            rows = self._upto(u, before)
            floor = (before or (rows[-1][0] if rows else None))
            valued = [s for s in rows if s[1] is not None and (floor is None or s[0] >= floor - VALUE_LOOKBACK)]
            if valued:
                out[u] = valued[-1]
        return out

    def latest_raw(self, point_uris: list[str], before: datetime | None = None) -> dict[str, Sample]:
        out = {}
        for u in point_uris:
            rows = self._upto(u, before)
            if rows:
                out[u] = rows[-1]
        return out

    def window(self, point_uris: list[str], start: datetime, end: datetime,
               truncated: set[str] | None = None) -> dict[str, list[Sample]]:
        out = {}
        for u in point_uris:
            rows = [(t, v) for t, v in self.series.get(u, []) if start <= t < end]
            if self.per_point_limit is not None and len(rows) > self.per_point_limit:
                rows = rows[:self.per_point_limit]
                if truncated is not None:
                    truncated.add(u)
            out[u] = rows
        return out
