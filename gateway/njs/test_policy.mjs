// Plain-`node`-runnable self-check for policy.js. `node gateway/njs/test_policy.mjs`.
import assert from "node:assert/strict";
import policy from "./policy.js";

let passed = 0;
function check(name, fn) {
    fn();
    passed++;
    console.log(`ok - ${name}`);
}

check("every row in ROUTES resolves against its own path", () => {
    for (const [method, re, resource, minRole] of policy.ROUTES) {
        // Build a concrete URI from the regex source by stripping
        // anchors/groups for the common `[^/]+` id-placeholder case.
        const sampleUri = re.source
            .replace(/^\^/, "")
            .replace("(?:[^/]+\\/)?", "demo/")
            .replace(/\(([A-Za-z_]+)\|[A-Za-z_|]+\)\$$/, "$1$")
            .replace(/\$$/, "")
            .replace(/\\\//g, "/")
            .replace(/\[\^\/\]\+/g, "abc123")
            .replace(/\(openapi\\\.yaml\|docs\)/, "openapi.yaml")
            .replace(/\[a-z_?\]\+/g, "fault");
        const resolved = policy.resolveRoute(method, sampleUri);
        assert.ok(resolved, `expected ${method} ${sampleUri} to resolve`);
        assert.equal(resolved.resource, resource);
        assert.equal(resolved.minRole, minRole);
    }
});

check("unmapped route resolves to null", () => {
    assert.equal(policy.resolveRoute("GET", "/ingest/does-not-exist"), null);
    assert.equal(policy.resolveRoute("PATCH", "/fault/rules"), null); // wrong method
});

check("public routes (openapi.yaml, docs) have no role requirement", () => {
    const spec = policy.resolveRoute("GET", "/fault/openapi.yaml");
    assert.ok(spec);
    assert.equal(spec.minRole, null);
    const docs = policy.resolveRoute("GET", "/validate/docs");
    assert.ok(docs);
    assert.equal(docs.minRole, null);
});

check("roleSatisfies respects the viewer < operator < admin ordinal", () => {
    assert.equal(policy.roleSatisfies("viewer", "viewer"), true);
    assert.equal(policy.roleSatisfies("viewer", "operator"), false);
    assert.equal(policy.roleSatisfies("operator", "viewer"), true);
    assert.equal(policy.roleSatisfies("admin", "operator"), true);
    assert.equal(policy.roleSatisfies("operator", "admin"), false);
});

check("roleSatisfies always grants public routes (minRole null) regardless of role", () => {
    assert.equal(policy.roleSatisfies("viewer", null), true);
    assert.equal(policy.roleSatisfies("nonsense-role", null), true);
});

check("service role satisfies operator-tier routes (same rank)", () => {
    assert.equal(policy.roleSatisfies("service", "operator"), true);
    assert.equal(policy.roleSatisfies("service", "admin"), false);
});

check("unknown role satisfies nothing but public routes", () => {
    assert.equal(policy.roleSatisfies("bogus", "viewer"), false);
});

check("no two rows share an identical (method, source) pair", () => {
    const seen = new Set();
    for (const [method, re] of policy.ROUTES) {
        const key = `${method} ${re.source}`;
        assert.ok(!seen.has(key), `duplicate route: ${key}`);
        seen.add(key);
    }
});

check("haystack routes: both URL layouts, reads are viewer, writes are operator", () => {
    for (const uri of ["/api/demo/read", "/api/read", "/api/site-1/hisRead", "/api/demo/defs", "/api/libs", "/api/demo/filetypes"]) {
        assert.equal(policy.resolveRoute("GET", uri).minRole, "viewer", uri);
        assert.equal(policy.resolveRoute("POST", uri).minRole, "viewer", uri);
    }
    assert.equal(policy.resolveRoute("POST", "/api/demo/hisWrite").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/api/pointWrite").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/api/demo/hisWrite"), null); // writes are POST-only
    assert.equal(policy.resolveRoute("GET", "/api/demo/evalAll"), null);  // unknown op: deny
    assert.equal(policy.resolveRoute("GET", "/api/a/b/read"), null);      // too deep
    assert.equal(policy.resolveRoute("GET", "/api/openapi.yaml").minRole, null);
});

check("mapping review: reading is viewer, answering is operator", () => {
    assert.equal(policy.resolveRoute("GET", "/ops/mapping/proposals").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/ops/mapping/summary").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/ops/mapping/decide").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/ops/mapping/proposals"), null);
});

