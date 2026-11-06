import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { seedDatabase } from "../bench/seed.mjs";
import { createStack } from "../local/stack.mjs";
import { MEMORIES_OPERATIONS } from "../src/observe-channel.ts";

const ORIGIN = "origin-a";
const COLLECTOR = "collector-client.access";
const FIXTURE = JSON.parse(readFileSync(
  new URL("../../tests/fixtures/stats_observe_events.json", import.meta.url), "utf8"));
const LEARNING = JSON.parse(readFileSync(
  new URL("../../tests/fixtures/learning_records.json", import.meta.url), "utf8"));
const JSON_INIT = { method: "POST", headers: { "Content-Type": "application/json" } };

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

function statsOperations() {
  const directory = process.env.STATS_DIR;
  if (directory === undefined) return FIXTURE.operations;
  const source = readFileSync(join(directory, "cloudflare/src/observe/core/policies.ts"), "utf8");
  const match = /export const MEMORIES_OPERATIONS = \[([\s\S]*?)\] as const;/u.exec(source);
  assert.notEqual(match, null);
  return [...match[1].matchAll(/"([^"]+)"/gu)].map((entry) => entry[1]);
}

function messages(events) {
  return events.flatMap((event) => (event.diagnosticsChannelEvents ?? [])
    .filter((entry) => entry.channel === "stats.observe")
    .map((entry) => entry.message));
}

function operations(events) {
  return messages(events).filter((message) => message.kind === "operation");
}

async function stackFor(t) {
  const stack = await createStack({
    harness: true,
    accessService: true,
    identity: { service_token_id: COLLECTOR },
    readers: "reader@example.test",
    teamDomain: "team.example.test",
    audience: "test-audience",
  });
  t.after(() => stack.dispose());
  const db = await stack.d1();
  await seedDatabase(db);
  await db.prepare(
    "INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)",
  ).bind(COLLECTOR, ORIGIN, "contract", 0).run();
  return { stack, db };
}

async function send(stack, path, init, expected) {
  const before = operations(await stack.tailEvents()).length;
  await stack.dispatchFetch("https://memories.test/__bench/reset");
  const target = path.startsWith("https://") ? path : `https://memories.test${path}`;
  const response = await stack.dispatchFetch(target, init);
  const buffered = response.clone();
  const status = await (await stack.dispatchFetch("https://memories.test/__bench/status")).json();
  let current = operations(await stack.tailEvents());
  const deadline = Date.now() + 2_000;
  while (current.length <= before && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 20));
    current = operations(await stack.tailEvents());
  }
  assert.equal(current.length, before + 1, `${expected}: one operation message`);
  const operation = current.at(-1);
  assert.equal(operation.operation, expected);
  assert.equal(operation.synthetic, false);
  assert.deepEqual(operation.rows, status.rows, `${expected}: rows`);
  const sameSpan = messages(await stack.tailEvents())
    .filter((message) => message.kind === "operation" && message.spanId === operation.spanId);
  assert.equal(sameSpan.length, 1, `${expected}: one operation per invocation`);
  return { response: buffered, operation, rows: status.rows };
}

async function putObject(stack, data) {
  const ciphertext = Buffer.concat([Buffer.from("ciphertext:"), Buffer.from(data)]);
  const result = await send(stack, `/api/v1/sync/objects/${digest(ciphertext)}`, {
    method: "PUT",
    headers: {
      "X-Plaintext-Digest": digest(data),
      "X-Plaintext-Size": String(Buffer.byteLength(data)),
      "X-Encoding": "identity",
    },
    body: ciphertext,
  }, "sync.upload");
  assert.equal(result.response.status, 200);
  return {
    plaintext: digest(data),
    size: Buffer.byteLength(data),
    ciphertext: digest(ciphertext),
    ciphertextSize: ciphertext.byteLength,
  };
}

function manifestFor(chunks) {
  const sourceDigest = createHash("sha256").update(chunks.map((chunk) => chunk.plaintext).join("")).digest("hex");
  let offset = 0;
  const tiled = chunks.map((chunk) => {
    const entry = { offset, size: chunk.size, digest: chunk.plaintext };
    offset += chunk.size;
    return entry;
  });
  return {
    origin_host: ORIGIN,
    harness: "codex",
    native_id: "contract-native",
    source_path: "/synthetic/contract-native.jsonl",
    source_digest: sourceDigest,
    source_size: offset,
    captured_at: 1762000000.5,
    chunks: tiled,
    artifacts: [],
    session_metadata: { cwd: "/synthetic", native: "contract-native" },
    manifest_digest: digest(`manifest:${ORIGIN}:contract-native:${sourceDigest}`),
    manifest_object: undefined,
  };
}

function snapshotFor(chunk) {
  const id = digest("contract-snapshot");
  return {
    id,
    created_at: 1762000000,
    schema_version: 10,
    db_digest: id,
    db_size: chunk.size,
    chunks: [{
      plaintext_digest: chunk.plaintext,
      size: chunk.size,
      ciphertext_digest: chunk.ciphertext,
    }],
    objects: 0,
    object_bytes: 0,
    objects_digest: digest(""),
  };
}

