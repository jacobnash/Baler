// Plain-node test for ratelimit.js (#71): a fake ngx.shared dict with
// TTL-on-create semantics, driven by a fake clock.
import assert from "node:assert/strict";

let now = 0;
const store = new Map();
globalThis.ngx = {
    shared: {
        ratelimit: {
            incr(key, delta, init) {
                const v = (store.has(key) ? store.get(key) : init) + delta;
                store.set(key, v);
                return v;
            },
        },
    },
};
const realNow = Date.now;
Date.now = () => now;

const { default: rl } = await import("./ratelimit.js");
let passed = 0;
function check(name, fn) {
    fn();
    passed += 1;
    console.log("ok - " + name);
}

check("a token over its limit is refused, then allowed again in the next window", () => {
    const claims = { sub: "tok", role: "viewer" };
    now = 5 * rl.WINDOW_MS + 1000;
    for (let i = 0; i < rl.LIMITS.viewer; i++) assert.equal(rl.checkRateLimit(null, claims), true);
    assert.equal(rl.checkRateLimit(null, claims), false);
    // retrying (rejected requests still count) never extends the window
    for (let i = 0; i < 100; i++) assert.equal(rl.checkRateLimit(null, claims), false);
    now = 6 * rl.WINDOW_MS + 1;
    assert.equal(rl.checkRateLimit(null, claims), true);
});

check("Retry-After is the time left in the window", () => {
    assert.equal(rl.retryAfterSeconds(10 * rl.WINDOW_MS + 15000), 45);
    assert.equal(rl.retryAfterSeconds(10 * rl.WINDOW_MS + 59999), 1);
});

check("operator limit is 3000/min", () => {
    assert.equal(rl.LIMITS.operator, 3000);
});

Date.now = realNow;
console.log("\n" + passed + " passed");
