import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

import { Miniflare } from "miniflare";

import { BUNDLE_PATH } from "./harness.mjs";

test("the built bundle boots and fails closed without Access", async (t) => {
  assert.ok(existsSync(BUNDLE_PATH), "missing worker bundle; run `npm run build` in cloudflare/");
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

test("the deployment module graph serves SDK calls in workerd", async (t) => {
  const { readdirSync } = await import("node:fs");
  const { dirname, join, relative } = await import("node:path");
  const { Client, StreamableHTTPClientTransport } = await import("@modelcontextprotocol/client");
  const { seedDatabase } = await import("../bench/seed.mjs");
  const root = dirname(BUNDLE_PATH);
  const modules = [{ type: "ESModule", path: join(root, "test-entry.js"), contents: `
    import worker from "./index.js";
    export default { fetch(request, env) {
        const headers = new Headers(request.headers);
      headers.set("Host", "memories.17166969.xyz");
      return worker.fetch(new Request(request, { headers }), env, { access: { getIdentity: async () => ({ email: "reader@example.test" }) } });
    } };
  ` }];
  function add(dir) {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) add(path);
      else if (entry.name.endsWith(".js")) modules.push({ type: "ESModule", path });
    }
  }
  add(root);
  assert.ok(modules.some((module) => relative(root, module.path) === "routes/mcp-bundled.js"));
  const miniflare = new Miniflare({ modules, modulesRoot: root, compatibilityDate: "2026-08-06",
    compatibilityFlags: ["nodejs_compat"], d1Databases: ["DB"], r2Buckets: ["BUCKET"],
    bindings: { READERS: "reader@example.test", ACCESS_TEAM_DOMAIN: "team.example.test", ACCESS_AUD: "test" } });
  t.after(() => miniflare.dispose());
  await seedDatabase(await miniflare.getD1Database("DB"));
  const endpoint = new URL("https://memories.17166969.xyz/mcp");
  const client = new Client({ name: "workerd-client", version: "1" });
  await client.connect(new StreamableHTTPClientTransport(endpoint, {
    fetch: async (url, init) => {
      const headers = new Headers(init.headers);
      headers.set("Host", endpoint.host);
      return await miniflare.dispatchFetch(String(url), { ...init, headers });
    },
  }));
  t.after(() => client.close());
  assert.equal((await client.listTools()).tools.length, 3);
  const found = await client.callTool({ name: "memory_search", arguments: {} });
  assert.deepEqual(JSON.parse(found.content[0].text).memories, []);
  const status = await client.callTool({ name: "server_status", arguments: {} });
  assert.equal(JSON.parse(status.content[0].text).scope, "reader");
});
