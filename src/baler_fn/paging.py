"""
phable reads that follow Baler's pages (#280).

phable's `HaystackClient.read_all(filter)` sends one `read` and returns
that grid. Baler answers a filter read 1,000 records at a time
(BALER_READ_LIMIT), so on a bigger site `read_all` returns the first page
only, and `Grid.to_pandas()` drops the grid meta (`incomplete`, `after`)
that says so. This follows `after` and returns one phable Grid of every
record:

    from baler_fn import phable_read_all

    grid = phable_read_all(client.phable(), "point")   # or BalerClient.phable_read_all("point")
    df = grid.to_pandas()

phable is imported lazily: baler_fn itself doesn't depend on it.
"""

from __future__ import annotations

from typing import Any


def phable_read_all(client: Any, filter: str, limit: int | None = None) -> Any:
    """Every record matching `filter`, through a phable HaystackClient, as
    one phable Grid. With `limit`, one request for at most that many, as
    phable's own `read_all` (its meta keeps `after` when more follow).
    Columns are the union of the pages' columns, in first-seen order; the
    grid's meta is the last page's, so it has no `after`."""
    import phable

    if limit is not None:
        return client.read_all(filter, limit)
    cols: dict[str, Any] = {}
    rows: list = []
    meta: dict = {"ver": "3.0"}
    after = None
    while True:
        row: dict[str, Any] = {"filter": filter}
        if after is not None:
            row["after"] = after
        grid = client.call("read", phable.Grid.to_grid(row))
        for col in grid.cols:
            cols.setdefault(col.name, col)
        rows += [r for r in grid.rows if r]
        meta = dict(grid.meta)
        nxt = meta.get("after")
        if not isinstance(nxt, phable.Ref):
            return phable.Grid(meta=meta, cols=list(cols.values()) or [phable.GridCol("empty")], rows=rows)
        if after is not None and nxt.val == after.val:
            raise RuntimeError(f"read {filter!r}: the server handed back the same `after` ({nxt.val}) twice")
        after = nxt
