import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

import { Miniflare } from "miniflare";

import { BUNDLE_PATH } from "./harness.mjs";

test("the built bundle boots and fails closed without Access", async (t) => {
  assert.ok(existsSync(BUNDLE_PATH), "missing worker bundle; run `npm run build` in agent-memory/cloudflare");
  const miniflare = new Miniflare({
    modules: true,
    modulesRules: [{ type: "ESModule", include: ["**/*.js"] }],
    scriptPath: BUNDLE_PATH,
    compatibilityDate: "2026-08-06",
    compatibilityFlags: ["nodejs_compat"],
    bindings: { READERS: "reader@example.test" },
  });
  t.after(() => miniflare.dispose());
  const response = await miniflare.dispatchFetch("https://memories.test/api/v1/status");
  assert.equal(response.status, 403);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal((await response.json()).error.code, "forbidden");
});
