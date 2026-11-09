"""Deferred maintenance, service-life references and FCI (#200), no services."""

from datetime import datetime, timezone

import pytest

from baler.lifecycle import routes, rows

POS1, POS2 = "urn:equip:fbf/hosp/chw-p1", "urn:equip:fbf/hosp/chw-p2"


class Client:
    def __init__(self, allows=lambda _u: True):
        self.rows, self.allows = rows.MemoryRows(), allows
        self.now = datetime(2036, 6, 1, tzinfo=timezone.utc)

    def __call__(self, method, path, body=None, **query):
        req = routes.Request(method=method, path=path, rows=self.rows, org_id="org1", body=body, now=self.now,
                             query={k: str(v) for k, v in query.items()}, who={"sub": "u1"}, allows=self.allows)
        return routes.dispatch(req)

    def ok(self, *a, **kw):
        st, out = self(*a, **kw)
        assert st in (200, 201), out
        return out


@pytest.fixture
def c():
    c = Client()
    for pos, at in ((POS1, "2016-05-01"), (POS2, "2030-05-01")):
        a = c.ok("POST", "/assets", {"asset_type": "pump", "tag": pos[-6:]})
        c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": pos, "at": at})
    return c


def _ids(c):
    return {a["position_uri"]: a["id"] for a in c.ok("GET", "/assets")}


def test_service_life_is_entered_with_its_reference(c):
    st, out = c("POST", "/service-life", {"asset_type": "pump", "median_years": 20, "source_title": "x"})
    assert st == 400 and "retrieved_on" in out["error"]
    ref = c.ok("POST", "/service-life", {"asset_type": "Pump", "median_years": 20,
                                         "source_title": "ASHRAE service-life database",
                                         "source_url": "https://costdatabase.ashrae.org/service_life.asp",
                                         "retrieved_on": "2036-05-01"})
    assert ref["median_years"] == 20
    # one reference per type: setting it again replaces it (audited)
    c.ok("POST", "/service-life", {"asset_type": "pump", "median_years": 25, "source_title": "owner standard",
                                   "retrieved_on": "2036-05-02"})
    assert len(c.ok("GET", "/service-life")) == 1
    life = {r["position_uri"]: r for r in c.ok("GET", "/service-life/assets")}
    assert life[POS1]["age_years"] == 20.1 and life[POS1]["expected_life_years"] == 25
    assert life[POS1]["remaining_years"] == 4.9 and life[POS1]["end_of_life_year"] == 2041
    assert life[POS1]["reference"]["source_title"] == "owner standard"


def test_fci_says_when_its_denominator_is_partial(c):
    ids = _ids(c)
    c.ok("POST", f"/assets/{ids[POS1]}/values", {"replacement_value": 40000, "as_of": "2030-01-01"})
    c.ok("POST", f"/assets/{ids[POS1]}/values", {"replacement_value": 50000, "as_of": "2035-01-01", "basis": "quote"})
    c.ok("POST", "/deferred", {"title": "Rebuild seal", "asset_id": ids[POS1], "cost": 4000, "risk": "high"})
    item = c.ok("POST", "/deferred", {"title": "Repaint", "position_uri": POS2, "cost": 1000, "risk": "low"})
    f = c.ok("GET", "/deferred/fci")
    assert (f["deferred_cost"], f["replacement_value"], f["fci"]) == (5000, 50000, 0.1)
    assert f["assets_without_value"] == 1 and "overstated" in f["note"] and f["by_risk"]["high"] == 4000
    st, out = c("PATCH", f"/deferred/{item['id']}", {"status": "done"})
    assert st == 400 and "resolution" in out["error"]
    c.ok("PATCH", f"/deferred/{item['id']}", {"status": "done", "resolution": "painted 2036-06"})
    assert c.ok("GET", "/deferred/fci")["deferred_cost"] == 4000
    assert [i["title"] for i in c.ok("GET", "/deferred", status="open")] == ["Rebuild seal"]


def test_capital_plan_combines_end_of_life_and_deferred(c):
    ids = _ids(c)
    c.ok("POST", "/service-life", {"asset_type": "pump", "median_years": 20, "source_title": "ref",
                                   "retrieved_on": "2036-01-01"})
    c.ok("POST", f"/assets/{ids[POS1]}/values", {"replacement_value": 50000})
    c.ok("POST", "/deferred", {"title": "Seal", "asset_id": ids[POS1], "cost": 4000, "target_year": 2038})
    plan = {y["year"]: y for y in c.ok("GET", "/deferred/capital-plan", years=20)}
    # P1 (2016 + 20 = 2036) is due this year; P2 (2030 + 20 = 2050) is within 20 years
    assert plan[2036]["replacement_value"] == 50000 and len(plan[2036]["end_of_life"]) == 1
    assert plan[2038]["deferred_cost"] == 4000 and 2050 in plan
    assert 2050 not in {y["year"] for y in c.ok("GET", "/deferred/capital-plan", years=10)}


def test_validation_and_scope(c):
    assert c("POST", "/deferred", {"title": "x", "cost": 1})[0] == 400
    assert c("POST", "/deferred", {"title": "x", "position_uri": POS1, "cost": -1})[0] == 400
    assert c("POST", "/deferred", {"title": "x", "position_uri": POS1, "cost": 1, "risk": "meh"})[0] == 400
    c.ok("POST", "/deferred", {"title": "x", "position_uri": POS1, "cost": 1})
    narrow = Client(allows=lambda u: "p2" in u)
    narrow.rows = c.rows
    assert narrow.ok("GET", "/deferred") == []
    assert [r["position_uri"] for r in narrow.ok("GET", "/service-life/assets")] == [POS2]
