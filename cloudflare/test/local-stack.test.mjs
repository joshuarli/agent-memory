import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { seedDatabase } from "../bench/seed.mjs";
import { BUNDLE_DIR, loadReleaseConfig, prepareWorkerConfig } from "../local/config.mjs";
import { computeBundleDigest, createStack } from "../local/stack.mjs";

const workerRoot = fileURLToPath(new URL("..", import.meta.url));

function workerdChildren() {
  return execFileSync("ps", ["-axo", "pid=,ppid=,command="], { encoding: "utf8" })
    .split("\n")
    .filter((line) => /workerd/.test(line) && Number(line.trim().split(/\s+/)[1]) === process.pid);
}

function channelMessages(events) {
  return events.flatMap((event) =>
    (event.diagnosticsChannelEvents ?? []).map((entry) => ({ channel: entry.channel, ...entry.message })));
}

async function waitForTail(stack, predicate, timeout = 2_000) {
  const started = Date.now();
  let events = await stack.tailEvents();
  while (!predicate(events) && Date.now() - started < timeout) {
    await new Promise((resolve) => setTimeout(resolve, 25));
    events = await stack.tailEvents();
  }
  return events;
}

test("the release config rejects every unrepresentable key and binding by name", () => {
  const release = loadReleaseConfig();
  assert.throws(
    () => prepareWorkerConfig({ ...release, env: { ...release.env, QUEUE: { type: "queue", name: "q" } } }),
    /env\.QUEUE\.type is unsupported/,
  );
  assert.throws(
    () => prepareWorkerConfig({ ...release, env: { ...release.env, DB: { ...release.env.DB, preview_id: "x" } } }),
    /env\.DB\.preview_id is unsupported/,
  );
  assert.throws(() => prepareWorkerConfig({ ...release, routes: [] }), /routes is unsupported/);
  assert.throws(
    () => prepareWorkerConfig({ ...release, tailConsumers: [{ worker: "stats-observe" }, { worker: "stats-observe" }] }),
    /tailConsumers/,
  );
  assert.throws(() => prepareWorkerConfig({ ...release, tailConsumers: [] }), /tailConsumers/);
  assert.throws(
    () => prepareWorkerConfig({ ...release, exports: { default: { type: "worker", cache: { enabled: true } } } }),
    /exports\.default\.cache\.enabled/,
  );
  const prepared = prepareWorkerConfig(release, { teamDomain: "team.example.test", audience: "test" });
  assert.equal(prepared.env.ACCESS_TEAM_DOMAIN.value, "team.example.test");
  assert.equal(prepared.env.ACCESS_AUD.value, "test");
  assert.equal(prepared.manifest.modulesRoot, BUNDLE_DIR);
  assert.equal("type" in prepared.manifest, false);
});

test("the stack runs every released module at the released compatibility date", async (t) => {
  const release = loadReleaseConfig();
  const stack = await createStack();
  t.after(() => stack.dispose());
  assert.equal(stack.compatibilityDate, release.compatibilityDate);
  assert.deepEqual(stack.compatibilityFlags, release.compatibilityFlags);
  assert.deepEqual(stack.moduleNames, Object.keys(release.manifest.modules).sort());
  for (const [name, module] of Object.entries(release.manifest.modules)) {
    assert.equal(stack.config.manifest.modules[name].type, module.type);
    assert.equal(stack.config.manifest.modules[name].contents, readFileSync(join(BUNDLE_DIR, name), "utf8"));
  }
  assert.equal(stack.config.manifest.mainModule, release.manifest.mainModule);
  assert.equal(stack.registryEnabled, false);
  assert.equal(stack.registryPath, undefined);
  assert.equal(stack.unsafeRegisterWorker, false);
  for (const binding of Object.values(stack.config.env)) {
    assert.ok(["text", "d1", "r2"].includes(binding.type), binding.type);
    assert.equal("dev" in binding, false);
  }
  const response = await stack.dispatchFetch("https://memories.test/api/v1/status");
  assert.equal(response.status, 403);
  assert.equal((await response.json()).error.code, "forbidden");
});

test("the stack boots the exact bundle that bundle-digest.sh publishes", async (t) => {
  const stack = await createStack();
  t.after(() => stack.dispose());
  const published = execFileSync("sh", [join(workerRoot, "scripts/bundle-digest.sh")], { encoding: "utf8" }).trim();
  assert.equal(stack.bundleDigest, published);
  assert.equal(computeBundleDigest(), published);
});

test("nodejs_compat carries diagnostics-channel messages from a production route to the tail worker", async (t) => {
  const stack = await createStack({
    readers: "reader@example.test",
    identity: { email: "reader@example.test" },
  });
  t.after(() => stack.dispose());
  await seedDatabase(await stack.d1());
  const response = await stack.dispatchFetch("https://memories.test/api/v1/status");
  assert.equal(response.status, 200);
  const events = await waitForTail(stack, (items) =>
    channelMessages(items).some((message) => message.kind === "invocation"));
  const invocation = channelMessages(events).find((message) => message.kind === "invocation");
  assert.notEqual(invocation, undefined);
  assert.equal(invocation.channel, "stats.observe");
  assert.match(invocation.traceId, /^[0-9a-f]{32}$/);
  assert.match(invocation.spanId, /^[0-9a-f]{16}$/);
});

test("an unexpected outbound fetch fails dispose and keeps its state", async () => {
  const stack = await createStack({ teamDomain: "team.example.test", audience: "test" });
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const assertion = `${encode({ alg: "RS256", kid: "key-1" })}.${encode({})}.${"A".repeat(64)}`;
  const response = await stack.dispatchFetch("https://memories.test/api/v1/status", {
    headers: { "Cf-Access-Jwt-Assertion": assertion },
  });
  assert.equal(response.status, 503);
  assert.deepEqual(stack.outbound, ["https://team.example.test/cdn-cgi/access/certs"]);
  assert.equal(existsSync(stack.stateDir), true);
  await assert.rejects(stack.dispose(), /unexpected outbound request/);
  assert.equal(existsSync(stack.stateDir), true);
  assert.equal(existsSync(join(stack.stateDir, "workerd.log")), true);
  rmSync(stack.stateDir, { recursive: true, force: true });
});

test("two stacks have separate state, and dispose removes every state directory and process", async () => {
  const first = await createStack();
  const second = await createStack();
  assert.notEqual(first.stateDir, second.stateDir);
  const firstDb = await first.d1();
  const secondDb = await second.d1();
  await seedDatabase(firstDb);
  await seedDatabase(secondDb);
  await firstDb.prepare(
    "INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)",
  ).bind("first.access", "first", "test", 0).run();
  assert.equal((await firstDb.prepare("SELECT count(*) AS count FROM collector").first()).count, 1);
  assert.equal((await secondDb.prepare("SELECT count(*) AS count FROM collector").first()).count, 0);
  assert.ok(workerdChildren().length >= 2);
  const firstState = first.stateDir;
  const secondState = second.stateDir;
  await first.dispose();
  await second.dispose();
  assert.equal(first.disposed, true);
  assert.equal(second.disposed, true);
  assert.equal(existsSync(firstState), false);
  assert.equal(existsSync(secondState), false);
  assert.deepEqual(workerdChildren(), []);
  await first.dispose();
  await second.dispose();
});
