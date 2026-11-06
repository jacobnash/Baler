"""A Cx run (#37): rung-1 existence and template completeness over a
scoped inventory, deterministic, honest about missing data."""

from __future__ import annotations

from datetime import datetime, timezone

import cx_fixtures as fx
import pytest

from baler.commissioning import engine
from baler.commissioning.history import InMemoryHistory
from baler.scope import Scope


@pytest.fixture
def run():
    return engine.run(fx.store(), fx.history(), [Scope(("fbf/a",))], fx.NOW, 24)


def _r(results, check, subject):
    return next(r for r in results if r["check_id"] == check and r["subject"] == subject)


def test_existence_pass_fail_inconclusive(run):
    _, results = run
    assert _r(results, "exists", fx.pt(fx.AHU, "SAT"))["status"] == "pass"
    stale = _r(results, "exists", fx.pt(fx.AHU, "RAT"))
    assert stale["status"] == "fail" and "older than the 24 h window" in stale["symptom"]
    never = _r(results, "exists", fx.pt(fx.VAV, "DMP"))
    assert never["status"] == "inconclusive"  # no data is never a pass, and not a verdict either
    assert all(c["cause"] and c["distinguish"] for c in never["candidates"])


def test_template_results(run):
    _, results = run
    assert _r(results, "template:AHU_SAT", fx.AHU)["status"] == "pass"
    missing = _r(results, "template:AHU_FanCommand", fx.AHU)
    assert missing["status"] == "fail" and missing["severity"] == "expected"
    assert _r(results, "template:VAV_Damper", fx.VAV)["evidence"]["matched_by"] == "haystack"
    assert not [r for r in results if r["equip_uri"] == fx.MTR and r["check_id"].startswith("template:")]


def test_summary_and_freshness(run):
    summary, results = run
    assert summary["equipment"] == 3 and summary["points"] == 7
    assert summary["equipment_with_template"] == 2
    assert summary["equipment_without_template"] == [{"id": fx.MTR, "brickClass": "Electrical_Meter"}]
    assert summary["unclassified_points"] == 2
    assert summary["by_status"]["pass"] + summary["by_status"]["fail"] + summary["by_status"]["inconclusive"] == len(results)
    assert summary["by_check"]["exists"] == {"pass": 4, "fail": 1, "inconclusive": 2, "not_applicable": 0}
    assert summary["freshness"] == "as of 2026-11-06 12:00 UTC: 4 of 7 points reported in the last 24 h, 1 stale, 2 never reported"
    assert summary["required_missing"] == 0  # VAV_ZAT, VAV_Damper, AHU_SAT, AHU_FanStatus all met


def test_other_org_never_appears(run):
    _, results = run
    assert not [r for r in results if "fbf/b" in r["equip_uri"] or "fbf/b" in r["subject"]]


def test_deterministic(run):
    again = engine.run(fx.store(), fx.history(), [Scope(("fbf/a",))], fx.NOW, 24)
    assert again == run


def test_empty_scope_says_so():
    summary, results = engine.run(fx.store(), InMemoryHistory(), [Scope(())], datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert results == [] and "nothing could be verified" in summary["freshness"]


@pytest.mark.parametrize("bad", [0, -1, 10_000, "24", True])
def test_window_hours_validation(bad):
    with pytest.raises(ValueError):
        engine.window_hours(bad)
    assert engine.window_hours(None) == 24.0
