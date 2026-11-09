"""
Traditional commissioning steps, in the same procedure as automated ones
(Baler#275, slice a): manual results, the per-step method choice, and how
manual, passive and active verdicts become one test run.

A manual result is a person saying "I checked this": pass, fail or N/A for
one step on one equipment in one test run, with

  - who: the caller's own sub, email, display name and role, from their
    verified token. There is no field for someone else - a result can't be
    recorded on another person's behalf, and only its recorder may edit it;
  - when: recorded_at (the server's clock) and observed_at (when the person
    says they checked it - a phone that was offline sends it later);
  - note, attachments (photos, a signed sheet: stored with their sha256),
    and an optional witness {name, org, role} - often someone without a
    Baler login, such as the owner's representative;
  - client_id: the phone's id for the entry, so a draft queued offline and
    sent twice on reconnect is stored once;
  - history: every edit keeps the previous version (status, note,
    witness, attachments, who edited, when, why).

`apply` turns a test run's computed per-equipment verdicts plus the manual
results and active-test outcomes into the run's statuses per the chosen
methods (procedures.choose_methods), keeping the machine's verdict beside
each one and flagging a contradiction when a person's pass/fail disagrees
with the machine's. `issue_results` makes those into Cx results, so a
manual fail, an active fail or a contradiction opens an issue and a later
passing retest resolves it the way any check's does.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta

from baler.commissioning import db, procedures

RESULTS = "cx_manual_results"
ATTACHMENTS = "cx_manual_attachments"
MAX_NOTE = 4000
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf", "text/plain")
MAX_OFFLINE_AGE = timedelta(days=14)  # observed_at may be this far back (a phone offline for a while)
CLOCK_SKEW = timedelta(minutes=10)


class NotYours(Exception):
    """Only the person who recorded a result may edit it."""


# ------------------------------------------------------------- validation


def _text(v, limit: int, what: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise TypeError(f"{what} is text")
    return v.strip()[:limit] or None


def witness(v) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) - {"name", "org", "role"}:
        raise ValueError("witness is {name, org?, role?}")
    name = _text(v.get("name"), 200, "witness.name")
    if not name:
        raise ValueError("a witness has a name")
    return {"name": name, "org": _text(v.get("org"), 200, "witness.org"), "role": _text(v.get("role"), 100, "witness.role")}


def attachments(v) -> list[dict]:
    """[{filename, media_type, data (base64)}] -> rows' parts with sha256
    and the decoded bytes."""
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > MAX_ATTACHMENTS:
        raise ValueError(f"attachments is a list of up to {MAX_ATTACHMENTS}")
    out = []
    for a in v:
        if not isinstance(a, dict) or set(a) - {"filename", "media_type", "data"}:
            raise ValueError("an attachment is {filename, media_type, data (base64)}")
        if a.get("media_type") not in MEDIA_TYPES:
            raise ValueError(f"attachment media_type is one of {', '.join(MEDIA_TYPES)}")
        try:
            raw = base64.b64decode(str(a.get("data") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("attachment data is base64") from exc
        if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"an attachment is 1 byte to {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        name = _text(a.get("filename"), 200, "attachment filename") or "attachment"
        out.append({"filename": name, "media_type": a["media_type"], "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(), "data": raw})
    return out


def observed_at(v, now: datetime) -> datetime:
    if v is None:
        return now
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at is an ISO date-time with a time zone") from exc
    if t.tzinfo is None:
        raise ValueError("observed_at needs a time zone")
    if t > now + CLOCK_SKEW or t < now - MAX_OFFLINE_AGE:
        raise ValueError(f"observed_at must be within the last {MAX_OFFLINE_AGE.days} days and not in the future")
    return t


def new_result(body: dict, tr: dict, proc_snapshot: dict, equip_uris: set[str], claims: dict, email: str | None,
               name: str | None, now: datetime) -> tuple[dict, list[dict]]:
    """(result row, attachment rows) for POST /test-runs/{id}/manual-results.
    The recorder is the caller - the body can't name anyone else."""
    if set(body) - {"equip_uri", "step_id", "status", "note", "witness", "attachments", "client_id", "observed_at"}:
        raise ValueError("a manual result is {equip_uri, step_id, status, note?, witness?, attachments?, client_id?, "
                         "observed_at?} - it is always recorded as the signed-in person")
    step = next((s for s in proc_snapshot["steps"] if s["id"] == body.get("step_id")), None)
    if step is None:
        raise ValueError("step_id must be a step of this test run's procedure")
    if "manual" not in procedures.step_methods(step):
        raise ValueError(f"step {step['id']} has no manual method (methods: {', '.join(procedures.step_methods(step))})")
    if body.get("equip_uri") not in equip_uris:
        raise ValueError("equip_uri must be equipment this test run judged")
    if body.get("status") not in procedures.MANUAL_STATUSES:
        raise ValueError(f"status is one of {', '.join(procedures.MANUAL_STATUSES)}")
    w = witness(body.get("witness"))
    if step.get("witness_required") and w is None:
        raise ValueError(f"step {step['id']} needs a witness")
    cid = body.get("client_id")
    if cid is not None and (not isinstance(cid, str) or not 8 <= len(cid) <= 80):
        raise ValueError("client_id is the device's id for this entry (8-80 characters)")
    files = attachments(body.get("attachments"))
    rid = db.new_id()
    att_rows = [{"id": db.new_id(), "result_id": rid, "project_id": tr["project_id"], "created_at": now, **f} for f in files]
    row = {"id": rid, "project_id": tr["project_id"], "procedure_id": tr["procedure_id"], "test_run_id": tr["id"],
           "equip_uri": body["equip_uri"], "step_id": step["id"], "status": body["status"],
           "note": _text(body.get("note"), MAX_NOTE, "note"), "witness": w,
           "attachments": [{k: a[k] for k in ("id", "filename", "media_type", "size", "sha256")} for a in att_rows],
           "recorded_by": claims.get("sub"), "recorded_email": email, "recorded_name": name, "recorded_role": claims.get("role"),
           "recorded_at": now, "observed_at": observed_at(body.get("observed_at"), now), "client_id": cid,
           "history": [], "updated_at": now}
    return row, att_rows


