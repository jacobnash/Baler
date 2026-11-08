"""The Cx project report, pinned (Baler#150): the speed work on its read
path must not change a byte of what it says. A project with a completed
run, issues opened from it (one with an accepted risk, one closed), a
procedure and its test run, spec items and acceptance settings - every
section the report has - rendered as JSON and HTML on MemoryRepo with a
fixed clock. Ids are random per run, so they are replaced by stable
placeholders in order of first appearance. No services.

To accept an intended change: BALER_UPDATE_GOLDEN=1 pytest this file,
then review the diff of tests/golden/."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import timedelta
from pathlib import Path

import cx_fixtures as fx
import pytest
import requests
import test_commissioning_openapi as api

from baler.commissioning import db

GOLDEN = Path(__file__).parent / "golden"
A = api.A


@pytest.fixture
def repo():
    return db.MemoryRepo(store=fx.store(), history=fx.history(), sites=api.SITES, scope_fn=api._scope,
                         users={"u-a": "a@example.com"}, names={"u-a": "Ann Example"})


@pytest.fixture
def base(monkeypatch, repo):
    # Ids in creation order: with a fixed clock every row has the same
    # created_at, and lists ordered (created_at, id) would follow random ids.
    from baler import json_store

    counter = iter(range(1, 10**6))

    def seq():
        return f"{next(counter):032x}"

    monkeypatch.setattr(db, "new_id", seq)
    monkeypatch.setattr(json_store, "new_id", seq)
    server = api._serve(monkeypatch, repo)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _stable(text: str) -> str:
    """Ids (32 hex, json_store.new_id) -> id-1, id-2, ... by first appearance."""
    seen: dict[str, str] = {}
    return re.sub(r"\b[0-9a-f]{32}\b", lambda m: seen.setdefault(m.group(0), f"id-{len(seen) + 1}"), text)


def _check(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("BALER_UPDATE_GOLDEN"):
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"no golden file {path}: run with BALER_UPDATE_GOLDEN=1"
    assert text == path.read_text(encoding="utf-8"), f"{name} changed - see the module docstring"


def _report_project(base) -> str:
    p, run = api._completed_run(base, settings={
        "cxa_name": "Ann Example", "acceptance": {"min_pct": 90, "max_gap_minutes": 10, "settle_minutes": 30}})
    opened = requests.post(f"{base}/runs/{run['id']}/issues", headers=A, json={}).json()["opened"]
    assert len(opened) >= 2, opened
    review = (fx.NOW.date() + timedelta(days=60)).isoformat()
    assert requests.post(f"{base}/issues/{opened[0]}/risk", headers=api._h(), json={
        "reason": "read-only point", "owner": "Controls Co", "retest_trigger": "writes enabled",
        "accepted_name": "Ann Example", "review_by": review}).status_code == 201
    assert requests.patch(f"{base}/issues/{opened[1]}", headers=A,
                          json={"status": "in_progress", "note": "on it"}).status_code == 200
    manual = requests.post(f"{base}/projects/{p['id']}/issues", headers=A, json={"title": "Label the AHU"}).json()
    assert requests.patch(f"{base}/issues/{manual['id']}", headers=A, json={"status": "closed", "note": "labelled"}).status_code == 200
    proc = requests.post(f"{base}/projects/{p['id']}/procedures", headers=A, json={
        "name": "AHU seq", "kind": "startup", "equip_class": "Air_Handling_Unit",
        "steps": [{"id": "s1", "title": "Supply air band",
                   "criteria": [{"range": {"min": 40, "max": 80, "unit": "F"},
                                 "point_class": "Discharge_Air_Temperature_Sensor", "sequence": "S1"}]}]}).json()
    tr = requests.post(f"{base}/procedures/{proc['id']}/runs", headers=A,
                       json={"started_at": (fx.NOW - timedelta(hours=2)).isoformat(), "hours": 2})
    assert tr.status_code in (200, 201, 202), tr.text
    deadline = time.monotonic() + 10
    while requests.get(f"{base}/procedures/{proc['id']}/runs", headers=A).json()[0]["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    requests.post(f"{base}/projects/{p['id']}/spec/items", headers=A, json={"items": [
        {"tag": "AHU-1", "equip_class": "Air_Handling_Unit", "points": ["Supply_Air_Temperature_Sensor"]}]})
    return p["id"]


def test_project_report_is_pinned(base):
    pid = _report_project(base)
    as_json = requests.get(f"{base}/projects/{pid}/report", headers=A)
    as_html = requests.get(f"{base}/projects/{pid}/report", params={"format": "html"}, headers=A)
    assert as_json.status_code == 200 and as_html.status_code == 200
    _check("cx_project_report.json", _stable(json.dumps(as_json.json(), indent=1, sort_keys=True)) + "\n")
    _check("cx_project_report.html", _stable(as_html.text))
