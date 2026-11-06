import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";

import { seedDatabase } from "../bench/seed.mjs";
import { createStack } from "../local/stack.mjs";
import { BUNDLE_PATH } from "./harness.mjs";

test("the built bundle boots and fails closed without Access", async (t) => {
  assert.ok(existsSync(BUNDLE_PATH), "missing worker bundle; run `npm run build` in cloudflare/");
  const stack = await createStack();
  t.after(() => stack.dispose());
  const response = await stack.dispatchFetch("https://memories.test/api/v1/status");
  assert.equal(response.status, 403);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal((await response.json()).error.code, "forbidden");
});

test("the deployment module graph serves SDK calls in workerd", async (t) => {
  const stack = await createStack({
    readers: "reader@example.test",
    identity: { email: "reader@example.test" },
    hostFromUrl: true,
  });
  t.after(() => stack.dispose());
  assert.ok(stack.moduleNames.includes("routes/mcp-bundled.js"));
  await seedDatabase(await stack.d1());
  const endpoint = new URL("https://memories.17166969.xyz/mcp");
  const client = new Client({ name: "workerd-client", version: "1" });
  await client.connect(new StreamableHTTPClientTransport(endpoint, {
    fetch: async (url, init) => {
      const headers = new Headers(init?.headers);
      headers.set("Host", endpoint.host);
      return await stack.dispatchFetch(String(url), { ...init, headers });
    },
  }));
  t.after(() => client.close());
  assert.equal((await client.listTools()).tools.length, 3);
  const found = await client.callTool({ name: "memory_search", arguments: {} });
  assert.deepEqual(JSON.parse(found.content[0].text).memories, []);
  const status = await client.callTool({ name: "server_status", arguments: {} });
  assert.equal(JSON.parse(status.content[0].text).scope, "reader");
});
