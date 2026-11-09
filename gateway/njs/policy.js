/*
 * The whole access policy, in one flat, reviewable table - the point of
 * "very fine control in the gateway". Pure data/logic, no nginx globals,
 * so it's plain-`node`-runnable (see test_policy.mjs).
 *
 * Deliberately no separate role->resource permission matrix on top of
 * this: each row's minRole already states exactly who can hit it, and a
 * second matrix encoding the same fact a different way is the first
 * thing that goes stale when a route is added. Enumerated straight from
 * openapi.yaml / fault-api-openapi.yaml / derivation-api-openapi.yaml /
 * validate-api-openapi.yaml - keep this table in sync with those specs,
 * not the other way around.
 *
 * Paths are matched as seen at the gateway (with the service prefix),
 * not stripped - one row is the whole story for a route, no separate
 * "which service" lookup needed to read it.
 */

// viewer < operator < admin. "service" (API-key class) is checked by
// presence against a route's minRole exactly like any other role - it's
// not a rank above admin, just a distinct credential class.
const ROLE_RANK = { viewer: 1, operator: 2, admin: 3, service: 2 };

// [method, path regex, resource, minRole]. `resource: null` / `minRole:
// null` marks a route as public - no token required at all.
const ROUTES = [
  // ingest_api
  ["POST",   /^\/ingest\/ingest$/,                 "points", "operator"],
  ["POST",   /^\/ingest\/tags$/,                    "points", "operator"],
  ["POST",   /^\/ingest\/equip\/merge$/,             "points", "operator"],
  ["POST",   /^\/ingest\/part$/,                     "points", "operator"],
  ["GET",    /^\/ingest\/history$/,                  "points", "viewer"],
  ["DELETE", /^\/ingest\/history$/,                  "points", "operator"],
  ["GET",    /^\/ingest\/his$/,                      "points", "viewer"],
  // hand edits to equip/point records (Baler#19), logged in record_edits
  ["POST",   /^\/ingest\/records$/,                   "points", "operator"],
  ["PATCH",  /^\/ingest\/records$/,                   "points", "operator"],
  ["DELETE", /^\/ingest\/records$/,                   "points", "admin"],
  ["GET",    /^\/ingest\/records\/edits$/,            "points", "viewer"],

  // fault_api
  ["POST",   /^\/fault\/rules$/,                     "rules", "operator"],
  ["GET",    /^\/fault\/rules$/,                     "rules", "viewer"],
  ["DELETE", /^\/fault\/rules\/[^/]+$/,               "rules", "operator"],
  ["POST",   /^\/fault\/webhooks$/,                  "webhooks", "operator"],
  ["GET",    /^\/fault\/webhooks$/,                  "webhooks", "viewer"],
  ["DELETE", /^\/fault\/webhooks\/[^/]+$/,            "webhooks", "operator"],
  ["POST",   /^\/fault\/webhooks\/[^/]+\/enable$/,     "webhooks", "operator"],
  ["GET",    /^\/fault\/faults$/,                    "faults", "viewer"],
  ["POST",   /^\/fault\/faults\/[^/]+\/ack$/,          "faults", "operator"],
  ["POST",   /^\/fault\/faults\/[^/]+\/snooze$/,       "faults", "operator"],

  // derivation_api
  ["POST",   /^\/derivation\/derivations$/,                              "derivations", "operator"],
  ["GET",    /^\/derivation\/derivations$/,                              "derivations", "viewer"],
  ["DELETE", /^\/derivation\/derivations\/[^/]+$/,                        "derivations", "operator"],
  ["POST",   /^\/derivation\/derivations\/test$/,                         "derivations", "operator"],
  ["POST",   /^\/derivation\/derivations\/dry-run$/,                      "derivations", "operator"],
  ["GET",    /^\/derivation\/derivations\/[^/]+\/targets$/,                "derivations", "viewer"],
  // The target is a URI with "/" in it; clients send %2F, and r.uri here is decoded.
  ["POST",   /^\/derivation\/derivations\/[^/]+\/targets\/.+\/enable$/,      "derivations", "operator"],

  // validate_api
  ["POST",   /^\/validate\/validate$/,               "validate", "viewer"],

  // ops_api
  ["GET",    /^\/ops\/status$/,                      "ops", "viewer"],
  // Post-deploy verification (Baler#178, tools/live-check): read-only.
  // Freshness is site-scoped by ops_api like /ops/status; the gateway's
  // own 5xx log spans every org and site, so it is whole-org and operator.
  ["GET",    /^\/ops\/freshness$/,                   "ops", "viewer"],
  ["GET",    /^\/ops\/gateway-errors$/,              "gateway", "operator"],
  // source registry (Baler#20): settings include credentials - admin only
  ["GET",    /^\/ops\/sources$/,                     "ops", "admin"],
  ["GET",    /^\/ops\/sources\/kinds$/,               "ops", "admin"],
  ["POST",   /^\/ops\/sources$/,                     "ops", "admin"],
  ["PATCH",  /^\/ops\/sources\/[^/]+$/,                "ops", "admin"],
  ["DELETE", /^\/ops\/sources\/[^/]+$/,                "ops", "admin"],
  ["GET",    /^\/ops\/orphans$/,                     "ops", "admin"],
  ["POST",   /^\/ops\/orphans\/remove$/,              "ops", "admin"],
  // Mapping review queue (Baler#184): reading the queue and the "how much
  // is understood" summary is viewer; answering (accept, escalate, site
  // visit) writes classes and is operator. ops_api scopes both to the
  // caller's sites.
  ["GET",    /^\/ops\/mapping\/proposals$/,            "points", "viewer"],
  ["GET",    /^\/ops\/mapping\/summary$/,              "points", "viewer"],
  ["POST",   /^\/ops\/mapping\/decide$/,               "points", "operator"],
  // Relationship proposals (Baler#187): equipment groupings and feeds.
  ["GET",    /^\/ops\/mapping\/relationships$/,        "points", "viewer"],
  ["POST",   /^\/ops\/mapping\/relationships\/decide$/, "points", "operator"],
  // Site naming conventions (Baler#186): listing and previewing (a dry run,
  // POST for its body) are viewer; saving, applying and deleting are operator.
  ["GET",    /^\/ops\/conventions$/,                    "points", "viewer"],
  ["POST",   /^\/ops\/conventions\/preview$/,           "points", "viewer"],
  ["POST",   /^\/ops\/conventions$/,                    "points", "operator"],
  ["POST",   /^\/ops\/conventions\/[^/]+\/apply$/,      "points", "operator"],
  ["DELETE", /^\/ops\/conventions\/[^/]+$/,             "points", "operator"],
  // Connectors page: edge nodes and their devices/connections, local or
  // remote (baler.edges). Listing is read-only; every other action changes
  // an edge node (or scans its network) and is admin. Whole-org, like the
  // old /windrow routes (WHOLE_ORG devices).
  ["GET",    /^\/ops\/edges$/,                        "devices", "viewer"],
  ["POST",   /^\/ops\/edges\/[^/]+\/[^/]+\/actions\/(list_devices|list_connections|modbus_list_connections|list_announced)$/, "devices", "viewer"],
  ["POST",   /^\/ops\/edges\/[^/]+\/[^/]+\/actions\/[a-z_]+$/, "devices", "admin"],

  // Building lifecycle record (epic #221), served by ops_api from
  // baler.lifecycle. Reads viewer, changes operator; ops_api also keeps
  // each org's rows to itself and positions to the caller's sites.
  // Document store (#201): deleting (a soft delete) is admin.
  ["GET",    /^\/ops\/documents$/,                       "documents", "viewer"],
  ["POST",   /^\/ops\/documents$/,                       "documents", "operator"],
  ["GET",    /^\/ops\/documents\/search$/,               "documents", "viewer"],
  ["GET",    /^\/ops\/documents\/kinds$/,                "documents", "viewer"],
  ["GET",    /^\/ops\/documents\/[^/]+$/,                "documents", "viewer"],
  ["PATCH",  /^\/ops\/documents\/[^/]+$/,                "documents", "operator"],
  ["DELETE", /^\/ops\/documents\/[^/]+$/,                "documents", "admin"],
  ["GET",    /^\/ops\/documents\/[^/]+\/(content|verify|audit)$/, "documents", "viewer"],
  ["GET",    /^\/ops\/documents\/[^/]+\/pages\/[^/]+$/,     "documents", "viewer"],
  ["POST",   /^\/ops\/documents\/[^/]+\/links$/,          "documents", "operator"],
  ["DELETE", /^\/ops\/documents\/[^/]+\/links\/[^/]+$/,   "documents", "operator"],
  // Extraction from documents (#202): proposals only; a person confirms each.
  ["POST",   /^\/ops\/documents\/[^/]+\/extract$/,        "documents", "operator"],
  ["GET",    /^\/ops\/documents\/[^/]+\/proposals$/,      "documents", "viewer"],
  ["GET",    /^\/ops\/document-proposals$/,              "documents", "viewer"],
  ["POST",   /^\/ops\/document-proposals\/[^/]+\/decide$/, "documents", "operator"],

  // commissioning_api (Baler#37): reads viewer, changes operator,
  // archiving a project admin. The backend also checks the caller's org
  // and sites against the project itself.
  ["GET",    /^\/cx\/projects$/,                              "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects$/,                              "commissioning", "operator"],
  ["GET",    /^\/cx\/projects\/[^/]+$/,                        "commissioning", "viewer"],
  ["PATCH",  /^\/cx\/projects\/[^/]+$/,                        "commissioning", "operator"],
  ["DELETE", /^\/cx\/projects\/[^/]+$/,                        "commissioning", "admin"],
  ["PUT",    /^\/cx\/projects\/[^/]+\/phases$/,                "commissioning", "operator"],
  ["POST",   /^\/cx\/projects\/[^/]+\/phase$/,                 "commissioning", "operator"],
  ["GET",    /^\/cx\/projects\/[^/]+\/gate$/,                  "commissioning", "viewer"],
  ["GET",    /^\/cx\/projects\/[^/]+\/report$/,                "commissioning", "viewer"],
  ["GET",    /^\/cx\/projects\/[^/]+\/events$/,                "commissioning", "viewer"],
  ["GET",    /^\/cx\/projects\/[^/]+\/inventory$/,             "commissioning", "viewer"],
  ["GET",    /^\/cx\/projects\/[^/]+\/runs$/,                  "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/runs$/,                  "commissioning", "operator"],
  ["GET",    /^\/cx\/runs\/[^/]+$/,                            "commissioning", "viewer"],
  ["GET",    /^\/cx\/templates$/,                             "commissioning", "viewer"],
  // Cx 5 (Baler#41): the UI's reads
  ["GET",    /^\/cx\/runs\/[^/]+\/report$/,                     "commissioning", "viewer"],
  ["GET",    /^\/cx\/projects\/[^/]+\/gaps$/,                   "commissioning", "viewer"],
  ["GET",    /^\/cx\/equipment-status$/,                      "commissioning", "viewer"],
  // Cx 4 (Baler#40): issues log and accepted risks. Accepting or revoking
  // a risk is an owner's call - admin.
  ["GET",    /^\/cx\/projects\/[^/]+\/issues$/,                 "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/issues$/,                 "commissioning", "operator"],
  ["POST",   /^\/cx\/projects\/[^/]+\/issues\/import$/,          "commissioning", "operator"],
  ["GET",    /^\/cx\/projects\/[^/]+\/issues\/export$/,          "commissioning", "viewer"],
  ["GET",    /^\/cx\/projects\/[^/]+\/risks$/,                  "commissioning", "viewer"],
  ["POST",   /^\/cx\/runs\/[^/]+\/issues$/,                     "commissioning", "operator"],
  ["GET",    /^\/cx\/issues\/[^/]+$/,                          "commissioning", "viewer"],
  ["PATCH",  /^\/cx\/issues\/[^/]+$/,                          "commissioning", "operator"],
  ["GET",    /^\/cx\/issues\/[^/]+\/events$/,                   "commissioning", "viewer"],
  ["POST",   /^\/cx\/issues\/[^/]+\/risk$/,                     "commissioning", "admin"],
  ["DELETE", /^\/cx\/risks\/[^/]+$/,                           "commissioning", "admin"],
  // Cx 6 (Baler#42): test procedures (passive) and run diffs
  ["GET",    /^\/cx\/procedure-templates$/,                   "commissioning", "viewer"],
  ["GET",    /^\/cx\/projects\/[^/]+\/procedures$/,             "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/procedures$/,             "commissioning", "operator"],
  ["GET",    /^\/cx\/procedures\/[^/]+$/,                      "commissioning", "viewer"],
  ["PATCH",  /^\/cx\/procedures\/[^/]+$/,                      "commissioning", "operator"],
  ["DELETE", /^\/cx\/procedures\/[^/]+$/,                      "commissioning", "admin"],
  ["GET",    /^\/cx\/procedures\/[^/]+\/runs$/,                 "commissioning", "viewer"],
  ["POST",   /^\/cx\/procedures\/[^/]+\/runs$/,                 "commissioning", "operator"],
  ["GET",    /^\/cx\/test-runs\/[^/]+$/,                       "commissioning", "viewer"],
  ["GET",    /^\/cx\/runs\/[^/]+\/diff$/,                       "commissioning", "viewer"],
  // Cx 7 (Baler#43): spec ingestion and alignment. Extraction calls the
  // Claude API, so it's a change (operator), like imports.
  ["GET",    /^\/cx\/projects\/[^/]+\/spec$/,                   "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/spec\/import$/,            "commissioning", "operator"],
  ["POST",   /^\/cx\/projects\/[^/]+\/spec\/extract$/,           "commissioning", "operator"],
  ["GET",    /^\/cx\/projects\/[^/]+\/spec\/documents$/,         "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/spec\/review$/,            "commissioning", "operator"],
  ["GET",    /^\/cx\/spec-documents\/[^/]+$/,                  "commissioning", "viewer"],
  ["PATCH",  /^\/cx\/spec-items\/[^/]+$/,                      "commissioning", "operator"],
  ["GET",    /^\/cx\/projects\/[^/]+\/alignment$/,              "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/alignment\/decisions$/,   "commissioning", "operator"],
  ["DELETE", /^\/cx\/mappings\/[^/]+$/,                        "commissioning", "operator"],
  // Cx 8 (Baler#44): mirrored faults; FDD faults into the issues log
  ["GET",    /^\/cx\/projects\/[^/]+\/faults$/,                 "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/faults\/ingest$/,          "commissioning", "operator"],
  // Cx 9 (Baler#45): active tests command equipment - admin. Aborting one
  // is the safe direction, so an operator can pull that cord too. The
  // backend re-checks admin, and BALER_CX_ALLOW_WRITES gates it all.
  ["GET",    /^\/cx\/active\/status$/,                         "commissioning", "viewer"],
  ["POST",   /^\/cx\/active\/kill$/,                           "commissioning", "admin"],
  ["GET",    /^\/cx\/projects\/[^/]+\/active-runs$/,            "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/active-runs$/,            "commissioning", "admin"],
  ["GET",    /^\/cx\/active-runs\/[^/]+$/,                     "commissioning", "viewer"],
  ["POST",   /^\/cx\/active-runs\/[^/]+\/confirm$/,             "commissioning", "admin"],
  ["POST",   /^\/cx\/active-runs\/[^/]+\/abort$/,               "commissioning", "operator"],
  // Cx 10 (Baler#46): the agent. Talking to it is a change (it can start
  // passive runs and propose); accepting a proposal is the change itself,
  // and the backend requires admin for an active-test proposal.
  ["GET",    /^\/cx\/projects\/[^/]+\/agent\/sessions$/,         "commissioning", "viewer"],
  ["POST",   /^\/cx\/projects\/[^/]+\/agent\/sessions$/,         "commissioning", "operator"],
  ["GET",    /^\/cx\/agent-sessions\/[^/]+$/,                  "commissioning", "viewer"],
  ["POST",   /^\/cx\/agent-sessions\/[^/]+\/messages$/,         "commissioning", "operator"],
  ["GET",    /^\/cx\/projects\/[^/]+\/agent\/proposals$/,        "commissioning", "viewer"],
  ["POST",   /^\/cx\/agent-proposals\/[^/]+\/accept$/,          "commissioning", "operator"],
  ["POST",   /^\/cx\/agent-proposals\/[^/]+\/reject$/,          "commissioning", "operator"],

  // auth_api - POST /auth/login has its own exact-match nginx location
  // with no js_access at all (can't require a token to get a token), so
  // it deliberately has no row here.
  ["POST",   /^\/auth\/logout$/,                     "auth", "viewer"],
  ["GET",    /^\/auth\/me$/,                          "auth", "viewer"],
  ["POST",   /^\/auth\/api-keys$/,                   "auth", "admin"],
  ["GET",    /^\/auth\/api-keys$/,                   "auth", "admin"],
  ["DELETE", /^\/auth\/api-keys\/[^/]+$/,              "auth", "admin"],
  ["POST",   /^\/auth\/sites$/,                      "auth", "admin"],
  ["PUT",    /^\/auth\/sites\/[^/]+\/topic-prefixes$/,   "auth", "admin"],
  ["POST",   /^\/auth\/users$/,                      "auth", "admin"],
  ["GET",    /^\/auth\/users$/,                      "auth", "admin"],
  ["DELETE", /^\/auth\/users\/[^/]+$/,                 "auth", "admin"],
  ["PUT",    /^\/auth\/users\/[^/]+\/password$/,       "auth", "admin"],
  ["PATCH",  /^\/auth\/users\/[^/]+$/,                 "auth", "admin"],
  ["GET",    /^\/auth\/sites$/,                      "auth", "admin"],
  // Edge-node MQTT accounts (Baler#36)
  ["GET",    /^\/auth\/broker-accounts$/,            "auth", "admin"],
  ["POST",   /^\/auth\/broker-accounts$/,            "auth", "admin"],
  ["POST",   /^\/auth\/broker-accounts\/[^/]+\/rotate$/, "auth", "admin"],
  ["DELETE", /^\/auth\/broker-accounts\/[^/]+$/,      "auth", "admin"],
  ["POST",   /^\/auth\/me\/password$/,                "auth", "viewer"],
  // Public like /auth/login (not exempted via its own nginx location,
  // since unlike login it still needs js_access to run so the public
  // "no role" branch below actually executes) - a brand-new org has to
  // be creatable with zero prior credentials; its own payload creates
  // the first admin user in the same call (see auth.create_org).
  ["POST",   /^\/auth\/orgs$/,                       null, null],

  // haystack_api - the Project Haystack HTTP API, at SkySpark's
  // /api/{project}/{op} layout (and Haxall's flat /api/{op}). Ops take GET
  // (zinc query params) or POST (a grid body) interchangeably per the spec.
  // Read-type ops are viewer; anything that writes data is operator.
  ["GET",    /^\/api\/(?:[^/]+\/)?(about|ops|formats|read|nav|hisRead|watchSub|watchUnsub|watchPoll|close|defs|libs|filetypes)$/, "haystack", "viewer"],
  ["POST",   /^\/api\/(?:[^/]+\/)?(about|ops|formats|read|nav|hisRead|watchSub|watchUnsub|watchPoll|close|defs|libs|filetypes)$/, "haystack", "viewer"],
  ["POST",   /^\/api\/(?:[^/]+\/)?(hisWrite|pointWrite|invokeAction)$/,                                         "haystack", "operator"],

  // windrow_api (the Devices page): exactly the routes ui/devices.js uses.
  // Reads for any role; every change is admin-only. Anything else Windrow
  // serves - POST /write (a raw BACnet WriteProperty that bypasses Baler's
  // pointWrite priority array and its record), the /discover and
  // /modbus/discover network sweeps - has no row, so it's refused here.
  // WHOLE_ORG below also keeps site-restricted logins out (Baler#14).
  ["GET",    /^\/windrow\/devices$/,                                       "devices", "viewer"],
  ["GET",    /^\/windrow\/connections$/,                                   "devices", "viewer"],
  ["GET",    /^\/windrow\/modbus\/connections$/,                           "devices", "viewer"],
  ["POST",   /^\/windrow\/learn$/,                                         "devices", "admin"],
  ["POST",   /^\/windrow\/connections$/,                                   "devices", "admin"],
  ["POST",   /^\/windrow\/modbus\/connections$/,                           "devices", "admin"],
  ["POST",   /^\/windrow\/devices\/[^/]+\/recheck-credentials$/,           "devices", "admin"],
  ["PATCH",  /^\/windrow\/devices\/[^/]+$/,                                "devices", "admin"],
  ["PATCH",  /^\/windrow\/devices\/[^/]+\/credential$/,                    "devices", "admin"],
  ["PATCH",  /^\/windrow\/connections\/[^/]+\/credential$/,                "devices", "admin"],
  ["PATCH",  /^\/windrow\/modbus\/connections\/[^/]+\/credential$/,        "devices", "admin"],
  ["DELETE", /^\/windrow\/connections\/[^/]+$/,                            "devices", "admin"],
  ["DELETE", /^\/windrow\/modbus\/connections\/[^/]+$/,                    "devices", "admin"],

  // public on every service - no token required
  ["GET",    /^\/[a-z]+\/(openapi\.yaml|docs)$/,     null, null],
];

/*
 * The token from an Authorization header, in either form a client sends:
 * OAuth/RFC 6750 style `Bearer <token>`, or Project Haystack's
 * `BEARER authToken=<token>` (what SkySpark/Haxall/nhaystack clients send
 * after SCRAM login - haystack_api hands them the same session JWT).
 * Case-insensitive scheme, per RFC 7235.
 */
function bearerFromHeader(header) {
    if (!header) {
        return null;
    }
    const m = header.match(/^bearer\s+(?:authToken=)?([^\s,]+)\s*$/i);
    return m ? m[1] : null;
}

/*
 * A Haystack HELLO/SCRAM login step on a Haystack route: carries no token
 * yet by definition, so it's passed through to haystack_api (which runs
 * the handshake against the users table) instead of being 401'd here.
 * Only ever on /api/ - every other service still needs a JWT up front.
 */
function isHaystackHandshake(uri, header) {
    return typeof header === "string"
        && /^\/api\//.test(uri)
        && /^(hello|scram)\s/i.test(header);
}

/*
 * Returns the first matching ROUTES row as {resource, minRole}, or null
 * if nothing matches (main.js treats "no match" as deny, not fail-open -
 * an unlisted route under a proxied prefix is a policy gap to close, not
 * traffic to silently let through).
 */
function resolveRoute(method, uri) {
    // No destructuring (njs 1.0.0 doesn't support it) - plain index
    // access into each row instead.
    const row = ROUTES.find(function (entry) {
        return entry[0] === method && entry[1].test(uri);
    });
    if (!row) {
        return null;
    }
    return { resource: row[2], minRole: row[3] };
}

// Matches auth.SESSION_TTL_SECONDS: a session token without `iat` (issued
// before it was added) was issued at exp - this.
const SESSION_TTL_SECONDS = 12 * 3600;

/*
 * "user:<id>:<epoch>" denylist rows (auth.end_user_sessions) ->
 * {key: "user:<id>", epoch}, or null for an ordinary jti.
 */
function parseUserCutoff(jti) {
    // epoch seconds, with milliseconds since #50 (older rows: whole seconds)
    const m = /^(user:.+):(\d+(?:\.\d+)?)$/.exec(jti);
    return m ? { key: m[1], epoch: Number(m[2]) } : null;
}

/*
 * Whether a session token predates its user's cutoff (cached value from
 * the "user:<id>" key, a string epoch, or undefined/null for none).
 */
function sessionCutOff(claims, cutoff) {
    if (cutoff === undefined || cutoff === null || cutoff === "") {
        return false;
    }
    const iat = typeof claims.iat === "number" ? claims.iat : claims.exp - SESSION_TTL_SECONDS;
    return iat < Number(cutoff);
}

/*
 * Whether a token's jti is on the denylist (cached value for that jti, or
 * undefined/null). Every kind is checked: API keys revoked in Ops, and
 * session tokens ended early by Haystack `close`. The "user:<id>" cutoff
 * keys can't collide: a jti never contains ":".
 */
function jtiRevoked(claims, cached) {
    if (!claims.jti) {
        return false;
    }
    return cached !== undefined && cached !== null && cached !== "";
}

// Resources a site-restricted token can't reach at all: they span the
// whole org and have no per-site filter to apply.
const WHOLE_ORG = { devices: true, gateway: true };

/*
 * Whether these claims may reach a resource: site-restricted tokens
 * (non-empty site_ids) are kept out of WHOLE_ORG resources.
 */
function scopeSatisfies(resource, claims) {
    if (!WHOLE_ORG[resource]) {
        return true;
    }
    const sites = claims && claims.site_ids;
    return !(Array.isArray(sites) && sites.length > 0);
}

function roleSatisfies(role, minRole) {
    if (minRole === null) {
        return true; // public route
    }
    const have = ROLE_RANK[role];
    const need = ROLE_RANK[minRole];
    return typeof have === "number" && typeof need === "number" && have >= need;
}

/*
 * What to say for a path no policy row matches (#69): 404, the path, and
 * where to look - the API's own reference, a guess when the path looks like
 * one API under another's prefix (/api/derivation), and the site docs.
 * "no policy defined for this route" told a user nothing.
 */
const API_PREFIXES = ["ingest", "fault", "windrow", "derivation", "validate", "ops", "cx", "auth"];

function apiNamed(word) {
    const w = String(word || "").toLowerCase();
    for (let i = 0; i < API_PREFIXES.length; i++) {
        const p = API_PREFIXES[i];
        if (w === p || w === p + "s" || (p === "fault" && w === "faults")) {
            return p;
        }
    }
    return null;
}

function unknownRouteMessage(method, uri) {
    const parts = String(uri).split("/");
    const first = parts.length > 1 ? parts[1] : "";
    const lines = ["no such route: " + method + " " + uri];
    const own = apiNamed(first) === first ? first : null;
    if (own) {
        lines.push("API reference: /" + own + "/docs");
    } else if (first === "api") {
        lines.push("/api/ is the Haystack API: /api/{op} or /api/{project}/{op}, e.g. /api/about, /api/read, /api/hisRead");
        const guess = apiNamed(parts.length > 2 ? parts[2] : "");
        if (guess) {
            lines.push("did you mean /" + guess + "/...? API reference: /" + guess + "/docs");
        }
    } else {
        const guess = apiNamed(first);
        if (guess) {
            lines.push("did you mean /" + guess + "/...? API reference: /" + guess + "/docs");
        }
    }
    lines.push("all docs: /docs");
    return lines.join("\n") + "\n";
}

const SIGN_IN_HINT = "sign in with POST /auth/login (see /auth/docs), then send Authorization: Bearer <token>";

export default { ROLE_RANK, ROUTES, resolveRoute, roleSatisfies, scopeSatisfies, parseUserCutoff, sessionCutOff, jtiRevoked, bearerFromHeader, isHaystackHandshake, unknownRouteMessage, SIGN_IN_HINT };
