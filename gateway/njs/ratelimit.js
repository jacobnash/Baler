/*
 * Per-token rate limiting via a shared dict counter. This is an abuse
 * guard (fixed 60s window, reset by TTL) - not a usage meter. It throws
 * counts away every window on purpose; billing/usage metering (counts
 * that accumulate over a billing period, attributed per org/site) is a
 * separate concern for a separate piece of code, not this file.
 */

// Requests/minute per role. Arbitrary starting defaults, not derived
// from load data - tune once real traffic exists.
const LIMITS = { viewer: 1200, operator: 3000, admin: 6000, service: 12000 };
const WINDOW_MS = 60000;

/*
 * Returns true if `claims` is still within its rate limit (and atomically
 * counts this request against it); false if the limit is exceeded. `r`
 * is the nginx request object - needed here (unlike jwt.js/policy.js)
 * because `ngx.shared` is an nginx global, not something plain Node has.
 */
function windowKey(sub, now) {
    // One counter per token per clock minute (#71). Keyed by sub alone,
    // every incr - rejected requests included - kept the same entry alive,
    // so a client retrying each Retry-After stayed at 429 forever. A new
    // window is a new key; old keys just expire.
    return sub + ":" + Math.floor(now / WINDOW_MS);
}

function checkRateLimit(r, claims) {
    const limit = LIMITS[claims.role] || LIMITS.viewer;
    const n = ngx.shared.ratelimit.incr(windowKey(claims.sub, Date.now()), 1, 0, WINDOW_MS);
    return n <= limit;
}

/* Seconds until the current window ends: what Retry-After should say. */
function retryAfterSeconds(now) {
    return Math.max(1, Math.ceil((WINDOW_MS - (now % WINDOW_MS)) / 1000));
}

export default { checkRateLimit, retryAfterSeconds, windowKey, LIMITS, WINDOW_MS };
