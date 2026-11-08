"""Cx inventory (#37): scoped inside the SPARQL - to equipment *and* to
each point - and read Brick-first, Haystack/Xeto-second. Checked on
rdflib and, when pyoxigraph is installed, on Oxigraph too (the production
store), the same way test_scope_in_query does."""

from __future__ import annotations

import cx_fixtures as fx
import pytest
from rdflib import URIRef

from baler.commissioning import inventory
from baler.scope import Scope
from baler.store import BRICK


class _Row:
    def __init__(self, solution, names):
        for n in names:
            v = solution[n]
            if v is not None:
                setattr(self, n, v.value)


class _OxStore:
    """Just enough of the Store surface for inventory.build on Oxigraph."""

    def __init__(self, rdflib_store):
        import pyoxigraph as ox

        self._ox = ox.Store()
        self._ox.load(rdflib_store.graph.serialize(format="nt").encode(), ox.RdfFormat.N_TRIPLES)

    def query(self, sparql):
        res = self._ox.query(sparql)
        names = [v.value for v in res.variables]
        return [_Row(s, names) for s in res]


@pytest.fixture(scope="module", params=["rdflib", "oxigraph"])
def store(request):
    s = fx.store()
    if request.param == "oxigraph":
        pytest.importorskip("pyoxigraph")
        return _OxStore(s)
    return s


def _ids(rows):
    return [r["id"] for r in rows]


def test_unrestricted_sees_both_orgs(store):
    assert _ids(inventory.build(store, [Scope.everything()])) == sorted([fx.AHU, fx.VAV, fx.MTR, fx.AHU_B])


def test_site_scope_hides_the_other_org(store):
    assert _ids(inventory.build(store, [Scope(("fbf/a",))])) == [fx.AHU, fx.MTR, fx.VAV]
    assert _ids(inventory.build(store, [Scope(("fbf/b",))])) == [fx.AHU_B]


def test_all_scopes_must_allow(store):
    # caller sees fbf/a only, project site is fbf/b: nothing
    assert inventory.build(store, [Scope(("fbf/a",)), Scope(("fbf/b",))]) == []
    # an org with no prefixes is kept out of what another org claimed
    assert _ids(inventory.build(store, [Scope(None, ("fbf/b",))])) == [fx.AHU, fx.MTR, fx.VAV]


def test_points_are_scoped_too():
    s = fx.store()
    # a point of B's linked onto A's AHU must not leak into A's inventory
    s.graph.add((URIRef(fx.AHU), BRICK.hasPoint, URIRef(fx.pt(fx.AHU_B, "SAT"))))
    ahu = next(e for e in inventory.build(s, [Scope(("fbf/a",))]) if e["id"] == fx.AHU)
    assert fx.pt(fx.AHU_B, "SAT") not in [p["id"] for p in ahu["points"]]


def test_rows_read_brick_then_haystack(store):
    rows = {e["id"]: e for e in inventory.build(store, [Scope(("fbf/a",))])}
    ahu, vav = rows[fx.AHU], rows[fx.VAV]
    assert ahu["brickClass"] == "AHU" and ahu["spec"] == "ph::Ahu" and "ahu" in ahu["tags"]
    pts = {p["id"]: p for p in ahu["points"]}
    assert pts[fx.pt(fx.AHU, "SAT")]["brickClass"] == "Discharge_Air_Temperature_Sensor"
    assert pts[fx.pt(fx.AHU, "X")]["brickClass"] is None
    dmp = next(p for p in vav["points"] if p["id"] == fx.pt(fx.VAV, "DMP"))
    assert dmp["brickClass"] is None and {"damper", "cmd"} <= set(dmp["tags"]) and dmp["spec"] == "ph::CmdPoint"
    assert vav["spec"] == "ph::Vav"


def test_site_scope_helper():
    assert inventory.site_scope([]).unrestricted
    assert inventory.site_scope([" fbf/a/ "]).prefixes == ("fbf/a",)


def test_typed_equipment_without_points_is_listed_with_none():
    """_equip_point_pairs joins two scans (Baler#244): equipment found only
    by its type, with no points, must still be a row, as the single
    UNION/OPTIONAL query had it."""
    s = fx.store()
    lonely = URIRef("urn:equip:fbf/a/lonely-fan")
    s.add_entity(lonely, BRICK.Fan)
    rows = {r["id"]: r for r in inventory.build(s, [Scope(("fbf/a",))])}
    assert rows[str(lonely)]["points"] == []
    assert rows[fx.AHU]["points"]  # and equipment with points keeps them
