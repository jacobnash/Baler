/**
 * Targets the `windrow-haxall` container (localhost:8180) - originally
 * assumed to be commercial SkySpark (image name `maugustusa/myskyspark`),
 * but its own `about()` Axon call reports `productName='Haxall'`,
 * `moduleName='hxd'`, version 4.0.6 - real, plain open-source Haxall,
 * not SkySpark. Kept as a second, separate real target anyway (a
 * different running instance/config than the fresh one in
 * his-provider-oss.spec.ts), not renamed mid-plan since the plan's file
 * list already named it this way and it's still a distinct real
 * verification target.
 *
 * Real, working credentials (already used successfully by
 * ~/windrow/src/windrow/haxall_client.py and ~/benchmark's Haxall driver this
 * session): su / windrow-demo-pass.
 */
import { test, expect } from "@playwright/test";

const HAXALL_URL = process.env.HAXALL_SKYSPARK_URL ?? "http://localhost:8180";
const HAXALL_USER = "su";
const HAXALL_PASS = "windrow-demo-pass";
const BALER_URL = process.env.BALER_URL ?? "http://localhost:8000";

async function login(page: import("@playwright/test").Page) {
  await page.goto(`${HAXALL_URL}/`);
  await page.fill("#username", HAXALL_USER);
  await page.fill("#password", HAXALL_PASS);
  await page.click("#loginButton");
}

test("logs into windrow-haxall with the real benchmark-suite credentials", async ({ page }) => {
  await login(page);
  await expect(page).not.toHaveURL(/\/user\/login/);
});

test.fixme(
  "writing a point's history through the UI lands in Baler, not Folio",
  async ({ page }) => {
    // Same pending status as his-provider-oss.spec.ts - real selectors
    // need a live reconnaissance pass first. This is also the shared
    // benchmark/demo container other work this session depends on -
    // revert (libRemove(["tdHis"]); libAdd(["hx.hxd.his"])) after
    // verifying, per the plan's explicit risk note.
  },
);
