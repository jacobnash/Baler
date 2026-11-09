// `node --test ui/` - runs apibase.js the way a browser does (a classic
// script defining globals) against a fake location, and checks which
// ?param= API base overrides it accepts.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
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
    assert.equal(apiBase("api", "/api"), ORIGIN + "/api");
    assert.deepEqual(warnings, []);
  }
});

test("same-origin paths are accepted", () => {
  for (const [value, want] of [["/api", "/api"], ["/x/y", "/x/y"], ["/x/y/", "/x/y"], ["/api-2_v.1", "/api-2_v.1"]]) {
    const { apiBase, warnings } = load(withParam(value));
    assert.equal(apiBase("api", "/fallback"), ORIGIN + want, value);
    assert.deepEqual(warnings, [], value);
  }
});

test("the right parameter is read", () => {
  const { apiBase } = load("?ingest=%2Fmy-ingest&ops=%2Fmy-ops");
  assert.equal(apiBase("ingest", "/ingest"), ORIGIN + "/my-ingest");
  assert.equal(apiBase("ops", "/ops"), ORIGIN + "/my-ops");
  assert.equal(apiBase("cx", "/cx"), ORIGIN + "/cx");
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
  "/api?x=1",
  "/api#x",
];

test("anything but a same-origin path falls back with a warning", () => {
  for (const value of REJECTED) {
    const { apiBase, warnings } = load(withParam(value));
    assert.equal(apiBase("api", "/api"), ORIGIN + "/api", JSON.stringify(value));
    assert.equal(warnings.length, 1, JSON.stringify(value));
  }
});

test("a raw (unencoded) query string is handled the same way", () => {
  for (const search of ["?api=//other.example", "?api=https://other.example", "?api=%2F%2Fother.example", "?api=/%2F%2Fother"]) {
    const { apiBase } = load(search);
    assert.equal(apiBase("api", "/api"), ORIGIN + "/api", search);
  }
});

test("the result is always an absolute URL on the page's own origin", () => {
  const values = ["/api", "/x/y", "/a/../../b", "/./c", "/a:b", "/@other.example", "/x/y///"];
  for (const value of values.concat(REJECTED)) {
    const { apiBase } = load(withParam(value));
    const got = apiBase("api", "/api");
    assert.ok(got.startsWith(ORIGIN + "/"), `${JSON.stringify(value)} -> ${got}`);
    assert.equal(new URL(got).origin, ORIGIN, JSON.stringify(value));
    assert.ok(!got.endsWith("/"), `${JSON.stringify(value)} -> ${got} (callers append paths)`);
    assert.equal(new URL(got + "/sources").origin, ORIGIN, JSON.stringify(value));
  }
  assert.equal(load(withParam("/a/../../b")).apiBase("api", "/api"), ORIGIN + "/b");
  assert.equal(load(withParam("/x/y///")).apiBase("api", "/api"), ORIGIN + "/x/y");
});

test("the host comes from location.origin, whatever the origin is", () => {
  const warnings = [];
  const ctx = { URL, URLSearchParams, location: { origin: "http://localhost:8080", search: "?ops=%2Fops2" }, console: { warn: (m) => warnings.push(m) } };
  vm.runInNewContext(SRC, ctx);
  assert.equal(ctx.apiBase("ops", "/ops"), "http://localhost:8080/ops2");
  assert.equal(ctx.apiBase("auth", "/auth"), "http://localhost:8080/auth");
});

test("every page script takes its API base through apiBase()", () => {
  // Guard against a new page reintroducing `params.get("ingest") || "/ingest"`.
  const dir = new URL("./", import.meta.url);
  const names = ["api", "auth", "ingest", "ops", "fault", "cx", "derivation", "windrow", "validate"];
  const re = new RegExp(`\\.get\\(["'](?:${names.join("|")})["']\\)`);
  // Every page script in ui/, not a fixed list, so a new page is covered.
  const scripts = readdirSync(dir).filter((n) => n.endsWith(".js") && n !== "apibase.js").map((n) => n.slice(0, -3));
  for (const f of scripts) {
    const src = readFileSync(new URL(`${f}.js`, dir), "utf8");
    assert.ok(!re.test(src), `${f}.js reads an API base from the query string without apiBase()`);
  }
});

test("every page loads apibase.js before auth.js", () => {
  const dir = new URL("./", import.meta.url);
  // Every page that loads auth.js, not a fixed list, so a new page is covered.
  const pages = readdirSync(dir).filter((n) => n.endsWith(".html")).map((n) => n.slice(0, -5));
  for (const f of pages) {
    const html = readFileSync(new URL(`${f}.html`, dir), "utf8");
    if (!html.includes('<script src="auth.js"></script>')) continue;
    const a = html.indexOf('<script src="apibase.js"></script>');
    const b = html.indexOf('<script src="auth.js"></script>');
    assert.ok(a >= 0 && a < b, `${f}.html must load apibase.js before auth.js`);
  }
});
