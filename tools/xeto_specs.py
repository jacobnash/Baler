"""Generate ontology/xeto-ph-specs.json: every spec in the Project Haystack
Xeto libraries mapped to the marker tags a record needs to fit it.

    python tools/xeto_specs.py <path to Project-Haystack/xeto checkout> [commit]

A spec's markers are its own (bare names in its body, and required
`name: Marker` slots) plus everything it inherits (`Name : Base & Other`).
Baler uses the table for Xeto spec names in filters (`ph::Ahu`,
`ph.points::ZoneAirTempSensor`, bare `Ahu`); see hisquery._spec. Non-marker
slots (choices, refs) aren't part of the fit; that's a structural
approximation of Xeto's full typing, documented where it's used.

The Xeto libraries are AFL-3.0 (Project Haystack); this table is derived
data, kept with a provenance header.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LIBS = ["sys", "ph", "ph.points", "ph.points.x", "ph.equips.x"]

_DECL = re.compile(r"^([A-Z][A-Za-z0-9_]*)\s*:\s*(.*)$")


def _strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        in_str = False
        for i, c in enumerate(line):
            if c == '"':
                in_str = not in_str
            elif not in_str and line.startswith("//", i):
                line = line[:i]
                break
        out.append(line)
    return "\n".join(out)


def _decls(text: str):
    """(name, bases, body) for each top-level declaration."""
    text = _strip_comments(text)
    i, n = 0, len(text)
    while i < n:
        line_end = text.find("\n", i)
        line_end = n if line_end == -1 else line_end
        line = text[i:line_end]
        m = _DECL.match(line) if line and not line[0].isspace() else None
        if not m:
            i = line_end + 1
            continue
        name, rest = m.group(1), m.group(2)
        # header runs to the first top-level '{' (the body) or end of line
        j = text.find(rest, i) if rest else line_end
        depth_angle = 0
        k = j
        body = ""
        while k < n:
            c = text[k]
            if c == "<":
                depth_angle += 1
            elif c == ">":
                depth_angle -= 1
            elif c == '"':
                k = text.find('"', k + 1)
            elif c == "{" and depth_angle == 0:
                depth, s = 1, k + 1
                k += 1
                while k < n and depth:
                    if text[k] == "{":
                        depth += 1
                    elif text[k] == "}":
                        depth -= 1
                    elif text[k] == '"':
                        k = text.find('"', k + 1)
                    k += 1
                body = text[s:k - 1]
                break
            elif c == "\n" and depth_angle == 0:
                break
            k += 1
        header = text[j:k] if body == "" else text[j:text.find("{", j)]
        header = re.sub(r"<[^>]*>", "", header)
        bases = [b.strip().split("::")[-1] for b in header.split("&") if re.match(r"\s*[A-Z]", b)]
        bases = [re.match(r"[A-Za-z0-9_]+", b).group(0) for b in bases if re.match(r"[A-Za-z0-9_]+", b)]
        yield name, bases, body
        i = k + 1


def _own_markers(body: str) -> set[str]:
    markers = set()
    depth = 0
    for raw in body.splitlines():
        line = raw.strip()
        if depth == 0 and line:
            m = re.fullmatch(r"([a-z][A-Za-z0-9_]*)(\s*<[^>]*>)?", line)
            if m:
                markers.add(m.group(1))
            m = re.fullmatch(r"([a-z][A-Za-z0-9_]*)\s*:\s*Marker(\s*<[^>]*>)?", line)
            if m:
                markers.add(m.group(1))
        depth += raw.count("{") - raw.count("}")
    return markers


def build(root: Path) -> dict[str, list[str]]:
    specs: dict[str, tuple[str, list[str], set[str]]] = {}  # simple name -> (lib, bases, own)
    for lib in LIBS:
        d = root / "src" / "xeto" / lib
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.xeto")):
            if f.name == "lib.xeto":
                continue
            for name, bases, body in _decls(f.read_text(encoding="utf-8")):
                specs.setdefault(name, (lib, bases, _own_markers(body)))

    cache: dict[str, set[str]] = {}

    def markers(name: str, seen=()) -> set[str]:
        if name in cache:
            return cache[name]
        if name not in specs or name in seen:
            return set()
        _lib, bases, own = specs[name]
        out = set(own)
        for b in bases:
            out |= markers(b, seen + (name,))
        cache[name] = out
        return out

    table = {}
    for name, (lib, _b, _o) in specs.items():
        if lib == "sys":
            continue
        m = markers(name)
        if m:
            table[f"{lib}::{name}"] = sorted(m)
    return dict(sorted(table.items()))


if __name__ == "__main__":
    root = Path(sys.argv[1])
    commit = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    table = build(root)
    out = {
        "source": {"repo": "https://github.com/Project-Haystack/xeto", "commit": commit,
                   "libs": [lib for lib in LIBS if lib != "sys"], "license": "AFL-3.0",
                   "generator": "tools/xeto_specs.py"},
        "specs": table,
    }
    Path("ontology/xeto-ph-specs.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"{len(table)} specs")
