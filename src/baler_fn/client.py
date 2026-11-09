"""
connect(): a Haystack client for Baler, configured from the environment
(#91). It wraps baler.haystack_client.HaystackClient (the same client the
Haystack puller uses) and adds what a notebook wants on top: API-key auth,
DataFrames and hisWrite. For anything beyond that, .phable() hands back a
phable client on the same URL and key (#95's recommended library).

Everything runs in the caller's own Python process. Baler only ever sees
ordinary Haystack HTTP requests, authorized like any other API key.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from baler.haystack_client import HaystackClient, Records

DEFAULT_PROJECT = "demo"


class ConfigError(RuntimeError):
    """connect() is missing a setting. The message says which env var."""


class BalerClient:
    """One Baler Haystack API session, authenticated with an API key."""

    def __init__(self, url: str, api_key: str, project: str = DEFAULT_PROJECT):
        self.url = url.rstrip("/")
        self.project = project
        self.base_url = f"{self.url}/api/{project}"
        self._api_key = api_key
        self.haystack = HaystackClient(self.base_url, token=api_key)

    def __repr__(self) -> str:  # never show the key
        return f"BalerClient({self.base_url!r})"

    def about(self) -> dict:
        """The server's `about` record (productName, serverTime, ...)."""
        return self.haystack.about()

    def read(self, filter: str, limit: int | None = None) -> Records:
        """Records matching a Haystack filter, as dicts. Markers are True,
        refs are their id string, Numbers floats, dateTimes epoch seconds.

        Every match, however many pages the server answers in (Baler pages
        reads at 1,000 records by default, #280). With `limit`, one page of
        at most that many: the result's `.after` is then the cursor for
        read_page() when more follow, and None when the read is complete."""
        return self.haystack.read(filter, limit)

    def read_page(self, filter: str, limit: int | None = None, after: str | None = None) -> Records:
        """One page of a read, starting after a previous page's `.after`.
        `.after` on the result is None on the last page."""
        return self.haystack.read_page(filter, limit, after)

    def read_df(self, filter: str, limit: int | None = None) -> Any:
        """read() as a pandas DataFrame, one row per record. The paging
        meta rides along in `df.attrs` (`after`, `incomplete`)."""
        import pandas as pd

        recs = self.read(filter, limit)
        df = pd.DataFrame(recs)
        df.attrs.update(after=recs.after, incomplete=recs.incomplete)
        return df

    def his_read(self, id: str, range: str = "today") -> list[dict]:
        """History rows `{"ts": epoch_seconds, "val": ...}` for one point.
        `range` is any Haystack range: "today", "yesterday", "2026-11-01",
        "2026-11-01,2026-11-07", ..."""
        return self.haystack.his_read(_ref(id), range)

    def his_read_df(self, id: str, range: str = "today") -> Any:
        """his_read() as a pandas DataFrame indexed by UTC timestamp, with
        one column, `val`."""
        import pandas as pd

        rows = self.his_read(id, range)
        df = pd.DataFrame(rows, columns=["ts", "val"])
        # Epoch floats carry ~100 ns of noise; Haystack itself is ms-precise.
        df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.round("us")
        return df.set_index("ts")

    def his_write(self, id: str, items: Any) -> int:
        """Writes history to one point; returns how many samples were sent.
        `items` is a pandas Series or one-column DataFrame indexed by
        timezone-aware timestamps, or (datetime, value) pairs. Needs an
        API key with the operator role."""
        pairs = list(_pairs(items))
        self.haystack.his_write(_ref(id), pairs)
        return len(pairs)

    def phable(self) -> Any:
        """A phable.HaystackClient on the same URL and API key, for the ops
        and DataFrame helpers phable has (pip install "phable[pandas]").

        phable doesn't follow Baler's read pages: its `read_all` returns
        the first page (1,000 records by default), with grid meta `after`
        set when more follow. Use phable_read_all() (a phable Grid of every
        record) or read()/read_df() here instead."""
        try:
            import phable
        except ImportError as exc:
            raise ImportError('phable is not installed: pip install "phable[pandas,pyarrow]"') from exc
        return phable.HaystackClient(self.base_url, self._api_key)


    def phable_read_all(self, filter: str, limit: int | None = None) -> Any:
        """phable's read_all, following Baler's pages: one phable Grid of
        every match (baler_fn.paging.phable_read_all)."""
        from baler_fn.paging import phable_read_all

        return phable_read_all(self.phable(), filter, limit)


def _ref(id: str) -> str:
    return id if id.startswith("@") else f"@{id}"


def _pairs(items: Any) -> Iterable[tuple[datetime, Any]]:
    if hasattr(items, "columns"):  # DataFrame: its one column
        if len(items.columns) != 1:
            raise ValueError(f"his_write takes a one-column DataFrame, got columns {list(items.columns)}")
        items = items[items.columns[0]]
    if hasattr(items, "index") and hasattr(items, "items") and not isinstance(items, dict):  # Series
        items = [(ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts, v) for ts, v in items.items()]
    for ts, val in items:
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc)
        if hasattr(val, "item"):  # numpy scalar
            val = val.item()
        yield ts, val


def connect(url: str | None = None, api_key: str | None = None, project: str | None = None) -> BalerClient:
    """A BalerClient from arguments, falling back to the environment:

    - `BALER_URL`: Baler's gateway, e.g. http://localhost:8080 (in the
      notebook container: http://gateway)
    - `BALER_API_KEY`: a token from POST /auth/api-keys (viewer to read,
      operator to hisWrite)
    - `BALER_PROJECT`: the /api/<project> segment, default "demo"
    """
    url = url or os.environ.get("BALER_URL", "").strip()
    api_key = api_key or os.environ.get("BALER_API_KEY", "").strip()
    project = project or os.environ.get("BALER_PROJECT", "").strip() or DEFAULT_PROJECT
    if not url:
        raise ConfigError("BALER_URL is not set: Baler's gateway URL, e.g. http://localhost:8080")
    if not api_key:
        raise ConfigError("BALER_API_KEY is not set: create one with POST /auth/api-keys "
                          "(see the Notebooks page in the docs)")
    return BalerClient(url, api_key, project)
