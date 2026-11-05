/**
 * Proves BalerHisExt is a real drop-in for a fresh, plain
 * open-source Haxall instance (not SkySpark) - see
 * ~/haxall/docker-compose.yml (HAXALL_PORT default here overridden to
 * 8280 to avoid colliding with other locally-running projects).
 *
 * Login selectors below are real, not guessed - confirmed via `curl` of
 * the actual server-rendered login page: `#username`, `#password`,
 * `#loginButton`, form posts to `/user/auth`.
 *
 * The authenticated app past login is a SPA. Its exact history-view
 * selectors are NOT filled in yet - that needs a live reconnaissance
 * pass against the actual running instance first (see the plan's
 * explicit note on this), not guessed selectors that could silently
 * click the wrong thing. See the `test.fixme` below.
 */
import { test, expect } from "@playwright/test";

const HAXALL_URL = process.env.HAXALL_OSS_URL ?? "http://localhost:8280";
const HAXALL_USER = process.env.HAXALL_SU_USERNAME ?? "su";
const HAXALL_PASS = process.env.HAXALL_SU_PASSWORD ?? "";
const BALER_URL = process.env.BALER_URL ?? "http://localhost:8000";

async function login(page: import("@playwright/test").Page) {
  await page.goto(`${HAXALL_URL}/`);
  await page.fill("#username", HAXALL_USER);
  await page.fill("#password", HAXALL_PASS);
  await page.click("#loginButton");
}

test("logs into the real OSS Haxall instance", async ({ page }) => {
  await login(page);
  // A successful login redirects off /user/login (hxLogin.redirectUri = "/").
  await expect(page).not.toHaveURL(/\/user\/login/);
});

test.fixme(
  "writing a point's history through the UI lands in Baler, not Folio",
  async ({ page }) => {
    // Pending: needs a live reconnaissance pass against the real
    // post-login SPA to find the actual history-write/chart-view
    // selectors before this can be written for real. Once tdHis is
    // enabled (libRemove/libAdd via Axon - see the Python-side
    // verification in test_history_routes.py's live counterpart), the
    // real check here is: write a value through Haxall's UI, then
    // independently confirm it via Baler's own API:
    //
    //   const res = await fetch(`${BALER_URL}/history?point=<point>`);
    //   const items = await res.json();
    //   expect(items.at(-1).value).toBe(<value written through the UI>);
  },
);