test("the worker operation list equals stats' accepted set", () => {
  assert.deepEqual([...MEMORIES_OPERATIONS], statsOperations());
  assert.equal(FIXTURE.events[0].kind, "operation");
  assert.deepEqual(Object.keys(FIXTURE.events[0]).sort(),
    ["kind", "operation", "rows", "spanId", "synthetic", "traceId"]);
});

test("each instrumented route emits one operation message with the metered rows", async (t) => {
  const { stack, db } = await stackFor(t);
  await send(stack, "/api/v1/status", {}, "status.read");
  await send(stack, "/api/v1/memories", {}, "memories.search");
  await send(stack, "/api/v1/sync/hello", {
    ...JSON_INIT, body: JSON.stringify({ protocol: 1, origin: ORIGIN }),
  }, "sync.hello");
  await send(stack, "/api/v1/sync/objects/missing", {
    ...JSON_INIT, body: JSON.stringify({ protocol: 1, origin: ORIGIN, objects: [] }),
  }, "sync.missing");
  const chunk = await putObject(stack, Buffer.from("contract chunk"));
  await send(stack, `/api/v1/sync/objects/${chunk.plaintext}/replace`, {
    ...JSON_INIT,
    body: JSON.stringify({
      protocol: 1,
      origin: ORIGIN,
      plaintext_digest: chunk.plaintext,
      plaintext_size: chunk.size,
      encoding: "identity",
      ciphertext_digest: chunk.ciphertext,
      ciphertext_size: chunk.ciphertextSize,
    }),
  }, "sync.replace");
  const manifest = manifestFor([chunk]);
  manifest.manifest_object = (await putObject(
    stack, Buffer.from(JSON.stringify({ manifest: manifest.manifest_digest })))).ciphertext;
  await send(stack, "/api/v1/sync/generations", {
    ...JSON_INIT,
    body: JSON.stringify({ protocol: 1, origin: ORIGIN, base_revision: 0, manifest }),
  }, "generations.commit");
  const revision = (await db.prepare(
    "SELECT revision FROM origin_sync WHERE origin_host = ?").bind(ORIGIN).first()).revision;
  const snapshot = snapshotFor(chunk);
  await send(stack, `/api/v1/sync/snapshots/${snapshot.id}/objects`, {
    ...JSON_INIT,
    body: JSON.stringify({ protocol: 1, origin: ORIGIN, page: 0, objects: [] }),
  }, "snapshots.page");
  await send(stack, "/api/v1/sync/snapshots", {
    ...JSON_INIT,
    body: JSON.stringify({ protocol: 1, origin: ORIGIN, base_revision: revision, snapshot }),
  }, "snapshots.commit");
  await send(stack, `/api/v1/sync/snapshots/${snapshot.id}/verify`, {
    ...JSON_INIT, body: JSON.stringify({ protocol: 1, origin: ORIGIN }),
  }, "snapshots.verify");
  await send(stack, "/api/v1/sync/gc", {
    ...JSON_INIT, body: JSON.stringify({ protocol: 1, origin: ORIGIN, apply: false }),
  }, "snapshots.gc");
  const current = (await db.prepare(
    "SELECT revision FROM origin_sync WHERE origin_host = ?").bind(ORIGIN).first()).revision;
  await send(stack, "/api/v1/sync/tombstones", {
    ...JSON_INIT,
    body: JSON.stringify({ protocol: 1, origin: ORIGIN, base_revision: current, tombstones: [] }),
  }, "tombstones.commit");
  await send(stack, `/api/v1/sync/learning/state?origin=${ORIGIN}`, {}, "learning.state");
  await send(stack, "/api/v1/sync/learning/deltas", {
    ...JSON_INIT,
    body: JSON.stringify({
      protocol: 1, origin: ORIGIN, base_revision: 0, digest: digest("delta"), changes: [],
    }),
  }, "learning.delta");
  await send(stack, "/api/v1/sync/learning/reconcile", {
    ...JSON_INIT,
    body: JSON.stringify({
      protocol: 1, origin: ORIGIN, action: "begin", base_revision: 0,
      digest: digest("reconcile"), records: 0,
    }),
  }, "learning.reconcile");
  await send(stack, "https://memories.17166969.xyz/mcp", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }),
  }, "mcp.request");
});

test("no operation event carries a token, assertion, digest query, or plaintext", async (t) => {
  const { stack } = await stackFor(t);
  const marker = "sensitive contract plaintext marker";
  const assertion = "sensitive-assertion-value";
  const query = "sensitive-query-value";
  const status = await send(stack, `/api/v1/status?token=${query}`, {
    headers: { "Cf-Access-Jwt-Assertion": assertion },
  }, "status.read");
  const change = structuredClone(LEARNING.cases.find((entry) => entry.name === "experience").change);
  change.claim = marker;
  change.applies_when = marker;
  change.digest = digest("sensitive-record");
  const delta = await send(stack, "/api/v1/sync/learning/deltas", {
    ...JSON_INIT,
    body: JSON.stringify({
      protocol: 1, origin: ORIGIN, base_revision: 0, digest: digest("sensitive-delta"), changes: [change],
    }),
  }, "learning.delta");
  const bodies = [
    JSON.stringify(messages(await stack.tailEvents())),
    await status.response.text(),
    await delta.response.text(),
  ];
  for (const body of bodies) {
    for (const value of [marker, assertion, query, COLLECTOR]) {
      assert.equal(body.includes(value), false, value);
    }
  }
});
