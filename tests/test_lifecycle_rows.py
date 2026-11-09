"""lifecycle.rows' in-memory twin and the router, with no services (the
Postgres twin is exercised by each feature's integration path in CI)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from baler.lifecycle import routes, rows

rows.register("t_core", ("id", "org_id", "name", "meta", "created_at"), ("meta",))


def test_insert_find_update_delete():
    r = rows.MemoryRows()
    r.insert("t_core", {"id": "b", "org_id": "o", "name": "two", "created_at": 2})
    r.insert("t_core", {"id": "a", "org_id": "o", "name": "one", "meta": {"k": 1}, "created_at": 1})
    r.insert("t_core", {"id": "c", "org_id": "x", "name": "other", "created_at": 3})
    assert [x["id"] for x in r.find("t_core", org_id="o")] == ["a", "b"]
    assert [x["id"] for x in r.find("t_core", org_id=["o", "x"], order="name")] == ["a", "c", "b"]
    got = r.get("t_core", "a")
    got["meta"]["k"] = 99  # a copy: the store doesn't change
    assert r.get("t_core", "a")["meta"] == {"k": 1}
    assert r.update("t_core", "a", {"name": "uno", "nope": 1})["name"] == "uno"
    assert r.update("t_core", "zz", {"name": "x"}) is None
    assert r.delete("t_core", org_id="x") == 1
    with pytest.raises(ValueError):
        r.insert("t_core", {"id": "a"})
    with pytest.raises(ValueError):
        r.find("t_core", bogus=1)
    with pytest.raises(ValueError):
        r.delete("t_core")


def test_public_and_audit():
    out = rows.public({"d": date(2026, 1, 2), "t": datetime(2026, 1, 2, tzinfo=timezone.utc), "n": Decimal("1.5")})
    assert out == {"d": "2026-01-02", "t": "2026-01-02T00:00:00+00:00", "n": 1.5}
    r = rows.MemoryRows()
    r.audit("urn:baler:asset:1", "asset.create", {"sub": "u1", "email": "a@b"}, {"x": 1})
    r.audit("urn:baler:asset:2", "asset.create", {"sub": "u1"}, {})
    log = r.audit_log("urn:baler:asset:1")
    assert len(log) == 1 and log[0]["who"] == "a@b" and log[0]["action"] == "asset.create"


def test_dispatch_unknown_route_is_404():
    req = routes.Request(method="GET", path="/no/such/lifecycle/route", rows=rows.MemoryRows(), org_id="o")
    assert routes.dispatch(req)[0] == 404


def test_field_helpers():
    assert routes.when("2026-03-01", "d") == datetime(2026, 3, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        routes.when("March", "d")
    assert routes.number({"n": "2.5"}, "n") == 2.5
    with pytest.raises(TypeError):
        routes.number({"n": True}, "n")
    with pytest.raises(ValueError):
        routes.need({}, "x")
    with pytest.raises(ValueError):
        routes.text({"t": "x" * 10}, "t", limit=5)
