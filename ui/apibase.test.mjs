// `node --test ui/` - runs apibase.js the way a browser does (a classic
// script defining globals) against a fake location, and checks which
// ?param= API base overrides it accepts.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("./apibase.js", import.meta.url), "utf8");
const ORIGIN = "https://baler.example";

function load(search) {
  const warnings = [];
  const ctx = {
    URL,
    URLSearchParams,
    location: { origin: ORIGIN, search },
    console: { warn: (m) => warnings.push(m) },
  };
  vm.runInNewContext(SRC, ctx);
  return { apiBase: ctx.apiBase, warnings };
}

const withParam = (value) => "?api=" + encodeURIComponent(value);

test("no override, or an empty one, uses the fallback silently", () => {
  for (const search of ["", "?other=1", "?api="]) {
    const { apiBase, warnings } = load(search);
    assert.equal(apiBase("api", "/api"), "/api");
    assert.deepEqual(warnings, []);
  }
});

test("same-origin paths are accepted", () => {
  for (const [value, want] of [["/api", "/api"], ["/x/y", "/x/y"], ["/x/y/", "/x/y"], ["/api-2_v.1", "/api-2_v.1"]]) {
    const { apiBase, warnings } = load(withParam(value));
    assert.equal(apiBase("api", "/fallback"), want, value);
    assert.deepEqual(warnings, [], value);
  }
});

test("the right parameter is read", () => {
  const { apiBase } = load("?ingest=%2Fmy-ingest&ops=%2Fmy-ops");
  assert.equal(apiBase("ingest", "/ingest"), "/my-ingest");
  assert.equal(apiBase("ops", "/ops"), "/my-ops");
  assert.equal(apiBase("cx", "/cx"), "/cx");
});

const REJECTED = [
  "https://other.example",
  "http://other.example/api",
  "//other.example",
  "///other.example",
  "/\\other.example",
  "\\\\other.example",
  "javascript:alert(1)",
  "data:text/plain,x",
  "api",
  "other.example/api",
  "%2F%2Fother.example",
  "/%2F%2Fother.example",
  "/%5Cother.example",
  "/",
  "//",
  " /api",
  "/api ",
  "/a pi",
  "/api\t",
  "\n/api",
  "/api\r\n//other.example",
  "/ap\u0000i",
  "/ap\u007fi",
  "/ap\u2028i",
  "/api%0a",
];

test("anything but a same-origin path falls back with a warning", () => {
  for (const value of REJECTED) {
    const { apiBase, warnings } = load(withParam(value));
    assert.equal(apiBase("api", "/api"), "/api", JSON.stringify(value));
    assert.equal(warnings.length, 1, JSON.stringify(value));
  }
});

test("a raw (unencoded) query string is handled the same way", () => {
  for (const search of ["?api=//other.example", "?api=https://other.example", "?api=%2F%2Fother.example", "?api=/%2F%2Fother"]) {
    const { apiBase } = load(search);
    assert.equal(apiBase("api", "/api"), "/api", search);
  }
});

test("every page script takes its API base through apiBase()", () => {
  // Guard against a new page reintroducing `params.get("ingest") || "/ingest"`.
  const dir = new URL("./", import.meta.url);
  const names = ["api", "auth", "ingest", "ops", "fault", "cx", "derivation", "windrow", "validate"];
  const re = new RegExp(`\\.get\\(["'](?:${names.join("|")})["']\\)`);
  for (const f of ["alarms", "app", "auth", "connectors", "cx", "derivations", "devices", "his", "mapping", "ops", "orphans", "record_edit", "sources"]) {
    const src = readFileSync(new URL(`${f}.js`, dir), "utf8");
    assert.ok(!re.test(src), `${f}.js reads an API base from the query string without apiBase()`);
  }
});

test("every page loads apibase.js before auth.js", () => {
  const dir = new URL("./", import.meta.url);
  for (const f of ["alarms", "connectors", "cx", "derivations", "his", "index", "mapping", "ops"]) {
    const html = readFileSync(new URL(`${f}.html`, dir), "utf8");
    const a = html.indexOf('<script src="apibase.js"></script>');
    const b = html.indexOf('<script src="auth.js"></script>');
    assert.ok(a >= 0 && a < b, `${f}.html must load apibase.js before auth.js`);
  }
});