def edit(row: dict, body: dict, claims: dict, now: datetime) -> tuple[dict, list[dict]]:
    """(changes, new attachment rows) for PATCH /manual-results/{id}. Only
    the recorder edits; the version replaced goes into history with who,
    when and why."""
    if row.get("recorded_by") != claims.get("sub"):
        raise NotYours("only the person who recorded a result can edit it - record your own result instead")
    if set(body) - {"status", "note", "witness", "attachments", "reason"}:
        raise ValueError("an edit is {status?, note?, witness?, attachments? (added), reason}")
    why = _text(body.get("reason"), 500, "reason")
    if not why:
        raise ValueError("say why the result changed (reason)")
    changes: dict = {}
    if "status" in body:
        if body["status"] not in procedures.MANUAL_STATUSES:
            raise ValueError(f"status is one of {', '.join(procedures.MANUAL_STATUSES)}")
        changes["status"] = body["status"]
    if "note" in body:
        changes["note"] = _text(body["note"], MAX_NOTE, "note")
    if "witness" in body:
        changes["witness"] = witness(body["witness"])
    files = attachments(body.get("attachments"))
    att_rows = [{"id": db.new_id(), "result_id": row["id"], "project_id": row["project_id"], "created_at": now, **f} for f in files]
    if att_rows:
        listed = list(row.get("attachments") or []) + [{k: a[k] for k in ("id", "filename", "media_type", "size", "sha256")}
                                                        for a in att_rows]
        if len(listed) > MAX_ATTACHMENTS:
            raise ValueError(f"a result has at most {MAX_ATTACHMENTS} attachments")
        changes["attachments"] = listed
    if not changes:
        raise ValueError("nothing to change: status, note, witness or attachments")
    prior = {k: row.get(k) for k in ("status", "note", "witness", "attachments")}
    changes["history"] = [*(row.get("history") or []), {**prior, "replaced_at": now.isoformat(),
                                                        "replaced_by": claims.get("sub"), "reason": why}]
    changes["updated_at"] = now
    return changes, att_rows


# ------------------------------------------------------------ evaluation


