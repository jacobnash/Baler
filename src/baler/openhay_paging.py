"""
openhay's client, following Baler's read pages (#280).

Baler answers a filter `read` a page at a time (BALER_READ_LIMIT, 1000 by
default, #244). A short page carries grid meta `incomplete`, `limit` and
`after`, and the client sends `after` back to read on. Before openhay #9,
openhay's `Client.read` returned `call("read").rows`: it dropped the meta
and never sent `after`, so on a site with more than one page of matches it
handed back the first 1,000 records as if they were all of them.

openhay #9 made `Client.read` follow `after` natively (in its Rust client)
and added `Client.read_page`, and Baler's openhay pin (pyproject.toml and
the Dockerfile's OPENHAY_REF) includes it, so `NATIVE_PAGING` is true and
`Client.read`/`read_page` here are openhay's own. The protocol-level loop
below stays as the fallback for an older openhay, and for anything that
only has openhay's `call(op, grid)`:

    from baler import openhay_paging

    c = openhay_paging.Client("http://localhost:8080/api/demo", bearer=key)
    recs = c.read("point")            # every page; recs.after is None
    page = c.read_page("point", 500)  # one page; page.after continues it

or, with an existing `openhay.Client` (or any `call`-er),
`openhay_paging.read(c, "point")`.

Either way the contract is openhay's: `read(f)` is every match across
pages, `read(f, limit)` at most `limit` of them (following pages until it
has them), and `read_page(f, limit, after)` one page whose `.after` reads
on. Dropping this module: replace `openhay_paging.Client` with
`openhay.Client` and `openhay_paging.read(c, f)` with `c.read(f)`.

Records come back as openhay values (Ref, Number, ...), as from openhay
itself, in a list that carries the paging meta (`baler.haystack_client.
Records`: `after`, `incomplete`, `limit`, `pages`). Needs the `haystack`
extra (`pip install -e ".[haystack]"`).
"""

from __future__ import annotations

from typing import Any

import openhay as oh

from baler.haystack_client import HaystackError, Records

# openhay pages natively from #9 on (its Client has `read_page`, and `read`
# follows `after`); before that, Baler's loop below does it.
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
    """Every record the filter matches, following `after` page by page; with
    `limit`, at most that many (still following pages until it has them, as
    openhay's own `Client.read` does). A server that doesn't page answers in
    one request, as before."""
    out = Records(pages=0)
    after: str | None = None
    while True:
        want = None if limit is None else limit - len(out)
        page = read_page(client, filter, want, after)
        out.extend(page)
        out.pages += 1
        out.limit, out.incomplete = page.limit, page.incomplete
        if page.after is None or (limit is not None and len(out) >= limit):
            if limit is not None and len(out) > limit:  # a server that overshot
                del out[limit:]
                out.after = _after_id(out[-1].get("id")) if out else None
            else:
                out.after = page.after
            return out
        if page.after == after:
            raise HaystackError(f"read {filter!r}: the server handed back the same `after` ({after}) twice")
        if not page:
            raise HaystackError(f"read {filter!r}: the server handed back an empty page with a further `after`")
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
    `read_page`, both returning Records. Drop-in: everything else is
    openhay's. With an openhay that pages natively (`NATIVE_PAGING`, which
    Baler's pin does), both are openhay's own; the loop above is the
    fallback for an older openhay."""

    def read(self, filter: str, limit: int | None = None) -> Records:  # type: ignore[override]
        if NATIVE_PAGING:
            return _records(super().read(filter, limit))
        return read(self, filter, limit)

    def read_page(self, filter: str, limit: int | None = None, after: Any = None) -> Records:
        if NATIVE_PAGING:
            return _records(super().read_page(filter, limit, after))  # type: ignore[misc]
        return read_page(self, filter, limit, after)
