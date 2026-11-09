"""
Schedule activities linked to evidence, and the earned-evidence progress
report (Baler#218, epic #221 §9): "on schedule by the data, not by the
meeting".

A person links an activity (by its code, so the link survives a schedule
re-import) to one or more pieces of evidence Baler already has:

  points_online     {equip_uris: [...], min_pct: 95, within_hours: 24}
                    that share of the equipment's points reported a real
                    value in the last `within_hours` (BACnet/Modbus data)
  procedure_passed  {procedure_id, equip_uris?: [...]}
                    the procedure's latest completed test run (Cx 6:
                    start-up, power-on or FPT) passed for that equipment
  form_signed       {title}
                    a person signs it off in Baler (a checklist, or the
                    TAB report linked until the document store, #201,
                    can be pointed at): who, when, the document reference

An activity's evidence is met when every link is met. The report sorts
activities into:

  done_by_data              evidence met (and when it was met)
  due_not_evidenced         planned to finish by the as-of date, evidence not met
  marked_done_no_evidence   the schedule says complete, the evidence doesn't
  ahead_of_schedule_status  evidence met, the schedule doesn't say complete yet
  not_linked                activities nobody has linked yet (counted, listed)

The same report whether the evidence came from BACnet or from a form.
Suggested links (activity names that mention an equipment tag, matched to
that equipment's start-up procedure) are proposals only: a person
creates every link.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

KINDS = ("points_online", "procedure_passed", "form_signed")
CATEGORIES = ("done_by_data", "due_not_evidenced", "marked_done_no_evidence", "ahead_of_schedule_status")


def check_params(kind: str, params) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(params, dict):
        raise TypeError("params is an object")
    if kind == "points_online":
        uris = params.get("equip_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u.startswith("urn:equip:") for u in uris):
            raise ValueError("points_online needs equip_uris: a list of urn:equip: ids")
        pct = params.get("min_pct", 95)
        hours = params.get("within_hours", 24)
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 < pct <= 100:
            raise ValueError("min_pct is in (0, 100]")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 90:
            raise ValueError("within_hours is in (0, 2160]")
        return {"equip_uris": uris, "min_pct": pct, "within_hours": hours}
    if kind == "procedure_passed":
        if not isinstance(params.get("procedure_id"), str):
            raise ValueError("procedure_passed needs procedure_id")
        uris = params.get("equip_uris") or []
        if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
            raise ValueError("equip_uris is a list")
        return {"procedure_id": params["procedure_id"], "equip_uris": uris}
    title = params.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("form_signed needs a title (what is signed off), up to 200 characters")
    return {"title": title.strip()}


def _dt(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(v)


def judge_points(params: dict, equips: dict[str, dict], latest: dict, now: datetime) -> dict:
    pts = [p["id"] for u in params["equip_uris"] for p in (equips.get(u) or {}).get("points", [])]
    if not pts:
        return {"met": False, "detail": "none of that equipment's points are visible"}
    since = now - timedelta(hours=params["within_hours"])
    fresh = [u for u in pts if u in latest and latest[u][0] >= since]
    pct = 100.0 * len(fresh) / len(pts)
    met = pct >= params["min_pct"]
    when = max((latest[u][0] for u in fresh), default=None) if met else None
    return {"met": met, "at": when.isoformat() if when else None,
            "detail": f"{len(fresh)} of {len(pts)} points reported in the last {params['within_hours']:g} h ({pct:.0f}%, need {params['min_pct']:g}%)"}


def judge_procedure(params: dict, proc: dict | None, latest_run: dict | None) -> dict:
    if proc is None:
        return {"met": False, "detail": "no such procedure"}
    if latest_run is None:
        return {"met": False, "detail": f"{proc['name']}: no completed test run"}
    rows = latest_run.get("equipment") or []
    if params["equip_uris"]:
        rows = [r for r in rows if r["equip_uri"] in params["equip_uris"]]
        missing = set(params["equip_uris"]) - {r["equip_uri"] for r in rows}
        if missing:
            return {"met": False, "detail": f"{proc['name']}: not run on {', '.join(sorted(missing))}"}
    if not rows:
        return {"met": False, "detail": f"{proc['name']}: the test run judged no equipment"}
    bad = [r["equip_uri"] for r in rows if r["status"] != "pass"]
    fin = _dt(latest_run.get("finished_at"))
    return {"met": not bad, "at": fin.isoformat() if fin and not bad else None, "test_run_id": latest_run["id"],
            "detail": f"{proc['name']}: " + (f"not passed on {', '.join(bad)}" if bad else f"passed on {len(rows)} equipment")}


def judge_form(signoffs: list[dict]) -> dict:
    if not signoffs:
        return {"met": False, "detail": "not signed off"}
    s = signoffs[-1]
    at = _dt(s["signed_at"])
    return {"met": True, "at": at.isoformat() if at else None, "signoff_id": s["id"],
            "detail": f"signed off by {s.get('signed_by_email') or s.get('signed_by')}"
                      + (f": {s['document']['title']}" if (s.get("document") or {}).get("title") else "")}


def report(activities: list[dict], links: list[dict], judged: dict[str, dict], as_of: datetime) -> dict:
    """`judged`: link id -> {met, at, detail}."""
    by_code: dict[str, list[dict]] = {}
    for ln in links:
        by_code.setdefault(ln["activity_code"], []).append(ln)
    rows: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    not_linked = []
    for a in activities:
        if a.get("summary"):
            continue
        mine = by_code.get(a["code"], [])
        if not mine:
            not_linked.append({"code": a["code"], "name": a["name"]})
            continue
        ev = [{"link_id": ln["id"], "kind": ln["kind"], **judged[ln["id"]]} for ln in mine]
        met = all(e["met"] for e in ev)
        done_at = max((e["at"] for e in ev if e.get("at")), default=None) if met else None
        finish = a.get("planned_finish")
        due = finish is not None and datetime.fromisoformat(finish) <= as_of.replace(tzinfo=None)
        row = {"code": a["code"], "name": a["name"], "wbs": a.get("wbs"), "planned_finish": finish,
               "schedule_status": a["status"], "evidence_met": met, "evidenced_at": done_at, "evidence": ev}
        if met:
            rows["done_by_data"].append(row)
            if a["status"] != "complete":
                rows["ahead_of_schedule_status"].append(row)
        else:
            if due:
                rows["due_not_evidenced"].append(row)
            if a["status"] == "complete":
                rows["marked_done_no_evidence"].append(row)
    linked = sum(1 for a in activities if not a.get("summary") and a["code"] in by_code)
    return {"as_of": as_of.isoformat(), "activities": sum(1 for a in activities if not a.get("summary")),
            "linked": linked, "counts": {k: len(v) for k, v in rows.items()} | {"not_linked": len(not_linked)},
            **rows, "not_linked": not_linked[:500]}


_TAG = re.compile(r"\b([A-Z]{2,5})-?(\d{1,4}[A-Z]?)\b")


def suggest(activities: list[dict], equips: list[dict], procedures: list[dict]) -> list[dict]:
    """Proposals only: an activity whose name names an equipment tag
    (AHU-1, VAV-3-12) and says start-up / functional, matched to an active
    procedure of that kind that applies to that equipment."""
    from baler.commissioning import procedures as procs

    by_tag: dict[str, dict] = {}
    for e in equips:
        dis = (e.get("dis") or e["id"].rsplit("/", 1)[-1]).upper().replace(" ", "")
        by_tag[dis.replace("-", "")] = e
    out = []
    for a in activities:
        name = a["name"].upper()
        kind = "startup" if ("START" in name) else ("functional" if ("FUNCTIONAL" in name or "FPT" in name) else None)
        for m in _TAG.finditer(name):
            eq = by_tag.get(f"{m.group(1)}{m.group(2)}")
            if eq is None:
                continue
            if kind:
                for p in procedures:
                    if p.get("status") == "active" and p.get("kind") == kind and procs.applies(p, eq):
                        out.append({"activity_code": a["code"], "activity": a["name"], "kind": "procedure_passed",
                                    "params": {"procedure_id": p["id"], "equip_uris": [eq["id"]]},
                                    "why": f"names {eq.get('dis') or eq['id']} and '{kind}'; {p['name']} applies to it"})
            else:
                out.append({"activity_code": a["code"], "activity": a["name"], "kind": "points_online",
                            "params": {"equip_uris": [eq["id"]], "min_pct": 95, "within_hours": 24},
                            "why": f"names {eq.get('dis') or eq['id']}"})
    return out
