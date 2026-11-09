"""Drawing schedule extraction (#203): the staged pipeline on the labelled
eval set, and the route that turns rows into proposals. No model key."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")

from baler.lifecycle import routes, rows, schedule_eval, schedules


def test_two_row_headers_are_joined():
    grid = [["", "", "MOTOR", None, None], ["TAG", "CFM", "HP", "V", "PH"], ["EF-1", "1200", "0.5", "115", "1"]]
    headers, body = schedules.to_schedule(grid)
    assert headers == ["TAG", "CFM", "MOTOR HP", "MOTOR V", "MOTOR PH"] and body == [["EF-1", "1200", "0.5", "115", "1"]]


def test_regions_are_found_one_schedule_at_a_time():
    import io

    import pdfplumber

    case = next(c for c in schedule_eval.CASES if c.id == "two-schedules-sheet")
    with pdfplumber.open(io.BytesIO(schedule_eval.render(case))) as pdf:
        regions = schedules.find_regions(pdf.pages[0], 1)
    assert [r.title for r in regions] == ["CHILLER SCHEDULE", "COOLING TOWER SCHEDULE"]
    assert regions[0].bbox[2] < regions[1].bbox[0] + 4  # side by side: the first ends where the second begins
    assert all("SCHEDULES" not in r.title for r in regions)  # the title block's "MECHANICAL SCHEDULES" isn't one


def test_local_reader_meets_its_measured_baseline():
    _results, tot = schedule_eval.run("local")
    for k, v in schedule_eval.BASELINE.items():
        assert tot[k] >= v, (k, tot)


def test_scoring_counts_wrong_and_invented_cells():
    case = next(c for c in schedule_eval.CASES if c.id == "pump-unruled")
    s = case.schedules[0]
    perfect = schedule_eval.score_case(case, [{"title": s.title, "headers": s.headers, "rows": s.rows}])
    assert perfect["correct_cells"] == perfect["true_cells"] == perfect["predicted_cells"]
    wrong = [list(r) for r in s.rows]
    wrong[0][2] = "999"
    got = schedule_eval.score_case(case, [{"title": s.title, "headers": s.headers, "rows": wrong},
                                          {"title": "BOILER SCHEDULE", "headers": ["TAG"], "rows": [["B-1"]]}])
    assert got["correct_cells"] == perfect["true_cells"] - 1 and got["predicted_cells"] == perfect["true_cells"] + 1


class Client:
    def __init__(self, llm=None):
        self.rows, self.llm = rows.MemoryRows(), llm

    def __call__(self, method, path, body=None, raw=None, ctype="", **query):
        req = routes.Request(method=method, path=path, rows=self.rows, org_id="org1", body=body, raw=raw,
                             content_type=ctype, query={k: str(v) for k, v in query.items()}, who={"sub": "u1"},
                             now=datetime(2026, 11, 9, tzinfo=timezone.utc), services={"llm": self.llm} if self.llm else {})
        return routes.dispatch(req)

    def ok(self, *a, **kw):
        st, out = self(*a, **kw)
        assert st in (200, 201), out
        return out


def _drawing(c, case_id="ahu-ruled"):
    case = next(x for x in schedule_eval.CASES if x.id == case_id)
    return c.ok("POST", "/documents", raw=schedule_eval.render(case), ctype="application/pdf", filename="M-601.pdf",
                kind="drawing")


def test_rows_become_cited_proposals(monkeypatch):
    for k in ("BALER_LLM_PROVIDER", "ANTHROPIC_API_KEY", "BALER_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    c = Client()
    d = _drawing(c)
    out = c.ok("POST", f"/documents/{d['id']}/schedules", {})
    assert out["schedules"][0]["reader"] == "local" and out["schedules"][0]["rows"] == 3
    p = out["proposals"][0]
    assert p["kind"] == "schedule_row" and p["status"] == "proposed" and p["quote_verified"] and p["page"] == 1
    assert p["payload"] == {"schedule": "AIR HANDLING UNIT SCHEDULE", "tag": "AHU-1", "values": {
        "TAG": "AHU-1", "LOCATION": "ROOF", "CFM": "12000", "ESP (IN WC)": "2.5", "MOTOR HP": "15",
        "MANUFACTURER": "TRANE", "MODEL": "CSAA012"}}
    assert c.ok("POST", f"/document-proposals/{p['id']}/decide", {"action": "confirm"})["status"] == "confirmed"
    txt = c.ok("POST", "/documents", raw=b"not a drawing", ctype="text/plain", filename="x.txt")
    assert c("POST", f"/documents/{txt['id']}/schedules", {})[0] == 415


def test_vision_reader_reads_the_region_image_and_falls_back_on_error():
    seen = []

    class Messages:
        def create(self, **kw):
            img = kw["messages"][0]["content"][0]
            seen.append(img["source"]["media_type"])
            reply = {"headers": ["TAG", "CFM"], "rows": [["AHU-1", "12000"]]}
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(reply))])

    fake = SimpleNamespace(kind="anthropic", beta=SimpleNamespace(messages=Messages()))
    case = next(x for x in schedule_eval.CASES if x.id == "ahu-ruled")
    found, info = schedules.extract(schedule_eval.render(case), "vision", fake)
    assert seen == ["image/png"] and found[0].reader == "vision" and found[0].rows == [["AHU-1", "12000"]]

    class Broken:
        kind = "anthropic"
        beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))))

    found, info = schedules.extract(schedule_eval.render(case), "vision", Broken())
    assert found[0].reader == "local" and "down" in info["fallbacks"][0]
