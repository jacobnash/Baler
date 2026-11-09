"""Facts from O&M documents as proposals (#202), no services, no model key."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from baler.lifecycle import extraction, routes, rows

OM = ("SUPPLY FAN MOTOR - OPERATION AND MAINTENANCE\n"
      "Manufacturer: Baldor Electric\nModel No: EM3615T\nSerial No: F1704A123\n"
      "\f"
      "Maintenance\n"
      "Replace the 24x24x2 pleated filters every 3 months. Lubricate motor bearings every 2,000 operating hours.\n"
      "Inspect belt tension monthly.\n"
      "\f"
      "Warranty\n"
      "The equipment is warranted against defects in parts and labor for one (1) year from the date of start-up.\n"
      "\f"
      "Start-up checklist\n"
      "1. Verify rotation\n2. Check belt tension\n3. Record amperage on each phase\n\nNotes follow.")


class Client:
    def __init__(self, llm=None):
        self.rows = rows.MemoryRows()
        self.llm = llm

    def __call__(self, method, path, body=None, raw=None, ctype="", **query):
        req = routes.Request(method=method, path=path, rows=self.rows, org_id="org1", body=body, raw=raw,
                             content_type=ctype, query={k: str(v) for k, v in query.items()}, who={"sub": "u1"},
                             now=datetime(2026, 11, 9, tzinfo=timezone.utc),
                             services={"llm": self.llm} if self.llm else {})
        return routes.dispatch(req)

    def ok(self, *a, **kw):
        st, out = self(*a, **kw)
        assert st in (200, 201), out
        return out

    def upload(self, text=OM):
        return self.ok("POST", "/documents", raw=text.encode(), ctype="text/plain", filename="sf-om.txt",
                       kind="om_manual", title="SF motor O&M")


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    for k in ("BALER_LLM_PROVIDER", "ANTHROPIC_API_KEY", "BALER_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_rules_only_without_a_model_key():
    c = Client()
    d = c.upload()
    out = c.ok("POST", f"/documents/{d['id']}/extract")
    run = out["extraction"]
    assert run["provider"] == "none" and "no model configured" in run["info"]["notes"]
    by = {}
    for p in out["proposals"]:
        by.setdefault(p["kind"], []).append(p)
        assert p["status"] == "proposed" and p["quote_verified"] and p["method"] == "rules"
    w = by["warranty"][0]
    assert w["payload"] == {"duration_months": 12, "covers": "parts_and_labor", "starts_from": "start-up"}
    assert w["page"] == 3 and "one (1) year" in w["quote"]
    pm = {p["payload"].get("interval_days") or p["payload"].get("interval_hours"): p["payload"] for p in by["pm_task"]}
    assert pm[90]["parts"] == ["filter 24x24x2"] and pm[2000]["trigger"] == "run_hours" and pm[30]["trigger"] == "calendar"
    assert by["nameplate"][0]["payload"] == {"manufacturer": "Baldor Electric", "model": "EM3615T", "serial": "F1704A123"}
    assert by["startup_checklist"][0]["payload"]["steps"] == ["Verify rotation", "Check belt tension",
                                                              "Record amperage on each phase"]


def test_nothing_is_applied_until_a_person_confirms():
    c = Client()
    d = c.upload()
    props = c.ok("POST", f"/documents/{d['id']}/extract", {"kinds": ["warranty"]})["proposals"]
    assert [p["kind"] for p in props] == ["warranty"]
    p = props[0]
    out = c.ok("POST", f"/document-proposals/{p['id']}/decide",
               {"action": "confirm", "payload": {**p["payload"], "duration_months": 18}, "note": "per rep letter"})
    assert out["status"] == "confirmed" and out["decided_payload"]["duration_months"] == 18
    assert out["payload"]["duration_months"] == 12  # what was read stays as read
    assert out["citation"] == "SF motor O&M, p. 3"
    assert c("POST", f"/document-proposals/{p['id']}/decide", {"action": "reject"})[0] == 409
    assert c.ok("GET", "/document-proposals", status="confirmed")[0]["id"] == p["id"]
    log = [a["action"] for a in c.rows.audit_log(rows.entity_uri("document", d["id"]))]
    assert log[:2] == ["proposal.confirm", "document.extract"]


class FakeOpenAI:
    kind = "openai"
    model = "fake-model"

    def __init__(self, items):
        self.items = items

    def chat(self, messages, **params):
        return {"model": self.model, "choices": [{"message": {"content": json.dumps({"items": self.items})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_model_proposals_need_a_real_quote():
    llm = FakeOpenAI([
        {"kind": "tab_measurement", "payload": {"space": "OR 3", "quantity": "supply airflow", "design": 2400,
                                                "measured": 2310, "unit": "cfm"},
         "page": 1, "quote": "OR 3 supply 2400 design 2310 measured", "confidence": 0.9},
        {"kind": "warranty", "payload": {"duration_months": 60}, "page": 3, "quote": "five year compressor warranty",
         "confidence": 0.9},  # invented: not in the document
        {"kind": "nameplate", "payload": {"model": "EM3615T"}, "page": 4, "quote": "Model No: EM3615T", "confidence": 0.8},
    ])
    c = Client(llm=llm)
    d = c.upload("OR 3 supply 2400 design 2310 measured\f" + OM)
    out = c.ok("POST", f"/documents/{d['id']}/extract")
    llm_props = [p for p in out["proposals"] if p["method"] == "llm"]
    assert {p["kind"] for p in llm_props} == {"tab_measurement", "nameplate"}
    plate = next(p for p in llm_props if p["kind"] == "nameplate")
    assert plate["page"] == 2  # the cited page was wrong; the quote is on page 2
    assert out["extraction"]["info"]["dropped"] == 1 and out["extraction"]["model"] == "fake-model"


def test_a_failing_model_leaves_the_rules_proposals():
    class Broken(FakeOpenAI):
        def chat(self, messages, **params):
            raise RuntimeError("endpoint down")

    c = Client(llm=Broken([]))
    d = c.upload()
    out = c.ok("POST", f"/documents/{d['id']}/extract")
    assert out["proposals"] and "endpoint down" in out["extraction"]["error"]


def test_anthropic_path_uses_structured_output():
    pytest.importorskip("pydantic")

    class Messages:
        def parse(self, **kw):
            assert kw["output_format"] is extraction._Extraction and kw["fallbacks"] == "default"
            item = extraction._Item(kind="pm_task", payload_json=json.dumps({"title": "Inspect belt", "trigger": "calendar",
                                                                             "interval_days": 30}),
                                    page=2, quote="Inspect belt tension monthly.", confidence=0.7)
            return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                                   parsed_output=extraction._Extraction(items=[item]),
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    fake = SimpleNamespace(kind="anthropic", beta=SimpleNamespace(messages=Messages()))
    c = Client(llm=fake)
    d = c.upload()
    out = c.ok("POST", f"/documents/{d['id']}/extract", {"kinds": ["pm_task"]})
    assert out["extraction"]["provider"] == "anthropic"
    # the model's item quotes the same sentence the rules found, so it's deduplicated against it
    assert out["extraction"]["model"] == extraction.llm.anthropic_model() and len(out["proposals"]) == 3


def test_a_document_without_text_is_refused():
    c = Client()
    d = c.ok("POST", "/documents", raw=b"\x89PNG", ctype="image/png", filename="n.png")
    assert c("POST", f"/documents/{d['id']}/extract")[0] == 409
