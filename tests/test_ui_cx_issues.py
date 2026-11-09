"""The Cx page's issue actions on inconclusive results (Baler#341), run
under node against the real ui/cx_issues.js and ui/cx.js code: an
inconclusive result card offers "Open issue" worded for waiving or
deferring and opens a check-tied issue from it; the bulk action sends
include_inconclusive only when its checkbox is ticked (off by default);
a viewer gets neither. The API side (waive meets the gate, revoke
unmeets it) is test_commissioning_gates.py::test_ui_paths_for_inconclusive_issues_341.

There is no browser harness in this repo (Playwright is used only by
tools/live-check against a deployed install), so this drives the page's
own functions with a stub DOM, like test_ui_manual_drafts.py."""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

ROOT = pathlib.Path(__file__).resolve().parent.parent
ISSUES_JS = (ROOT / "ui" / "cx_issues.js").read_text()
CX_JS = (ROOT / "ui" / "cx.js").read_text()
HTML = (ROOT / "ui" / "cx.html").read_text()


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


HARNESS = """
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.hidden = false; }
  append(...cs) { this.children.push(...cs); }
  addEventListener(k, f) { this.listeners[k] = f; }
}
const document = { createElement: (tag) => new Node(tag) };
const calls = [];
const flashed = [];
let confirmText = null;
const confirm = (t) => { confirmText = t; return true; };
const alert = (t) => { throw new Error("alert: " + t); };
const checkbox = { checked: false };
const $ = (id) => (id === "issues-include-inconclusive" ? checkbox : {});
const flash = (_el, msg) => flashed.push(msg);
const cx = { me: { role: "operator" }, project: { id: "p1" }, run: { id: "r1" } };
const issueState = { list: [] };
const ISSUE_STATUS_LABEL = { open: "open", risk_accepted: "risk accepted" };
let opened = null;
async function loadIssues() {}
async function loadEvents() {}
function openIssue(id) { opened = id; }
async function cxApi(method, path, body) {
  calls.push({ method, path, body });
  if (path.endsWith("/issues") && path.startsWith("/runs/")) return { opened: [], already_tracked: 0 };
  return { id: "i-new" };
}
function buttons(card) {
  return card.children.flatMap((c) => c.children).filter((c) => c.tag === "button");
}
function cardFor(r) {
  const card = new Node("div");
  for (const act of cxResultActions) act(card, r);
  return card;
}
const out = {};
"""


def _run(script: str) -> dict:
    code = (HARNESS
            + _slice(CX_JS, "const ROLE_RANK", "\n") + "\n"
            + _slice(CX_JS, "function el(", "async function cxApi") + "\n"
            + _slice(CX_JS, "const can =", "function setUrl") + "\n"
            + _slice(ISSUES_JS, "const cxResultActions", "async function loadIssues") + "\n"
            + _slice(ISSUES_JS, "async function issuesFromRun", "async function exportIssues") + "\n"
            + script)
    res = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


RESULT = 'const inc = { status: "inconclusive", check_id: "cov", subject: "urn:point:fbf/a/ahu-1/rat" };\n'


def test_inconclusive_card_offers_an_issue_for_waiving_and_opens_one():
    got = _run(RESULT + """
(async () => {
  const [btn] = buttons(cardFor(inc));
  out.text = btn.textContent;
  out.title = btn.title;
  await btn.listeners.click();
  out.calls = calls;
  out.opened = opened;
  console.log(JSON.stringify(out));
})();
""")
    assert "waive" in got["text"].lower() and "defer" in got["text"].lower()
    assert "waive" in got["title"]
    assert got["calls"] == [{"method": "POST", "path": "/projects/p1/issues",
                             "body": {"from_result": {"run_id": "r1", "check_id": "cov",
                                                      "subject": "urn:point:fbf/a/ahu-1/rat"}}}]
    assert got["opened"] == "i-new"


def test_fail_still_offers_open_issue_and_pass_offers_nothing():
    got = _run("""
out.fail = buttons(cardFor({ status: "fail", check_id: "c", subject: "s" })).map((b) => b.textContent);
out.pass = buttons(cardFor({ status: "pass", check_id: "c", subject: "s" })).length;
out.na = buttons(cardFor({ status: "not_applicable", check_id: "c", subject: "s" })).length;
console.log(JSON.stringify(out));
""")
    assert got == {"fail": ["Open issue"], "pass": 0, "na": 0}


def test_tracked_inconclusive_shows_its_issue_state():
    got = _run(RESULT + """
issueState.list = [{ id: "i1", check_id: "cov", subject: inc.subject, status: "risk_accepted" }];
(async () => {
  const [btn] = buttons(cardFor(inc));
  out.text = btn.textContent;
  await btn.listeners.click();
  out.calls = calls.length;
  out.opened = opened;
  console.log(JSON.stringify(out));
})();
""")
    assert got == {"text": "Issue: risk accepted", "calls": 0, "opened": "i1"}


def test_viewer_gets_no_card_action():
    got = _run(RESULT + """
cx.me.role = "viewer";
out.inc = buttons(cardFor(inc)).length;
out.fail = buttons(cardFor({ status: "fail", check_id: "c", subject: "s" })).length;
console.log(JSON.stringify(out));
""")
    assert got == {"inc": 0, "fail": 0}


def test_bulk_sends_include_inconclusive_only_when_ticked():
    got = _run("""
(async () => {
  await issuesFromRun();
  out.unticked = { body: calls[0].body, confirm: confirmText };
  checkbox.checked = true;
  await issuesFromRun();
  out.ticked = { body: calls[1].body, confirm: confirmText, path: calls[1].path };
  console.log(JSON.stringify(out));
})();
""")
    assert got["unticked"]["body"] == {}
    assert "inconclusive" not in got["unticked"]["confirm"]
    assert got["ticked"] == {"body": {"include_inconclusive": True}, "path": "/runs/r1/issues",
                             "confirm": got["ticked"]["confirm"]}
    assert "inconclusive" in got["ticked"]["confirm"]


def test_bulk_checkbox_is_unchecked_by_default_and_operator_only():
    [tag] = re.findall(r'<label[^>]*>\s*<input[^>]*id="issues-include-inconclusive"[^>]*>', HTML)
    assert 'data-min-role="operator"' in tag
    assert "checked" not in tag.split('id="issues-include-inconclusive"')[1]
    assert 'type="checkbox"' in tag
    assert re.search(r'id="issues-from-run-btn"[^>]*data-min-role="operator"', HTML)


def test_viewer_has_the_bulk_controls_hidden():
    """applyRoles (cx.js) hides every data-min-role element the role can't use."""
    got = _run("""
const btn = new Node("button"); btn.dataset.minRole = "operator";
const label = new Node("label"); label.dataset.minRole = "operator";
const root = { querySelectorAll: () => [btn, label] };
cx.me.role = "viewer";
applyRoles(root);
out.viewer = [btn.hidden, label.hidden];
cx.me.role = "operator";
applyRoles(root);
out.operator = [btn.hidden, label.hidden];
console.log(JSON.stringify(out));
""")
    assert got == {"viewer": [True, True], "operator": [False, False]}


def test_issue_lifecycle_copy_is_resolve_then_close():
    section = _slice(HTML, '<section id="issues-section">', "</section>")
    assert "closes only when a later run passes" not in section
    assert "ready for retest" in section and "closes it" in section
