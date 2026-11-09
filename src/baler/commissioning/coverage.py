"""
Test coverage by equipment class and method (Baler#275, slice b): how
much of the building was actually tested, and how - "220 of 220 VAVs
tested automatically, 12 witnessed".

Read from each active procedure's latest completed test run (the same run
the gates judge), against the project's inventory:

  in_class   equipment of the procedure's class in scope (subclass-aware)
  sampled    the ones the procedure covers (all of them, or its equip_uris)
  tested     judged: at least one step reached pass or fail - inconclusive
             and N/A are not a test
  passed     every judged step passed and none was inconclusive
  by_method  tested by each method (manual / passive / active); one
             equipment can count under several
  automatic  tested passively or actively
  witnessed  at least one manual result with a witness

Rows are per equipment class (the procedure's), summed over the
procedures for it; equipment in more than one procedure of a class counts
once. history.coverage_pct is a different thing: data availability.
"""

from __future__ import annotations

from baler.commissioning import procedures

METHODS = procedures.METHODS


def compute(procs: list[dict], latest: dict[str, dict | None], equips: list[dict]) -> list[dict]:
    rows: dict[str, dict] = {}
    for proc in procs:
        if proc.get("status") != "active":
            continue
        cls = proc["equip_class"]
        row = rows.setdefault(cls, {"equip_class": cls, "in_class": set(), "sampled": set(), "tested": set(),
                                    "failed": set(), "incomplete": set(), "by_method": {m: set() for m in METHODS},
                                    "witnessed": set(), "procedures": []})
        fam = procedures._family(cls)
        row["in_class"] |= {e["id"] for e in equips if any(c in fam for c in e.get("brickClasses") or [])}
        row["sampled"] |= {e["id"] for e in equips if procedures.applies(proc, e)}
        tr = latest.get(proc["id"])
        row["procedures"].append({"procedure_id": proc["id"], "name": proc.get("name"), "kind": proc.get("kind"),
                                  "test_run_id": tr["id"] if tr else None})
        for e in (tr or {}).get("equipment") or []:
            uri = e["equip_uri"]
            judged = [s for s in e.get("steps") or [] if s["status"] in ("pass", "fail")]
            if judged:
                row["tested"].add(uri)
                for s in judged:
                    row["by_method"][s.get("method") or "passive"].add(uri)
            if any(s["status"] == "fail" for s in e.get("steps") or []):
                row["failed"].add(uri)
            if any(s["status"] == "inconclusive" for s in e.get("steps") or []):
                row["incomplete"].add(uri)
            if any((s.get("manual") or {}).get("witness") for s in e.get("steps") or []):
                row["witnessed"].add(uri)
    out = []
    for cls, r in sorted(rows.items()):
        auto = r["by_method"]["passive"] | r["by_method"]["active"]
        out.append({"equip_class": cls, "in_class": len(r["in_class"] | r["sampled"]), "sampled": len(r["sampled"]),
                    "tested": len(r["tested"]), "passed": len(r["tested"] - r["failed"] - r["incomplete"]),
                    "failed": len(r["failed"]), "untested": len(r["sampled"] - r["tested"]),
                    "by_method": {m: len(v) for m, v in r["by_method"].items()}, "automatic": len(auto),
                    "witnessed": len(r["witnessed"]), "procedures": r["procedures"]})
    return out


def sentence(row: dict) -> str:
    """'220 of 220 tested (220 automatically, 12 manually, 12 witnessed)'."""
    parts = [f"{row['automatic']} automatically"] if row["automatic"] else []
    if row["by_method"].get("manual"):
        parts.append(f"{row['by_method']['manual']} manually")
    if row["witnessed"]:
        parts.append(f"{row['witnessed']} witnessed")
    sampled = f" (sampled {row['sampled']} of {row['in_class']})" if row["sampled"] < row["in_class"] else ""
    return f"{row['tested']} of {row['sampled']} tested" + (f": {', '.join(parts)}" if parts else "") + sampled
