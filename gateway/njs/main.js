/*
 * The only file that touches nginx globals (`r`, `ngx`, the periodic
 * session `s`) - everything else (jwt.js, policy.js, ratelimit.js) is
 * pure logic this file glues together. Two exports:
 *   - authorize(r):        js_access handler - the real access-control gate.
 *   - refreshRevoked(s):   js_periodic handler - keeps ngx.shared.revoked warm.
 */

import jwt from "./jwt.js";
import policy from "./policy.js";
import ratelimit from "./ratelimit.js";

function bearerToken(r) {
    return policy.bearerFromHeader(r.headersIn.Authorization);
}

async function authorize(r) {
    // Set on every invocation, regardless of outcome below - harmless on
    // a path that ends in a 401/403 (the request never reaches a
    // backend), and simplest to set once here rather than on every
    // individual "grant" return point.
    r.variables.gateway_secret = process.env.BALER_GATEWAY_SECRET;

    const route = policy.resolveRoute(r.method, r.uri);
    if (!route) {
        // Unmapped route under a proxied prefix - a policy gap to close,
        // not traffic to silently let through. See policy.js's own note.
        r.return(403, "no policy defined for this route\n");
        return;
    }

    if (route.minRole === null) {
        // Public route (e.g. GET /*/openapi.yaml, GET /*/docs) - no
        // token required, no rate limit applied.
        return;
    }

    if (policy.isHaystackHandshake(r.uri, r.headersIn.Authorization)) {
        // HELLO/SCRAM login step - no token exists yet; haystack_api runs
        // the handshake itself and is rate-limited by nginx (zone=login).
        return;
    }

    const token = bearerToken(r);
    if (!token) {
        r.return(401, "missing bearer token\n");
        return;
    }

    const claims = await jwt.verify(token, process.env.BALER_JWT_SECRET);
    if (!claims) {
        r.return(401, "invalid or expired token\n");
        return;
    }

    if (!policy.roleSatisfies(claims.role, route.minRole)) {
        r.return(403, `role '${claims.role}' cannot access this route\n`);
        return;
    }

    if (!policy.scopeSatisfies(route.resource, claims)) {
        r.return(403, "this resource spans the whole org; a site-restricted login can't use it\n");
        return;
    }

    // Only long-lived (api_key-class) tokens are checked against the
    // revocation cache - short-TTL session tokens skip it entirely, per
    // the original design rationale (their own expiry is the guard).
    if (claims.kind === "api_key" && ngx.shared.revoked.get(claims.jti)) {
        r.return(401, "token has been revoked\n");
        return;
    }

    // A user deleted, or their password reset, since this session token
    // was issued (auth.end_user_sessions): it's over, though not expired.
    if (claims.kind === "session" && policy.sessionCutOff(claims, ngx.shared.revoked.get("user:" + claims.sub))) {
        r.return(401, "session ended - log in again\n");
        return;
    }

    if (!ratelimit.checkRateLimit(r, claims)) {
        r.headersOut["Retry-After"] = "60";
        r.return(429, "rate limit exceeded\n");
        return;
    }

    // Forwarded downstream for logging/tracing only - backends don't
    // treat this as a trust boundary of their own (see X-Gateway-Secret,
    // set below, for that).
    r.variables.user = `${claims.sub}:${claims.role}`;
}

async function refreshRevoked(s) {
    try {
        const reply = await ngx.fetch("http://auth_api:8006/internal/revoked-jtis");
        if (reply.status !== 200) {
            ngx.log(ngx.WARN, `refreshRevoked: auth_api returned ${reply.status}`);
            return;
        }
        const jtis = await reply.json();
        ngx.shared.revoked.clear();
        // No for...of - njs 1.0.0 (bundled with nginx:alpine) doesn't support it.
        jtis.forEach(function (jti) {
            // "user:<id>:<epoch>" is a per-user session cutoff: keep the
            // newest epoch under "user:<id>" (policy.sessionCutOff).
            const cut = policy.parseUserCutoff(jti);
            if (cut) {
                const prev = ngx.shared.revoked.get(cut.key);
                if (!prev || Number(prev) < cut.epoch) {
                    ngx.shared.revoked.set(cut.key, String(cut.epoch));
                }
                return;
            }
            ngx.shared.revoked.set(jti, "1"); // string zone (default type) - value must be a string, not a number
        });
        ngx.log(ngx.INFO, `refreshRevoked: cached ${jtis.length} revoked jti(s)`);
    } catch (e) {
        ngx.log(ngx.WARN, `refreshRevoked failed: ${e}`);
        // auth_api unreachable this cycle - leave the existing cache in
        // place (it has its own zone-level timeout as a safety net) and
        // try again next interval. Never let a poll failure take the
        // gateway down.
    }
}

export default { authorize, refreshRevoked };
