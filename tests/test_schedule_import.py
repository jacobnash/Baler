"""Construction schedule import (#217): clean-room XER and MSPDI parsers on
small synthetic files, and the API."""

from __future__ import annotations

import base64
import os

import cx_api_harness as hx
import pytest
import requests

from baler import schedule_import

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "schedules")


def _read(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


EXPECTED = [("AHU-1 start-up", "complete", "2027-02-01T08:00:00", "2027-02-04T12:00:00"),
            ("Level 3 VAVs online", "in_progress", "2027-02-04T08:00:00", None),
            ("TAB complete, Level 3", "not_started", "2027-02-20T17:00:00", None)]


@pytest.mark.parametrize("name, fmt", [("sample.xer", "xer"), ("sample-mspdi.xml", "mspdi")])
def test_both_formats_give_the_same_activities(name, fmt):
    acts, info = schedule_import.parse(_read(name))
    assert info["format"] == fmt and info["data_date"].startswith("2027-03-01")
    work = [a for a in acts if not a["summary"]]
    assert [(a["name"], a["status"], a["planned_start"], a["actual_finish"]) for a in work] == EXPECTED
    tab = work[2]
    assert tab["milestone"] is True and tab["predecessors"] == [work[1]["code"]]
    assert work[1]["percent_complete"] == 40.0


def test_xer_wbs_and_codes():
    acts, info = schedule_import.parse(_read("sample.xer"))
    assert [a["code"] for a in acts] == ["M-1000", "M-1010", "M-1020"] and acts[0]["wbs"] == "Level 3 Mechanical"
    assert info["project"] == "HOSP-EXP" and info["tables"]["TASK"] == 3


def test_refuses_other_files_and_doctype():
    with pytest.raises(schedule_import.ScheduleError, match="not a P6"):
        schedule_import.parse(b"hello")
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Project xmlns="http://schemas.microsoft.com/project"/>'
    with pytest.raises(schedule_import.ScheduleError, match="DOCTYPE"):
        schedule_import.parse(evil)
    with pytest.raises(schedule_import.ScheduleError, match="TASK"):
        schedule_import.parse(b"ERMHDR\t19\n%T\tPROJECT\n%F\tproj_id\n%R\t1\n%E\n")


def test_schedule_api(monkeypatch):
    base, server = hx.serve(monkeypatch, hx.repo())
    try:
        pid = hx.project(base)["id"]
        s = hx.check(requests.post(f"{base}/projects/{pid}/schedules", headers=hx.A,
                                   json={"filename": "sample.xer", "data": base64.b64encode(_read("sample.xer")).decode()}),
                     "/projects/{id}/schedules", "post", 201)
        assert s["format"] == "xer" and s["activities"] == 3
        acts = hx.check(requests.get(f"{base}/schedules/{s['id']}/activities?status=in_progress", headers=hx.A),
                        "/schedules/{id}/activities", "get", 200)
        assert [a["code"] for a in acts] == ["M-1010"]
        assert len(requests.get(f"{base}/schedules/{s['id']}/activities?q=vav", headers=hx.A).json()) == 1
        hx.check(requests.get(f"{base}/projects/{pid}/schedules", headers=hx.A), "/projects/{id}/schedules", "get", 200)
        assert requests.get(f"{base}/schedules/{s['id']}/activities", headers=hx.B).status_code == 404
        assert requests.post(f"{base}/projects/{pid}/schedules", json={"data": base64.b64encode(b"x").decode()},
                             headers=hx.A).status_code == 400
    finally:
        server.shutdown()
