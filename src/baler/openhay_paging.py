"""
openhay's client, following Baler's read pages (#280).

Baler answers a filter `read` a page at a time (BALER_READ_LIMIT, 1000 by
default, #244). A short page carries grid meta `incomplete`, `limit` and
`after`, and the client sends `after` back to read on. openhay's
`Client.read` returns `call("read").rows`: it drops the meta and never
sends `after`, so on a site with more than one page of matches it hands
back the first 1,000 records as if they were all of them.

Until openhay follows `after` itself, use this instead:

    from baler import openhay_paging

    c = openhay_paging.Client("http://localhost:8080/api/demo", bearer=key)
    recs = c.read("point")            # every page; recs.after is None
    page = c.read_page("point", 500)  # one page; page.after continues it

or, with an existing `openhay.Client`, `openhay_paging.read(c, "point")`.

Dropping it: openhay is gaining native `after` paging. Once Baler's openhay
pin (pyproject.toml and the Dockerfile's OPENHAY_REF) includes it,
`NATIVE_PAGING` is true and `Client.read`/`read_page` delegate to openhay's
own; then replace `openhay_paging.Client` with `openhay.Client`,
`openhay_paging.read(c, f)` with `c.read(f)`, and delete this module. The
module-level `read`/`read_page` page at the protocol level (`call("read")`),
so they stay correct against any openhay version meanwhile.

Records come back as openhay values (Ref, Number, ...), as from openhay
itself, in a list that carries the paging meta (`baler.haystack_client.
Records`: `after`, `incomplete`, `limit`, `pages`). Needs the `haystack`
extra (`pip install -e ".[haystack]"`).
"""

from __future__ import annotations

from typing import Any

import openhay as oh

from baler.haystack_client import HaystackError, Records

# openhay pages natively once its Client has `read_page` (the upstream
# change that follows `after`); until then, Baler's loop below does it.
NATIVE_PAGING = callable(getattr(oh.Client, "read_page", None))


def _after_id(after: Any) -> str | None:
    if after is None:
        return None
    if isinstance(after, oh.Ref):
        return str(after.id)
    return str(after).lstrip("@")


def read_page(client: Any, filter: str, limit: int | None = None, after: Any = None) -> Records:
    """One page of a filter read through `client` (an openhay.Client, or
    anything with its `call(op, grid)`): at most `limit` records (None: the
    server's page size), after the record id `after` (str, @str or Ref; a
    previous page's `.after`). `.after` on the result is the cursor when
    the server says more follow, else None."""
    row: dict[str, Any] = {"filter": filter}
    if limit is not None:
        row["limit"] = oh.Number(limit)
    after_id = _after_id(after)
    if after_id is not None:
        row["after"] = oh.Ref(after_id)
    return _records(client.call("read", oh.Grid.from_rows([row])))


def read(client: Any, filter: str, limit: int | None = None) -> Records:
    """Every record the filter matches, following `after` page by page; or,
    with `limit`, one page of at most that many (its `.after` continues
    it). A server that doesn't page answers in one request, as before."""
    if limit is not None:
        return read_page(client, filter, limit)
    out = Records(pages=0)
    after: str | None = None
    while True:
        page = read_page(client, filter, None, after)
        out.extend(page)
        out.pages += 1
        if page.after is None:
            out.limit, out.incomplete = page.limit, page.incomplete
            return out
        if page.after == after:
            raise HaystackError(f"read {filter!r}: the server handed back the same `after` ({after}) twice")
        after = page.after


def _records(result: Any) -> Records:
    """openhay's native result (a Grid with meta, or a list) as Records."""
    if isinstance(result, Records):
        return result
    meta = getattr(result, "meta", None) or {}
    after, incomplete, limit = meta.get("after"), meta.get("incomplete"), meta.get("limit")
    return Records(
        [r for r in getattr(result, "rows", result) if r],
        after=str(after.id) if isinstance(after, oh.Ref) else None,
        incomplete=None if incomplete is None else ("incomplete" if incomplete is oh.MARKER else str(incomplete)),
        limit=int(limit.val) if isinstance(limit, oh.Number) else None,
    )


class Client(oh.Client):
    """openhay.Client whose `read` follows Baler's pages (see `read`), plus
    `read_page`. Drop-in: everything else is openhay's. With an openhay
    that pages natively (`NATIVE_PAGING`), both are openhay's own."""

    def read(self, filter: str, limit: int | None = None) -> Records:  # type: ignore[override]
        if NATIVE_PAGING:
            return _records(super().read(filter, limit))
        return read(self, filter, limit)

    def read_page(self, filter: str, limit: int | None = None, after: Any = None) -> Records:
        if NATIVE_PAGING:
            return _records(super().read_page(filter, limit, after))  # type: ignore[misc]
        return read_page(self, filter, limit, after)