check("naming conventions: list and preview viewer, save/apply/delete operator", () => {
    assert.equal(policy.resolveRoute("GET", "/ops/conventions").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/ops/conventions/preview").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/ops/conventions").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/ops/conventions/abc123/apply").minRole, "operator");
    assert.equal(policy.resolveRoute("DELETE", "/ops/conventions/abc123").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/ops/conventions/preview").resource, "points");
});

check("relationship proposals: reading is viewer, deciding is operator", () => {
    assert.equal(policy.resolveRoute("GET", "/ops/mapping/relationships").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/ops/mapping/relationships/decide").minRole, "operator");
});

check("bearerFromHeader accepts OAuth and Haystack forms only", () => {
    assert.equal(policy.bearerFromHeader("Bearer abc.def.ghi"), "abc.def.ghi");
    assert.equal(policy.bearerFromHeader("bearer abc.def.ghi"), "abc.def.ghi");
    assert.equal(policy.bearerFromHeader("BEARER authToken=abc.def.ghi"), "abc.def.ghi");
    assert.equal(policy.bearerFromHeader("Bearer abc def"), null);
    assert.equal(policy.bearerFromHeader("Basic dXNlcjpwYXNz"), null);
    assert.equal(policy.bearerFromHeader("SCRAM handshakeToken=x, data=y"), null);
    assert.equal(policy.bearerFromHeader(undefined), null);
});

check("isHaystackHandshake only on /api/ and only for HELLO/SCRAM", () => {
    assert.ok(policy.isHaystackHandshake("/api/demo/about", "HELLO username=YWRtaW4"));
    assert.ok(policy.isHaystackHandshake("/api/about", "SCRAM handshakeToken=x, data=y"));
    assert.ok(!policy.isHaystackHandshake("/ingest/ingest", "HELLO username=YWRtaW4"));
    assert.ok(!policy.isHaystackHandshake("/api/demo/read", "Bearer x"));
    assert.ok(!policy.isHaystackHandshake("/api/demo/read", undefined));
});


check("derivation target enable: the target URI's own slashes (decoded %2F) still route", () => {
    const uri = "/derivation/derivations/d1/targets/urn:equip:fbf/hospital-a/ahu-1/enable";
    assert.equal(policy.resolveRoute("POST", uri).minRole, "operator");
    assert.equal(policy.resolveRoute("GET", uri), null);
});

check("windrow (Devices page): reads for any role, every change admin-only", () => {
    assert.equal(policy.resolveRoute("GET", "/windrow/devices").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/windrow/connections").minRole, "admin");
    assert.equal(policy.resolveRoute("PATCH", "/windrow/devices/1234/credential").minRole, "admin");
    assert.equal(policy.resolveRoute("DELETE", "/windrow/connections/bacnet-1").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/windrow/learn").minRole, "admin");
    assert.equal(policy.resolveRoute("PATCH", "/windrow/modbus/connections/m1/credential").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/windrow/devices/d1/recheck-credentials").minRole, "admin");
    assert.equal(policy.resolveRoute("GET", "/windrow/"), null);
    // not used by the Devices page, so not reachable through the gateway at all
    assert.equal(policy.resolveRoute("POST", "/windrow/write"), null); // raw BACnet write, bypasses pointWrite
    assert.equal(policy.resolveRoute("POST", "/windrow/discover"), null);
    assert.equal(policy.resolveRoute("POST", "/windrow/modbus/discover"), null);
    assert.equal(policy.resolveRoute("GET", "/windrow/openapi.yaml").minRole, null); // the public spec row
});

check("post-deploy verification reads (Baler#178): GET only, freshness for viewers, gateway 5xx for operators org-wide", () => {
    assert.equal(policy.resolveRoute("GET", "/ops/freshness").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/ops/freshness").resource, "ops");
    assert.equal(policy.resolveRoute("GET", "/ops/gateway-errors").minRole, "operator");
    assert.equal(policy.roleSatisfies("viewer", policy.resolveRoute("GET", "/ops/gateway-errors").minRole), false);
    assert.equal(policy.scopeSatisfies("gateway", { site_ids: ["s1"] }), false);
    assert.equal(policy.scopeSatisfies("gateway", {}), true);
    assert.equal(policy.resolveRoute("POST", "/ops/freshness"), null);
    assert.equal(policy.resolveRoute("DELETE", "/ops/gateway-errors"), null);
});

check("ops edges (Connectors page): listing for any role, changes and scans admin-only", () => {
    assert.equal(policy.resolveRoute("GET", "/ops/edges").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/ops/edges").resource, "devices");
    assert.equal(policy.resolveRoute("POST", "/ops/edges/local/local/actions/list_devices").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/ops/edges/home/pi-1/actions/modbus_list_connections").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/ops/edges/local/local/actions/list_announced").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/ops/edges/home/pi-1/actions/list_announced_and_more").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/ops/edges/home/pi-1/actions/create_connection").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/ops/edges/home/pi-1/actions/discover").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/ops/edges/home/pi-1/actions/list_devices_and_more").minRole, "admin");
    assert.equal(policy.resolveRoute("GET", "/ops/edges/home/pi-1/actions/list_devices"), null);
});

check("windrow: site-restricted logins are kept out; whole-org logins and other resources aren't", () => {
    assert.equal(policy.scopeSatisfies("devices", { site_ids: ["s1"] }), false);
    assert.equal(policy.scopeSatisfies("devices", { site_ids: [] }), true);
    assert.equal(policy.scopeSatisfies("devices", {}), true);
    assert.equal(policy.scopeSatisfies("points", { site_ids: ["s1"] }), true);
});

check("user lifecycle routes: admin, except changing your own password", () => {
    assert.equal(policy.resolveRoute("GET", "/auth/users").minRole, "admin");
    assert.equal(policy.resolveRoute("DELETE", "/auth/users/u1").minRole, "admin");
    assert.equal(policy.resolveRoute("PUT", "/auth/users/u1/password").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/auth/me/password").minRole, "viewer");
});

check("unknown routes say where to look (#69)", () => {
    const api = policy.unknownRouteMessage("GET", "/api/derivation");
    assert.match(api, /^no such route: GET \/api\/derivation/);
    assert.match(api, /Haystack API/);
    assert.match(api, /did you mean \/derivation\/\.\.\.\? API reference: \/derivation\/docs/);
    assert.match(policy.unknownRouteMessage("GET", "/derivations"), /did you mean \/derivation\//);
    assert.match(policy.unknownRouteMessage("GET", "/derivation/derivations/abc"), /API reference: \/derivation\/docs/);
    assert.match(policy.unknownRouteMessage("GET", "/nope"), /all docs: \/docs/);
    assert.doesNotMatch(policy.unknownRouteMessage("GET", "/nope"), /did you mean/);
});

check("orphaned points: admin only", () => {
    assert.equal(policy.resolveRoute("GET", "/ops/orphans").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/ops/orphans/remove").minRole, "admin");
});

check("edge-node broker accounts: admin only", () => {
    assert.equal(policy.resolveRoute("GET", "/auth/broker-accounts").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/auth/broker-accounts").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/auth/broker-accounts/a1/rotate").minRole, "admin");
    assert.equal(policy.resolveRoute("DELETE", "/auth/broker-accounts/a1").minRole, "admin");
    assert.equal(policy.resolveRoute("PUT", "/auth/broker-accounts/a1"), null);
});

check("per-user session cutoff: parse denylist rows, reject older sessions only", () => {
    assert.deepEqual(policy.parseUserCutoff("user:abc:1700000000"), { key: "user:abc", epoch: 1700000000 });
    assert.equal(policy.parseUserCutoff("3f2a9c"), null);
    assert.equal(policy.sessionCutOff({ iat: 99, exp: 99 + 43200 }, "100"), true);
    assert.equal(policy.sessionCutOff({ iat: 100, exp: 100 + 43200 }, "100"), false); // a fresh login that second
    assert.equal(policy.sessionCutOff({ iat: 99 }, undefined), false);
    // The jti denylist applies to session tokens too (Haystack close), not only API keys.
    assert.equal(policy.jtiRevoked({ kind: "session", jti: "s-1" }, "1"), true);
    assert.equal(policy.jtiRevoked({ kind: "api_key", jti: "k-1" }, "1"), true);
    assert.equal(policy.jtiRevoked({ kind: "session", jti: "s-2" }, undefined), false);
    assert.equal(policy.jtiRevoked({ kind: "session" }, "1"), false);
    assert.equal(policy.sessionCutOff({ exp: 50 + 43200 }, "100"), true); // pre-iat token: iat = exp - TTL
    // #50: ms precision - a token from earlier in the cutoff's second is cut off,
    // one issued after it in the same second (a fresh login) isn't
    assert.deepEqual(policy.parseUserCutoff("user:abc:1700000000.512"), { key: "user:abc", epoch: 1700000000.512 });
    assert.equal(policy.sessionCutOff({ iat: 1700000000.2 }, "1700000000.512"), true);
    assert.equal(policy.sessionCutOff({ iat: 1700000000.6 }, "1700000000.512"), false);
    assert.equal(policy.sessionCutOff({ iat: 1700000000 }, "1700000000.512"), true); // old whole-second token
});

check("commissioning (/cx/): reads viewer, changes operator, archive admin, site logins allowed", () => {
    assert.equal(policy.resolveRoute("GET", "/cx/projects").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/runs").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/runs/r1").minRole, "viewer");
    assert.equal(policy.resolveRoute("PUT", "/cx/projects/p1/phases").minRole, "operator");
    assert.equal(policy.resolveRoute("DELETE", "/cx/projects/p1").minRole, "admin");
    assert.equal(policy.resolveRoute("GET", "/cx/openapi.yaml").minRole, null);
    assert.equal(policy.resolveRoute("DELETE", "/cx/runs/r1"), null);
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/runs/extra"), null);
    // Cx 5 UI reads
    assert.equal(policy.resolveRoute("GET", "/cx/runs/r1/report").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/gaps").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/cx/equipment-status").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/runs/r1/report"), null);
    // Cx 4 issues and risks
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/issues").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/issues").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/issues/import").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/issues/export").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/risks").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/runs/r1/issues").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/issues/i1").minRole, "viewer");
    assert.equal(policy.resolveRoute("PATCH", "/cx/issues/i1").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/issues/i1/events").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/issues/i1/risk").minRole, "admin");
    assert.equal(policy.resolveRoute("DELETE", "/cx/risks/k1").minRole, "admin");
    assert.equal(policy.resolveRoute("DELETE", "/cx/issues/i1"), null);
    // Baler#275 manual results and coverage
    assert.equal(policy.resolveRoute("POST", "/cx/test-runs/t1/manual-results").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/test-runs/t1/manual-results").minRole, "viewer");
    assert.equal(policy.resolveRoute("PATCH", "/cx/manual-results/m1").minRole, "operator");
    assert.equal(policy.resolveRoute("DELETE", "/cx/manual-results/m1"), null);
    assert.equal(policy.resolveRoute("GET", "/cx/manual-attachments/a1").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/coverage").minRole, "viewer");
    // Cx 6 procedures
    assert.equal(policy.resolveRoute("GET", "/cx/procedure-templates").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/procedures").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/procedures").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/procedures/x1").minRole, "viewer");
    assert.equal(policy.resolveRoute("PATCH", "/cx/procedures/x1").minRole, "operator");
    assert.equal(policy.resolveRoute("DELETE", "/cx/procedures/x1").minRole, "admin");
    assert.equal(policy.resolveRoute("GET", "/cx/procedures/x1/runs").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/procedures/x1/runs").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/test-runs/t1").minRole, "viewer");
    assert.equal(policy.resolveRoute("GET", "/cx/runs/r1/diff").minRole, "viewer");
    // Cx 7 spec and alignment
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/spec").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/spec/import").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/spec/extract").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/spec/documents").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/spec/review").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/spec-documents/d1").minRole, "viewer");
    assert.equal(policy.resolveRoute("PATCH", "/cx/spec-items/s1").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/alignment").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/alignment/decisions").minRole, "operator");
    assert.equal(policy.resolveRoute("DELETE", "/cx/mappings/m1").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/spec"), null);
    // Cx 8 faults
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/faults").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/faults/ingest").minRole, "operator");
    // Cx 9 active tests: writes admin, abort operator
    assert.equal(policy.resolveRoute("GET", "/cx/active/status").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/active/kill").minRole, "admin");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/active-runs").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/active-runs").minRole, "admin");
    assert.equal(policy.resolveRoute("GET", "/cx/active-runs/a1").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/active-runs/a1/confirm").minRole, "admin");
    assert.equal(policy.resolveRoute("POST", "/cx/active-runs/a1/abort").minRole, "operator");
    assert.equal(policy.resolveRoute("DELETE", "/cx/active-runs/a1"), null);
    // Cx 10 agent
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/agent/sessions").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/projects/p1/agent/sessions").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/agent-sessions/s1").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/agent-sessions/s1/messages").minRole, "operator");
    assert.equal(policy.resolveRoute("GET", "/cx/projects/p1/agent/proposals").minRole, "viewer");
    assert.equal(policy.resolveRoute("POST", "/cx/agent-proposals/x1/accept").minRole, "operator");
    assert.equal(policy.resolveRoute("POST", "/cx/agent-proposals/x1/reject").minRole, "operator");
    // per-project tenancy is checked by commissioning_api, so not WHOLE_ORG
    assert.equal(policy.scopeSatisfies("commissioning", { site_ids: ["s1"] }), true);
});

console.log(`\n${passed} passed`);
