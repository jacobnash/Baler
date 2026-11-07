"""The equipment tree groups by canonical Brick class (Baler#76).

ui/app.js grouped equips by their raw brickClass, or by a Haystack marker
label when they had none, so `AHU` (Brick's alias, or an `ahu` marker) and
`Air_Handling_Unit` became two groups of the same equipment. app.js now
folds Brick's aliases and maps markers to the class they mean; these tests
pin its alias table to the vendored ontology and run its grouping code."""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from baler import brick_vocab

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "ui" / "app.js").read_text()


def _js_aliases() -> dict[str, str]:
    body = re.search(r"const EQUIP_CLASS_ALIASES = \{(.*?)\};", APP_JS, re.DOTALL).group(1)
    return dict(re.findall(r"(\w+): \"(\w+)\"", body))


def _js_kind_tags() -> dict[str, str]:
    body = re.search(r"const EQUIP_KIND_TAGS = \[(.*?)\];", APP_JS, re.DOTALL).group(1)
    return dict(re.findall(r'\["(\w+)", "(\w+)"\]', body))


def test_alias_table_matches_brick():
    vocab = brick_vocab.load()
    equipment = vocab.descendants("Equipment")
    expected = {a: c for a, c in vocab.aliases().items() if a in equipment}
    assert expected["AHU"] == "Air_Handling_Unit" and expected["VAV"] == "Variable_Air_Volume_Box"
    assert _js_aliases() == expected


def test_marker_groups_are_canonical_brick_classes():
    vocab = brick_vocab.load()
    aliases = vocab.aliases()
    tags = _js_kind_tags()
    assert tags["ahu"] == "Air_Handling_Unit" and tags["vav"] == "Variable_Air_Volume_Box"
    for tag, cls in tags.items():
        assert cls in vocab.classes, f"{tag} -> {cls} is not a Brick class"
        assert cls not in aliases, f"{tag} -> {cls} is an alias, not the canonical class"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_tree_groups_aliases_and_markers_together():
    start = APP_JS.index("const EQUIP_CLASS_ALIASES")
    end = APP_JS.index("\n", APP_JS.index("const classLabel"))
    m = {"_kind": "marker"}
    recs = [
        {"brickClass": "AHU"}, {"brickClass": "Air_Handling_Unit"}, {"ahu": m},
        {"brickClass": "VAV"}, {"vav": m, "equip": m}, {"brickClass": "Variable_Air_Volume_Box"},
        {"brickClass": "Weather_Station"}, {"equip": m},
    ]
    script = APP_JS[start:end] + f"""
const counts = {{}};
for (const rec of {json.dumps(recs)}) {{
  const kind = equipKind(rec);
  const label = kind ? classLabel(kind) : "Unclassified";
  counts[label] = (counts[label] || 0) + 1;
}}
console.log(JSON.stringify(counts));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == {
        "Air Handling Unit": 3,
        "Variable Air Volume Box": 3,
        "Weather Station": 1,
        "Unclassified": 1,
    }