def current(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """(equip_uri, step_id) -> the latest result (by observed time, then
    recorded time) - a later check of the same step replaces the earlier
    one as the step's verdict; both stay on the record."""
    out: dict[tuple[str, str], dict] = {}
    for r in sorted(rows, key=lambda r: (str(r.get("observed_at")), str(r.get("recorded_at")))):
        out[(r["equip_uri"], r["step_id"])] = r
    return out


def _dt(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def active_outcomes(active_runs: list[dict], since: datetime) -> dict[tuple[str, str], dict]:
    """(equip_uri, kind) -> the newest active run on that equipment that
    started at or after `since` (the test run's start), as a verdict."""
    out: dict[tuple[str, str], dict] = {}
    for r in sorted(active_runs, key=lambda r: str(r.get("started_at") or "")):
        started = _dt(r.get("started_at"))
        if started is None or started < since or r.get("status") in ("planned", "confirmed", "cancelled"):
            continue
        results = r.get("results") or []
        if r.get("status") == "running":
            status = "inconclusive"
        elif r.get("status") == "completed" and results and all(x.get("status") == "pass" for x in results):
            status = "pass"
        elif any(x.get("status") == "fail" for x in results):
            status = "fail"
        else:
            status = "inconclusive"
        out[(r["equip_uri"], r.get("kind") or "")] = {
            "active_run_id": r["id"], "status": status, "run_status": r.get("status"), "error": r.get("error"),
            "finished_at": r["finished_at"].isoformat() if isinstance(r.get("finished_at"), datetime) else r.get("finished_at"),
            "results": [{"id": x.get("id"), "title": x.get("title"), "status": x.get("status")} for x in results][:20]}
    return out


def run_kind(spec: dict) -> str:
    """The cx_active_runs kind a step's `active` spec is judged by:
    command_response, or library:<test id> (test_library, slice c)."""
    return f"library:{spec.get('test')}" if spec.get("kind") == "library" else str(spec.get("kind") or "")


def _public_manual(m: dict) -> dict:
    return {"id": m["id"], "status": m["status"], "note": m.get("note"), "witness": m.get("witness"),
            "attachments": len(m.get("attachments") or []), "recorded_by": m.get("recorded_by"),
            "recorded_email": m.get("recorded_email"), "recorded_name": m.get("recorded_name"),
            "recorded_role": m.get("recorded_role"),
            "observed_at": m["observed_at"].isoformat() if isinstance(m.get("observed_at"), datetime) else m.get("observed_at"),
            "edits": len(m.get("history") or [])}


def apply(per_equip: list[dict], proc: dict, methods: dict[str, str], manual_rows: list[dict],
          active_runs: list[dict], since: datetime) -> list[dict]:
    """Each step's status per its chosen method, the machine's verdict
    kept as machine_status (from its criteria), and a contradiction flag
    when a person's pass/fail disagrees with the machine's pass/fail.
    Re-runnable on a stored run: it only reads `criteria`."""
    by_step = {st["id"]: st for st in proc["steps"]}
    mine = current(manual_rows)
    act = active_outcomes(active_runs, since)
    for e in per_equip:
        for st in e["steps"]:
            spec = by_step.get(st["id"], {})
            method = methods.get(st["id"], procedures.step_methods(spec)[0] if spec else "passive")
            crit = st.get("criteria") or []
            machine = procedures.worst(c["status"] for c in crit) if crit else None
            m = mine.get((e["equip_uri"], st["id"]))
            a = act.get((e["equip_uri"], run_kind(spec.get("active") or {}))) if "active" in procedures.step_methods(spec) else None
            st["method"] = method
            st["machine_status"] = machine
            st["manual"] = _public_manual(m) if m else None
            st["active"] = a
            st.pop("reason", None)
            if method == "passive":
                st["status"] = machine or "inconclusive"
            elif method == "manual":
                st["status"] = m["status"] if m else "inconclusive"
                if not m:
                    st["reason"] = "awaiting a manual result"
                elif spec.get("witness_required") and not m.get("witness"):
                    st["status"], st["reason"] = "inconclusive", "the step needs a witness"
            else:
                st["status"] = a["status"] if a else "inconclusive"
                if not a:
                    st["reason"] = "no active test on this equipment since the test run started"
            st["contradiction"] = bool(m and machine in ("pass", "fail") and m["status"] in ("pass", "fail")
                                       and m["status"] != machine)
        e["status"] = procedures.worst(s["status"] for s in e["steps"])
    return per_equip


def issue_results(per_equip: list[dict], proc_id: str) -> list[dict]:
    """Cx results for what a person, an active test or a disagreement
    decided, so the issues log treats them like any check:

      manual:<procedure>/<step>         the manual verdict (manual method)
      active:<procedure>/<step>         the active-test verdict (active method)
      contradiction:<procedure>/<step>  fail while a person and the data
                                        disagree, pass once they agree

    subject is the equipment. Evidence carries last_ts (when the person
    checked, or the active run ended), which is what lets a later pass
    count as a retest after an issue was resolved."""
    out = []
    for e in per_equip:
        for st in e["steps"]:
            m, a = st.get("manual"), st.get("active")
            if st.get("method") == "manual" and m and m["status"] in ("pass", "fail"):
                out.append(_result(e, f"manual:{proc_id}/{st['id']}", m["status"],
                                   f"{st.get('title') or st['id']}: recorded {m['status']} by "
                                   f"{m.get('recorded_name') or m.get('recorded_email') or m.get('recorded_by')}"
                                   + (f" - {m['note']}" if m.get("note") else ""),
                                   {"method": "manual", "manual_result_id": m["id"], "last_ts": m["observed_at"],
                                    "witness": m.get("witness")}))
            if st.get("method") == "active" and a and a["status"] in ("pass", "fail"):
                out.append(_result(e, f"active:{proc_id}/{st['id']}", a["status"],
                                   f"{st.get('title') or st['id']}: active test {a['status']}",
                                   {"method": "active", "active_run_id": a["active_run_id"], "last_ts": a.get("finished_at")}))
            if m and st.get("machine_status") in ("pass", "fail") and m["status"] in ("pass", "fail"):
                out.append(_result(e, f"contradiction:{proc_id}/{st['id']}", "fail" if st.get("contradiction") else "pass",
                                   f"{st.get('title') or st['id']}: a person recorded {m['status']} but the data says "
                                   f"{st['machine_status']}" if st.get("contradiction") else None,
                                   {"method": "manual", "manual_result_id": m["id"], "machine_status": st["machine_status"],
                                    "manual_status": m["status"], "last_ts": m["observed_at"]}))
    return out


def _result(e: dict, check_id: str, status: str, symptom: str | None, evidence: dict) -> dict:
    return {"equip_uri": e["equip_uri"], "check_id": check_id, "subject": e["equip_uri"], "status": status,
            "severity": None, "symptom": symptom, "candidates": [], "evidence": evidence}
