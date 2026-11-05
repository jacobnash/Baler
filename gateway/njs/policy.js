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
  ["POST",   /^\/derivation\/derivations\/[^/]+\/targets\/[^/]+\/enable$/,  "derivations", "operator"],

  // validate_api
  ["POST",   /^\/validate\/validate$/,               "validate", "viewer"],

  // ops_api
  ["GET",    /^\/ops\/status$/,                      "ops", "viewer"],

  // auth_api - POST /auth/login has its own exact-match nginx location
  // with no js_access at all (can't require a token to get a token), so
  // it deliberately has no row here.
  ["POST",   /^\/auth\/logout$/,                     "auth", "viewer"],
  ["GET",    /^\/auth\/me$/,                          "auth", "viewer"],
  ["POST",   /^\/auth\/api-keys$/,                   "auth", "admin"],
  ["GET",    /^\/auth\/api-keys$/,                   "auth", "admin"],
  ["DELETE", /^\/auth\/api-keys\/[^/]+$/,              "auth", "admin"],
  ["POST",   /^\/auth\/sites$/,                      "auth", "admin"],
  ["POST",   /^\/auth\/users$/,                      "auth", "admin"],
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
  ["GET",    /^\/api\/(?:[^/]+\/)?(about|ops|formats|read|nav|hisRead|watchSub|watchUnsub|watchPoll|close)$/, "haystack", "viewer"],
  ["POST",   /^\/api\/(?:[^/]+\/)?(about|ops|formats|read|nav|hisRead|watchSub|watchUnsub|watchPoll|close)$/, "haystack", "viewer"],
  ["POST",   /^\/api\/(?:[^/]+\/)?(hisWrite|pointWrite|invokeAction)$/,                                         "haystack", "operator"],

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

function roleSatisfies(role, minRole) {
    if (minRole === null) {
        return true; // public route
    }
    const have = ROLE_RANK[role];
    const need = ROLE_RANK[minRole];
    return typeof have === "number" && typeof need === "number" && have >= need;
}

export default { ROLE_RANK, ROUTES, resolveRoute, roleSatisfies, bearerFromHeader, isHaystackHandshake };
