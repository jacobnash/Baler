"""The equipment tree's point history for a two-state status (Baler#81).

A BACnet binary-value arrived as text "active"/"inactive": no chart, and a
History list of every 5 s sample. MQTT ingest now stores those as Bools
(test_mqtt_listener.py); the tree charts Bools - and the old text - as an
off/on step line and collapses runs of one value in the list. These run
ui/app.js's and ui/chart.js's own code under node."""

import json
import pathlib
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "ui" / "app.js").read_text()
CHART_JS = (ROOT / "ui" / "chart.js").read_text()


def _node(script: str):
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _app_snippet() -> str:
    start = APP_JS.index("const BINARY_TEXT")
    end = APP_JS.index("function valueRunsList")
    return APP_JS[start:end]


def test_runs_of_one_value_collapse_and_text_states_match_bools():
    history = [
        {"ts": 0, "value": "active"}, {"ts": 5, "value": "active"}, {"ts": 10, "value": True},
        {"ts": 15, "value": False}, {"ts": 20, "value": "inactive"}, {"ts": 25, "value": True},
    ]
    runs = _node(_app_snippet() + f"""
const h = {json.dumps(history)};
console.log(JSON.stringify({{
  twoState: h.every((r) => isTwoState(r.value)),
  runs: valueRuns(h).map((r) => [r.key, r.start, r.end, r.count]),
  notStates: ["constructor", "toString", 1, "standby"].map(isTwoState),
}}));""")
    assert runs["twoState"] is True
    assert runs["runs"] == [[1, 0, 10, 3], [0, 15, 20, 2], [1, 25, 25, 1]]
    assert runs["notStates"] == [False, False, False, False]


def test_text_values_collapse_by_value():
    history = [{"ts": t, "value": v} for t, v in enumerate(["auto", "auto", "hand", "auto"])]
    runs = _node(_app_snippet() + f"console.log(JSON.stringify(valueRuns({json.dumps(history)}).map((r) => [r.key, r.count])));")
    assert runs == [["auto", 2], ["hand", 1], ["auto", 1]]


def test_two_state_chart_is_a_step_line_on_a_fixed_off_on_axis():
    """All-on history used to collapse min == max onto the bottom edge; the
    axis is 0..1 labelled off/on, and each change steps."""
    script = CHART_JS + """
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
globalThis.document = { documentElement: {} };
const calls = [];
const ctx = new Proxy({}, {
  get: (t, k) => (k in t ? t[k] : (...args) => calls.push([k, ...args])),
  set: (t, k, v) => { t[k] = v; return true; },
});
const canvas = { width: 640, height: 220, getContext: () => ctx };
const run = (values) => {
  calls.length = 0;
  drawLineChart(canvas, values.map((v, i) => ({ ts: i * 5, value: v })), 30, "", { states: ["off", "on"] });
  return {
    text: calls.filter((c) => c[0] === "fillText").map((c) => c[1]).slice(0, 2),
    lines: calls.filter((c) => c[0] === "lineTo").map((c) => [Math.round(c[1]), Math.round(c[2])]),
  };
};
console.log(JSON.stringify({ allOn: run([1, 1, 1]), flip: run([0, 1]) }));
"""
    got = _node(script)
    assert got["allOn"]["text"] == ["on", "off"]
    assert {y for _, y in got["allOn"]["lines"]} == {30}  # top of the plot: "on"
    # 0 -> 1: across at the old value (bottom, y=190), then up to the new one
    (x1, y1), (x2, y2) = got["flip"]["lines"]
    assert x1 == x2 and (y1, y2) == (190, 30)
