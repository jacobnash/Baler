"""The phone's offline draft queue for manual results (Baler#275), run
under node: a result saved offline stays on the device, is sent on
reconnect with its client_id (so the server stores it once however often
it's resent), is removed only once the server has it, and a refused one
is shown, never resent in a loop."""

import json
import pathlib
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "ui" / "cx_procedures.js").read_text()

HARNESS = """
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
const navigator = { onLine: true };
const statusEl = { textContent: "" };
const $ = (id) => (id === "manual-drafts" ? statusEl : null);
const procState = { openRun: null };
const showTestRun = async () => {};
const server = { rows: {}, posts: 0, mode: "offline" };
async function cxApi(method, path, body) {
  server.posts++;
  if (server.mode === "offline") throw new TypeError("Failed to fetch");
  if (server.mode === "refuse") { const e = new Error("step_id must be a step"); e.status = 400; throw e; }
  server.rows[body.client_id] = body;  // the server's client_id index: one row per entry
  if (server.mode === "lost-reply") { server.mode = "ok"; throw new TypeError("connection dropped after the write"); }
  return body;
}
"""


def _run(script: str) -> dict:
    start, end = JS.index("const DRAFTS_KEY"), JS.index("function readFile")
    out = subprocess.run(["node", "-e", HARNESS + JS[start:end] + script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_offline_entry_waits_then_syncs_once():
    got = _run("""
(async () => {
  saveDrafts([...loadDrafts(), { test_run_id: "t1", body: { equip_uri: "e", step_id: "s", status: "pass", client_id: newClientId() } }]);
  await syncDrafts();                 // offline: kept
  const offline = loadDrafts().length;
  server.mode = "lost-reply";         // reconnect; the first send lands but its answer is lost
  await syncDrafts();
  const afterDrop = loadDrafts().length;
  await syncDrafts();                 // resent with the same client_id
  console.log(JSON.stringify({ offline, afterDrop, left: loadDrafts().length, stored: Object.keys(server.rows).length,
                               status: statusEl.textContent }));
})();""")
    assert got == {"offline": 1, "afterDrop": 1, "left": 0, "stored": 1, "status": "Every result is sent."}


def test_a_refused_draft_is_shown_and_not_resent():
    got = _run("""
(async () => {
  server.mode = "refuse";
  saveDrafts([{ test_run_id: "t1", body: { client_id: "web:1234567890" } }]);
  await syncDrafts();
  const posts = server.posts;
  await syncDrafts();
  console.log(JSON.stringify({ posts, again: server.posts, left: loadDrafts().length, status: statusEl.textContent }));
})();""")
    assert got["posts"] == 1 and got["again"] == 1 and got["left"] == 1
    assert "refused: step_id must be a step" in got["status"]


def test_drafts_survive_a_reload():
    got = _run("""
saveDrafts([{ test_run_id: "t1", body: { client_id: "web:abcdefgh" } }]);
const persisted = JSON.parse(store["cx-manual-drafts"]);
console.log(JSON.stringify({ n: persisted.length, id: persisted[0].body.client_id }));""")
    assert got == {"n": 1, "id": "web:abcdefgh"}
