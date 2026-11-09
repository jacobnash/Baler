"""COBie import/export (#204), no services."""

import base64
import io
from datetime import date, datetime, timezone

import pytest

openpyxl = pytest.importorskip("openpyxl")

from baler.lifecycle import cobie, routes, rows

POS = "urn:equip:fbf/hosp/ahu-1"


def workbook() -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def sheet(name, header, *data):
        ws = wb.create_sheet(name)
        ws.append(header)
        for r in data:
            ws.append(r)

    sheet("Type", ["Name", "Category", "Manufacturer", "ModelNumber", "WarrantyGuarantorParts", "WarrantyDurationParts",
                   "WarrantyGuarantorLabor", "WarrantyDurationLabor", "WarrantyDurationUnit", "ReplacementCost",
                   "ExpectedLife", "DurationUnit"],
          ["AHU Type A", "23-33 00 00", "Trane", "CSAA012", "Trane", 5, "Mech Co", 1, "Year", 85000, 25, "Year"],
          ["Pump Type P", "23-21 23 00", "Bell & Gossett", "e-1510", "B&G", 18, None, None, "Month", None, None, None])
    sheet("Component", ["Name", "TypeName", "Space", "SerialNumber", "InstallationDate", "WarrantyStartDate", "TagNumber",
                        "BarCode"],
          ["AHU-1", "AHU Type A", "Mech 210", "T-123", "2026-03-01", "2026-04-15", "AHU-1", "BC001"],
          ["P-1", "Pump Type P", "Mech 110", "BG-9", date(2026, 2, 1), None, None, None],
          ["X-9", "No Such Type", "Roof", "X", None, None, None, None])
    sheet("Spare", ["Name", "Category", "TypeName", "Suppliers", "SetNumber", "PartNumber"],
          ["Filter 24x24x2 MERV 14", "Filter", "AHU Type A", "Filter Co", "12", "F-24242"],
          ["Mechanical seal", "Part", "", "", "", ""])
    sheet("Job", ["Name", "Category", "TypeName", "Description", "Duration", "DurationUnit", "Frequency", "FrequencyUnit"],
          ["Replace filters", "PM", "AHU Type A", "Replace prefilters and finals", 1, "Hour", 3, "Month"],
          ["Inspect seal", "PM", "Pump Type P", "", "", "", 1, "Year"],
          ["Vibration survey", "PM", "Pump Type P", "", "", "", 2000, "Hour"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class Client:
    def __init__(self, org="org1"):
        self.rows, self.org = rows.MemoryRows(), org

    def __call__(self, method, path, body=None, **query):
        req = routes.Request(method=method, path=path, rows=self.rows, org_id=self.org, body=body,
                             query={k: str(v) for k, v in query.items()}, who={"sub": "u1", "email": "cx@example.com"},
                             now=datetime(2026, 11, 9, tzinfo=timezone.utc))
        return routes.dispatch(req)

    def ok(self, *a, **kw):
        st, out = self(*a, **kw)
        assert st in (200, 201), out
        return out


def _import(c, data, commit, positions=None):
    return c.ok("POST", "/cobie/import", {"workbook": base64.b64encode(data).decode(), "commit": commit,
                                          "positions": positions or {}, "filename": "handover.xlsx"})


def test_dry_run_writes_nothing_and_explains_skips():
    c = Client()
    out = _import(c, workbook(), False, {"AHU-1": POS})
    assert out["dry_run"] and c.rows.find("lc_assets") == []
    s = out["summary"]
    assert (s["assets"], s["installs"], s["warranties"], s["pm_tasks"], s["spares"]) == (3, 1, 3, 2, 1)
    whys = {(k["sheet"], k["name"]): k["why"] for k in out["plan"]["skipped"]}
    assert "isn't on the Type sheet" in whys[("Component", "X-9")]
    assert "frequency" in whys[("Job", "Vibration survey")]  # hours aren't guessed into run hours
    assert whys[("Spare", "Mechanical seal")] == "no TypeName"


def test_commit_seeds_assets_warranties_pm_and_spares_once():
    c = Client()
    out = _import(c, workbook(), True, {"AHU-1": POS})
    assert out["created"] == {"assets": 3, "installs": 1, "warranties": 3, "pm_tasks": 2, "spares": 1} and not out["errors"]
    ahu = next(a for a in c.ok("GET", "/assets") if a["tag"] == "AHU-1")
    assert (ahu["manufacturer"], ahu["model"], ahu["serial"], ahu["position_uri"]) == ("Trane", "CSAA012", "T-123", POS)
    assert ahu["attributes"]["space"] == "Mech 210" and ahu["installed_at"].startswith("2026-03-01")
    ws = {w["kind"]: w for w in c.ok("GET", f"/assets/{ahu['id']}/warranties")}
    assert (ws["parts"]["starts_on"], ws["parts"]["ends_on"]) == ("2026-04-15", "2031-04-14")
    assert ws["labor"]["provider"] == "Mech Co"
    pump = next(a for a in c.ok("GET", "/assets") if a["serial"] == "BG-9")
    assert c.ok("GET", f"/assets/{pump['id']}/warranties")[0]["ends_on"] == "2027-07-31"  # 18 months from install
    tasks = {t["title"]: t for t in c.ok("GET", "/pm/tasks")}
    assert tasks["Replace filters"]["interval_days"] == 90 and tasks["Replace filters"]["asset_type"] == "AHU Type A"
    assert c.ok("GET", "/spares", type="ahu type a")[0]["part_number"] == "F-24242"
    again = _import(c, workbook(), True)
    assert again["created"]["assets"] == 0 and len(c.rows.find("lc_assets")) == 3


def test_export_round_trips_into_another_org():
    c = Client()
    _import(c, workbook(), True, {"AHU-1": POS})
    st, raw = c("GET", "/cobie/export")
    assert st == 200 and raw.filename.endswith(".xlsx")
    book = cobie.read_workbook(raw.body)
    comp = {r["Name"]: r for r in book["Component"]}
    assert comp["AHU-1"]["TypeName"] == "AHU Type A" and comp["AHU-1"]["WarrantyStartDate"] == "2026-04-15"
    t = {r["Name"]: r for r in book["Type"]}["AHU Type A"]
    assert (t["Manufacturer"], t["WarrantyDurationParts"], t["WarrantyDurationLabor"]) == ("Trane", 60, 12)
    assert {r["Name"] for r in book["Job"]} == {"Replace filters", "Inspect seal"}
    assert book["Spare"][0]["PartNumber"] == "F-24242"
    other = Client(org="org2")
    out = _import(other, raw.body, True)
    assert out["created"]["assets"] == 3 and out["created"]["spares"] == 1 and out["created"]["pm_tasks"] == 2
    assert out["created"]["warranties"] == 3  # the AHU's parts and labor, the pump's parts


def test_bad_input():
    c = Client()
    assert c("POST", "/cobie/import", {"workbook": "not base64!"})[0] == 400
    assert c("POST", "/cobie/import", {"workbook": base64.b64encode(b"not xlsx").decode()})[0] == 400
    assert c("POST", "/cobie/import", {"workbook": base64.b64encode(workbook()).decode(), "positions": {"A": 1}})[0] == 400
