"""Warranties on assets (#198), with no services."""

from datetime import date, datetime, timezone

from baler.lifecycle import routes, rows, warranties

POS = "urn:equip:fbf/hosp/chw-p1"


class Client:
    def __init__(self, now=datetime(2027, 1, 15, tzinfo=timezone.utc), failures=None, allows=lambda _u: True):
        self.rows = rows.MemoryRows()
        self.now, self.failures, self.allows = now, failures, allows

    def __call__(self, method, path, body=None, **query):
        services = {} if self.failures is None else {"failures": self.failures}
        req = routes.Request(method=method, path=path, rows=self.rows, org_id="org1", body=body, allows=self.allows,
                             query={k: str(v) for k, v in query.items()}, who={"sub": "u1"}, now=self.now,
                             services=services)
        return routes.dispatch(req)

    def ok(self, *a, **kw):
        st, out = self(*a, **kw)
        assert st in (200, 201), out
        return out


def _installed_pump(c, at="2026-03-01"):
    a = c.ok("POST", "/assets", {"asset_type": "pump", "manufacturer": "Bell & Gossett", "serial": "P-1"})
    c.ok("POST", f"/assets/{a['id']}/install", {"position_uri": POS, "at": at, "reason": "new"})
    return a


def test_duration_defaults_to_start_up_and_cites_its_page():
    c = Client()
    a = _installed_pump(c)
    w = c.ok("POST", f"/assets/{a['id']}/warranties", {
        "kind": "parts", "duration_months": 12, "terms": "Parts only, from start-up",
        "source": {"document_id": "doc1", "page": 47, "quote": "warranted for twelve (12) months from start-up"}})
    assert (w["starts_on"], w["ends_on"]) == ("2026-03-01", "2027-02-28")
    assert w["status"] == "active" and w["days_left"] == 44 and w["source"]["page"] == 47
    assert c("POST", f"/assets/{a['id']}/warranties", {"duration_months": 12, "source": {"page": 0}})[0] == 400
    stock = c.ok("POST", "/assets", {"serial": "spare"})
    st, out = c("POST", f"/assets/{stock['id']}/warranties", {"duration_months": 12})
    assert st == 400 and "starts_on" in out["error"]


def test_month_arithmetic_clamps_to_month_end():
    assert warranties._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert warranties._add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert warranties._add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)


def test_expiring_alert_window():
    c = Client()
    a = _installed_pump(c)
    c.ok("POST", f"/assets/{a['id']}/warranties", {"kind": "parts", "starts_on": "2026-03-01", "ends_on": "2027-02-28"})
    c.ok("POST", f"/assets/{a['id']}/warranties", {"kind": "extended", "starts_on": "2026-03-01", "ends_on": "2031-02-28"})
    out = c.ok("GET", "/warranties/alerts", within_days=60)
    assert [w["kind"] for w in out["expiring"]] == ["parts"] and out["expiring"][0]["asset"]["serial"] == "P-1"
    assert c.ok("GET", "/warranties/alerts", within_days=30)["expiring"] == []
    assert [w["kind"] for w in c.ok("GET", "/warranties", status="active")] == ["parts", "extended"]


def test_failure_under_warranty_matches_the_asset_installed_at_the_time():
    fails = [
        {"kind": "fault", "id": "1", "subject_uri": POS.replace("urn:equip:", "urn:point:") + "/status",
         "opened_at": "2026-09-01T00:00:00Z", "title": "pump_cmd_status_mismatch", "open": True},
        {"kind": "cx_issue", "id": "i2", "subject_uri": POS, "opened_at": "2026-12-01T00:00:00Z", "title": "seal leak",
         "open": True},
        {"kind": "fault", "id": "3", "subject_uri": "urn:point:fbf/hosp/ahu-9/sat", "opened_at": "2026-09-01T00:00:00Z",
         "title": "other equipment", "open": True},
    ]
    c = Client(failures=fails)
    a = _installed_pump(c)
    c.ok("POST", f"/assets/{a['id']}/warranties", {"kind": "parts", "starts_on": "2026-03-01", "ends_on": "2026-10-31"})
    out = c.ok("GET", "/warranties/alerts")["failures_under_warranty"]
    # the December issue is after the warranty ended; ahu-9 has no asset here
    assert [f["failure"]["id"] for f in out] == ["1"] and out[0]["asset"]["id"] == a["id"]

    # replaced in October: a failure after that is the new pump's, which has no warranty
    c.ok("POST", "/positions/replace", {"position_uri": POS, "at": "2026-10-15", "remove_reason": "seal failure",
                                        "asset": {"asset_type": "pump", "manufacturer": "Armstrong"}})
    c.failures = [{**fails[0], "id": "4", "opened_at": "2026-10-20T00:00:00Z"}]
    assert c.ok("GET", "/warranties/alerts")["failures_under_warranty"] == []
    hidden = Client(failures=fails, allows=lambda u: "chw" not in u)
    hidden.rows = c.rows
    got = hidden.ok("GET", "/warranties/alerts")
    assert got["expiring"] == [] and got["failures_under_warranty"] == []


def test_edit_and_void_are_audited():
    c = Client()
    a = _installed_pump(c)
    w = c.ok("POST", f"/assets/{a['id']}/warranties", {"duration_months": 12})
    c.ok("PATCH", f"/warranties/{w['id']}", {"provider": "Rep Co"})
    assert c("PATCH", f"/warranties/{w['id']}", {"ends_on": "2020-01-01"})[0] == 400
    assert c("PATCH", f"/warranties/{w['id']}", {"asset_id": "x"})[0] == 400
    assert c.ok("POST", f"/warranties/{w['id']}/void", {"reason": "entered twice"})["status"] == "void"
    assert c("PATCH", f"/warranties/{w['id']}", {"provider": "x"})[0] == 409
    log = [x["action"] for x in c.rows.audit_log(rows.entity_uri("warranty", w["id"]))]
    assert log == ["warranty.void", "warranty.edit", "warranty.create"]
    assert c.ok("GET", "/warranties/alerts")["expiring"] == []
