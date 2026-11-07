"""
Phase gates (Baler#86): what has to be true before a project moves
forward into start-up, functional testing or acceptance, computed from
the records - never typed in.

  startup      a verification run exists; every point reports
  functional   + every power-on / start-up procedure's latest test run
                 has no failing step
  acceptance   + every procedure's latest test run has no failing or
  (and later)    inconclusive step; no open issue (open, in progress,
                 or resolved but not yet closed by a person); no
                 inconclusive result in the latest run - unless waived

A waiver is an accepted risk on the issue for that (check, subject): the
CxA's explicit "known and accepted", with an owner and a review-by date.
Inconclusive is never a pass, so the acceptance gate needs zero of them
or a waiver for each.

Projects choose what an unmet gate does (settings.phase_gates): "warn"
(the default - the move goes ahead and the event records what was
unmet), "block" (409 unless the request overrides with a note), or
"off". Moving back a phase is never gated.

Pure functions over dicts; commissioning_api gathers the records.
"""

from __future__ import annotations

MODES = ("warn", "block", "off")
DEFAULT_MODE = "warn"
_ACCEPTANCE = {"acceptance", "warranty", "operations"}
_EARLY_PROCEDURES = {"power_on", "startup"}
MAX_ITEMS = 50


def mode(settings: dict | None) -> str:
    m = (settings or {}).get("phase_gates", DEFAULT_MODE)
    return m if m in MODES else DEFAULT_MODE


def validate_mode(value) -> None:
    if value not in MODES:
        raise ValueError(f"phase_gates is one of {', '.join(MODES)}")


def rules_for(key: str) -> list[str]:
    if key == "startup":
        return ["verification_run", "points_reporting"]
    if key == "functional":
        return ["verification_run", "points_reporting", "early_procedures_pass"]
    if key in _ACCEPTANCE:
        return ["verification_run", "points_reporting", "procedures_pass", "no_open_issues", "no_unwaived_inconclusive"]
    return []


def _waived(issues: list[dict], risks: dict[str, dict]) -> set[tuple]:
    return {(i["check_id"], i["subject"]) for i in issues
            if i["status"] == "risk_accepted" and i.get("check_id") and i["id"] in risks}


def evaluate(key: str, latest_run: dict | None, results: list[dict], procedures: list[dict],
             latest_test_runs: dict[str, dict | None], issues: list[dict], risks: dict[str, dict]) -> dict:
    """{target, rules, met, unmet: [{rule, message, items}]} for moving into
    phase `key`. `latest_run`/`results`: the newest completed verification
    run; `latest_test_runs`: procedure id -> newest completed test run (or
    None); `issues`: the project's active issues; `risks`: issue id ->
    standing risk."""
    rules = rules_for(key)
    waived = _waived(issues, risks)
    unmet: list[dict] = []

    def fail(rule: str, message: str, items: list | None = None) -> None:
        items = items or []
        unmet.append({"rule": rule, "message": message, "count": len(items), "items": items[:MAX_ITEMS]})

    if "verification_run" in rules and latest_run is None:
        fail("verification_run", "no completed verification run yet")
    if "points_reporting" in rules and latest_run is not None:
        bad = [{"subject": r["subject"], "equip_uri": r.get("equip_uri"), "status": r["status"], "symptom": r.get("symptom")}
               for r in results if r["check_id"] == "exists" and r["status"] != "pass"
               and (r["check_id"], r["subject"]) not in waived]
        if bad:
            fail("points_reporting", f"{len(bad)} point(s) not reporting in run {latest_run['id']}", bad)
    for rule, kinds, statuses in (("early_procedures_pass", _EARLY_PROCEDURES, ("fail",)),
                                  ("procedures_pass", None, ("fail", "inconclusive"))):
        if rule not in rules:
            continue
        bad = []
        for proc in procedures:
            if proc.get("status") != "active" or (kinds and proc.get("kind") not in kinds):
                continue
            tr = latest_test_runs.get(proc["id"])
            if tr is None:
                bad.append({"procedure_id": proc["id"], "procedure": proc.get("name"), "status": "never run"})
                continue
            by = (tr.get("summary") or {}).get("by_status") or {}
            hits = {s: by.get(s, 0) for s in statuses if by.get(s, 0)}
            if hits:
                bad.append({"procedure_id": proc["id"], "procedure": proc.get("name"), "test_run_id": tr["id"],
                            "status": ", ".join(f"{n} {s}" for s, n in hits.items())})
        if bad:
            what = "power-on / start-up procedure(s)" if kinds else "procedure(s)"
            fail(rule, f"{len(bad)} {what} not passing on their latest test run", bad)
    if "no_open_issues" in rules:
        bad = [{"issue_id": i["id"], "title": i["title"], "status": i["status"], "subject": i.get("subject")}
               for i in issues if i["status"] in ("open", "in_progress", "ready_for_retest")]
        if bad:
            fail("no_open_issues", f"{len(bad)} issue(s) not closed (a resolved issue still needs a person to close it "
                                   "after its retest)", bad)
    if "no_unwaived_inconclusive" in rules and latest_run is not None:
        bad = [{"check_id": r["check_id"], "subject": r["subject"], "equip_uri": r.get("equip_uri"), "symptom": r.get("symptom")}
               for r in results if r["status"] == "inconclusive" and (r["check_id"], r["subject"]) not in waived]
        if bad:
            fail("no_unwaived_inconclusive", f"{len(bad)} inconclusive result(s) without a waiver: inconclusive is "
                                             "never a pass - resolve each or accept it as a risk", bad)
    return {"target": key, "rules": rules, "met": not unmet, "unmet": unmet}


def summary(gate: dict) -> dict:
    """The gate as an event detail: rules and counts, no item lists."""
    return {"target": gate["target"], "met": gate["met"],
            "unmet": [{"rule": u["rule"], "count": u["count"], "message": u["message"]} for u in gate["unmet"]]}
