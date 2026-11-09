"""
A small labelled eval set for drawing-schedule extraction (#203).

No public MEP benchmark exists, so before any extraction number is quoted
we need our own. This one is **synthetic**: each case is a schedule (or
several) written down here as data, the ground truth, and rendered to a
vector PDF with reportlab in a layout real prints use:

  - ruled grids, and schedules with no ruling lines at all;
  - two-row headers (a group like MOTOR over HP / V / PH);
  - two schedules side by side on one sheet, with general notes and a
    title block around them;
  - landscape sheets, small dense type, blank and "-" cells.

Synthetic PDFs have a clean text layer, which real prints often lack
(scans, text drawn as vectors, SHX fonts). Scores here are an upper bound
for the text-layer pipeline, not a measure of real-world accuracy. Adding
real, labelled sheets (with permission to keep them) is the next step.

Scoring is per cell. Predicted schedules are matched to true ones by
title, rows by tag (first column), headers by normalised name. Precision
= correct predicted cells / predicted cells; recall = correct / true
cells. A schedule found counts toward region recall.

    python -m baler.lifecycle.schedule_eval            # local pipeline, per-case table
    python -m baler.lifecycle.schedule_eval --json     # machine-readable
    python -m baler.lifecycle.schedule_eval --check    # fail if below BASELINE (CI)
    python -m baler.lifecycle.schedule_eval --reader vision   # the configured vision model (costs tokens)

Needs reportlab (the dev extra) and pdfplumber (the cx-spec extra). PDFs
are built in memory; nothing is written to disk.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from dataclasses import dataclass, field

# Measured with the local reader on this set (see docs-site/pages/drawing-schedules.mdx).
# --check fails if the local reader drops below these.
BASELINE = {"region_recall": 1.0, "cell_f1": 0.98}


@dataclass
class Sched:
    title: str
    headers: list[str]  # final column names; a two-row header is "GROUP SUB"
    rows: list[list[str]]
    groups: list[tuple[str, int]] = field(default_factory=list)  # (group label, span) over headers, for 2-row headers
    ruled: bool = True


@dataclass
class Case:
    id: str
    schedules: list[Sched]
    landscape: bool = False
    font_size: float = 7.0
    side_by_side: bool = False
    clutter: bool = False  # general notes and a title block on the sheet
    note: str = ""


def _vavs(n: int) -> list[list[str]]:
    rows = []
    for i in range(1, n + 1):
        cfm_max = 400 + 50 * (i % 9)
        rows.append([f"VAV-{300 + i}", f"{6 + 2 * (i % 3)}", str(cfm_max), str(cfm_max // 3), "HW" if i % 2 else "-",
                     f"{(i % 4) + 1}", f"RM {300 + i}"])
    return rows


CASES: list[Case] = [
    Case("ahu-ruled", [Sched("AIR HANDLING UNIT SCHEDULE",
                             ["TAG", "LOCATION", "CFM", "ESP (IN WC)", "MOTOR HP", "MANUFACTURER", "MODEL"],
                             [["AHU-1", "ROOF", "12000", "2.5", "15", "TRANE", "CSAA012"],
                              ["AHU-2", "ROOF", "8500", "2.0", "10", "TRANE", "CSAA008"],
                              ["AHU-3", "MECH 210", "4200", "1.5", "5", "DAIKIN", "CAH004"]])],
         note="clean ruled grid"),
    Case("vav-long", [Sched("VAV BOX SCHEDULE", ["TAG", "INLET (IN)", "MAX CFM", "MIN CFM", "REHEAT", "AHU ZONE", "SERVES"],
                            _vavs(22))],
         note="22 rows, ruled"),
    Case("pump-unruled", [Sched("PUMP SCHEDULE", ["TAG", "SERVICE", "GPM", "HEAD (FT)", "HP", "RPM"],
                                [["P-1", "CHW PRIMARY", "450", "60", "15", "1750"],
                                 ["P-2", "CHW PRIMARY", "450", "60", "15", "1750"],
                                 ["P-3", "HW", "220", "45", "5", "1750"]], ruled=False)],
         note="no ruling lines: columns by whitespace"),
    Case("fan-two-row-header", [Sched("EXHAUST FAN SCHEDULE",
                                      ["TAG", "CFM", "SP (IN WC)", "MOTOR HP", "MOTOR V", "MOTOR PH", "DRIVE"],
                                      [["EF-1", "1200", "0.75", "0.5", "115", "1", "DIRECT"],
                                       ["EF-2", "3400", "1.0", "1.5", "460", "3", "BELT"],
                                       ["EF-3", "600", "0.5", "0.25", "115", "1", "DIRECT"]],
                                      groups=[("", 3), ("MOTOR", 3), ("", 1)])],
         note="MOTOR spans HP / V / PH"),
    Case("two-schedules-sheet", [
        Sched("CHILLER SCHEDULE", ["TAG", "TONS", "KW/TON", "GPM", "MANUFACTURER"],
              [["CH-1", "500", "0.55", "1200", "CARRIER"], ["CH-2", "500", "0.55", "1200", "CARRIER"]]),
        Sched("COOLING TOWER SCHEDULE", ["TAG", "TONS", "GPM", "FAN HP"],
              [["CT-1", "600", "1500", "30"], ["CT-2", "600", "1500", "30"]])],
         landscape=True, side_by_side=True, clutter=True, note="two schedules side by side, notes, title block"),
    Case("dense-small", [Sched("FAN COIL UNIT SCHEDULE", ["TAG", "CFM", "MBH CLG", "MBH HTG", "GPM", "V/PH"],
                               [[f"FCU-{i}", str(300 + 25 * i), f"{8 + i * 0.5:.1f}", f"{10 + i * 0.4:.1f}",
                                 f"{1 + i * 0.1:.1f}", "208/1"] for i in range(1, 16)])],
         font_size=5.5, note="5.5 pt type, 15 rows"),
    Case("blanks-landscape", [Sched("UNIT HEATER SCHEDULE", ["TAG", "TYPE", "MBH", "CFM", "GAS", "REMARKS"],
                                    [["UH-1", "HW", "30", "500", "-", ""], ["UH-2", "GAS", "60", "900", "YES", "SEE NOTE 2"],
                                     ["UH-3", "ELEC", "15", "300", "-", ""]])],
         landscape=True, clutter=True, note="landscape, blank and '-' cells, notes"),
    Case("unruled-two-row", [Sched("ENERGY RECOVERY UNIT SCHEDULE",
                                   ["TAG", "SUPPLY CFM", "EXHAUST CFM", "WHEEL EFF %", "SUPPLY FAN HP", "EXHAUST FAN HP"],
                                   [["ERU-1", "5000", "4500", "72", "5", "3"], ["ERU-2", "3000", "2700", "70", "3", "2"]],
                                   groups=[("", 1), ("SUPPLY", 1), ("EXHAUST", 1), ("", 1), ("SUPPLY", 1), ("EXHAUST", 1)],
                                   ruled=False)],
         note="no ruling lines and a two-row header: the hard one"),
]


# ------------------------------------------------------------------ render


def _sub(header: str, groups: list[tuple[str, int]], i: int) -> str:
    """The second-row label of column i under a two-row header."""
    k = 0
    for label, span in groups:
        if k <= i < k + span:
            return header[len(label) + 1:] if label and header.startswith(label + " ") else header
        k += span
    return header


def render(case: Case) -> bytes:
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.pdfgen import canvas

    size = landscape(letter) if case.landscape else letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    w, h = size
    fs = case.font_size
    row_h = fs * 2.2
    x0, top = 36.0, h - 60
    across = len(case.schedules) if case.side_by_side else 1
    usable = (w - 2 * x0 - 40 * (across - 1)) / across  # every schedule fits on the sheet
    x = x0
    for si, s in enumerate(case.schedules):
        col_w = min(fs * 13, usable / len(s.headers))
        if case.side_by_side and si:
            x += usable + 40
        else:
            x = x0 if not case.side_by_side else x
        y = top if (case.side_by_side or si == 0) else top - si * 300
        c.setFont("Helvetica-Bold", fs + 2)
        c.drawString(x, y, s.title)
        y -= row_h * 1.2
        c.setFont("Helvetica-Bold", fs)
        n = len(s.headers)
        header_rows = 2 if s.groups else 1
        table_top = y + row_h * 0.7
        if s.groups:
            k = 0
            for label, span in s.groups:
                if label:
                    c.drawString(x + k * col_w + 2, y, label)
                k += span
            y -= row_h
        for i, head in enumerate(s.headers):
            c.drawString(x + i * col_w + 2, y, _sub(head, s.groups, i) if s.groups else head)
        y -= row_h
        c.setFont("Helvetica", fs)
        for r in s.rows:
            for i, v in enumerate(r):
                if v:
                    c.drawString(x + i * col_w + 2, y, v)
            y -= row_h
        if s.ruled:
            bottom = y + row_h * 0.7
            for i in range(n + 1):
                c.line(x + i * col_w, table_top, x + i * col_w, bottom)
            yy = table_top
            for _ in range(header_rows + len(s.rows) + 1):
                c.line(x, yy, x + n * col_w, yy)
                yy -= row_h
    if case.clutter:
        c.setFont("Helvetica", 7)
        notes = ["GENERAL NOTES:", "1. ALL EQUIPMENT SHALL BE INSTALLED PER MANUFACTURER'S INSTRUCTIONS.",
                 "2. PROVIDE GAS COCK AND UNION AT EACH UNIT. COORDINATE WITH PLUMBING.",
                 "3. VERIFY ALL DIMENSIONS IN FIELD."]
        for i, line in enumerate(notes):
            c.drawString(36, 150 - i * 10, line)
        c.rect(w - 230, 20, 200, 80)
        for i, line in enumerate(["PROJECT: MEDICAL OFFICE BUILDING", "SHEET: M-601", "MECHANICAL SCHEDULES",
                                  "DATE: 2026-03-01"]):
            c.drawString(w - 222, 85 - i * 16, line)
    c.showPage()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------------------- score


def norm(s: str | None) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip().upper()
    return "" if s in ("-", "—", "–") else s


def _title_key(t: str) -> str:
    return re.sub(r"[^A-Z]", "", norm(t).replace("SCHEDULE", ""))


def score_case(case: Case, predicted: list[dict]) -> dict:
    """predicted: [{title, headers, rows}]"""
    tp = n_pred = n_true = 0
    found = 0
    for s in case.schedules:
        n_true += sum(1 for r in s.rows for v in r if norm(v))
        match = next((p for p in predicted if _title_key(p.get("title") or "") == _title_key(s.title)), None)
        if match is None:
            continue
        found += 1
        heads = [norm(h) for h in match.get("headers") or []]
        truth = {norm(r[0]): {norm(hh): norm(v) for hh, v in zip(s.headers, r, strict=True)} for r in s.rows}
        for row in match.get("rows") or []:
            cells = {heads[i]: norm(v) for i, v in enumerate(row) if i < len(heads) and norm(v)}
            n_pred += len(cells)
            want = truth.get(norm(row[0]) if row else "")
            if want:
                tp += sum(1 for k, v in cells.items() if want.get(k) == v)
    for p in predicted:  # schedules that don't exist: every cell is a false positive
        if not any(_title_key(p.get("title") or "") == _title_key(s.title) for s in case.schedules):
            n_pred += sum(1 for r in p.get("rows") or [] for v in r if norm(v))
    return {"case": case.id, "schedules": len(case.schedules), "found": found, "true_cells": n_true,
            "predicted_cells": n_pred, "correct_cells": tp}


def totals(results: list[dict]) -> dict:
    t = {k: sum(r[k] for r in results) for k in ("schedules", "found", "true_cells", "predicted_cells", "correct_cells")}
    p = t["correct_cells"] / t["predicted_cells"] if t["predicted_cells"] else 0.0
    r = t["correct_cells"] / t["true_cells"] if t["true_cells"] else 0.0
    return {**t, "region_recall": round(t["found"] / t["schedules"], 3), "cell_precision": round(p, 3),
            "cell_recall": round(r, 3), "cell_f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}


def run(reader: str = "local", client=None) -> tuple[list[dict], dict]:
    from baler.lifecycle import schedules

    results = []
    for case in CASES:
        found, _info = schedules.extract(render(case), reader=reader, client=client)
        results.append(score_case(case, [{"title": s.title, "headers": s.headers, "rows": s.rows} for s in found]))
    return results, totals(results)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--reader", choices=("local", "vision"), default="local")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true", help="exit 1 if below BASELINE")
    args = ap.parse_args(argv)
    results, tot = run(args.reader)
    if args.json:
        print(json.dumps({"cases": results, "totals": tot}, indent=2))
    else:
        print(f"{'case':24} {'found':>5} {'true':>5} {'pred':>5} {'ok':>5}")
        for r in results:
            print(f"{r['case']:24} {r['found']:>2}/{r['schedules']:<2} {r['true_cells']:>5} {r['predicted_cells']:>5} "
                  f"{r['correct_cells']:>5}")
        print(f"region recall {tot['region_recall']}, cell precision {tot['cell_precision']}, "
              f"recall {tot['cell_recall']}, F1 {tot['cell_f1']}")
    if args.check:
        bad = [k for k, v in BASELINE.items() if tot[k] < v]
        if bad:
            print(f"below baseline: {', '.join(f'{k} {tot[k]} < {BASELINE[k]}' for k in bad)}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
