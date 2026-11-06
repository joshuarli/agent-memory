import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { after, before, test } from "node:test";

import { Miniflare } from "miniflare";

import { seedDatabase } from "../bench/seed.mjs";
import { MAX_JSON_BYTES, MAX_OBJECT_BYTES } from "../src/protocol.ts";
import { loadWorker } from "./harness.mjs";

const worker = await loadWorker();
const READER = "adversarial-reader@example.test";
const COLLECTOR = "adversarial-collector.access";
const OTHER = "adversarial-other.access";
const ORIGIN = "adversarial-a";
const OTHER_ORIGIN = "adversarial-b";
const MAX_JSON = MAX_JSON_BYTES;

let miniflare;
let db;
let bucket;
let env;
let server;
let base;

before(async () => {
  miniflare = new Miniflare({
    modules: true,
    script: "export default { fetch() { return new Response('ok'); } }",
    compatibilityDate: "2026-08-06",
    d1Databases: ["DB"],
    r2Buckets: ["BUCKET"],
  });
  db = await miniflare.getD1Database("DB");
  bucket = await miniflare.getR2Bucket("BUCKET");
  await seedDatabase(db);
  const insert = db.prepare(
    "INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)",
  );
  await insert.bind(COLLECTOR, ORIGIN, "adversarial", 0).run();
  await insert.bind(OTHER, OTHER_ORIGIN, "adversarial", 0).run();
  env = {
    READERS: READER,
    ACCESS_TEAM_DOMAIN: "team.example.test",
    ACCESS_AUD: "test-audience",
    DB: db,
    BUCKET: bucket,
  };
  server = createServer(async (request, response) => {
    try {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = Buffer.concat(chunks);
      const identity = typeof request.headers["cf-access-client-id"] === "string"
        ? { service_token_id: request.headers["cf-access-client-id"] }
        : typeof request.headers["x-test-reader"] === "string"
          ? { email: request.headers["x-test-reader"] }
          : null;
      const forwarded = new Request(`https://memories.test${request.url}`, {
        method: request.method,
        headers: request.headers,
        body: request.method === "GET" || request.method === "HEAD" || body.length === 0 ? undefined : body,
      });
      const result = await worker.fetch(forwarded, env, {
        access: { getIdentity: async () => identity },
      });
      response.writeHead(result.status, Object.fromEntries(result.headers));
      response.end(Buffer.from(await result.arrayBuffer()));
    } catch (error) {
      response.writeHead(500, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: String(error) }));
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  await new Promise((resolve) => server.close(resolve));
  await miniflare.dispose();
});

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

function ciphertext(data) {
  return Buffer.concat([Buffer.from("ciphertext:"), Buffer.from(data)]);
}

async function send(method, path, { body, token, reader = false, headers = {} } = {}) {
  const merged = { ...headers };
  if (token) merged["cf-access-client-id"] = token;
  if (reader) merged["x-test-reader"] = READER;
  const options = { method, headers: merged };
  if (method !== "GET" && method !== "HEAD") options.body = body;
  return await fetch(base + path, options);
}

async function json(method, path, value, options = {}) {
  const response = await send(method, path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
    body: method === "GET" || method === "HEAD" ? undefined : JSON.stringify(value),
  });
  const text = await response.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  return { response, body };
}

async function pair(label, disabled = false) {
  const origin = `adversarial-${label}`;
  const token = `token-${label}.access`;
  await db.prepare(
    "INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, ?)",
  ).bind(token, origin, label, 0, disabled ? 1 : 0).run();
  return { origin, token };
}

async function storeObject(data, token = COLLECTOR) {
  const cipher = ciphertext(data);
  const response = await send("PUT", `/api/v1/sync/objects/${digest(cipher)}`, {
    token,
    body: cipher,
    headers: {
      "X-Plaintext-Digest": digest(data),
      "X-Plaintext-Size": String(Buffer.byteLength(data)),
      "X-Encoding": "identity",
      "Content-Type": "application/octet-stream",
    },
  });
  const body = await response.json();
  return {
    response, body, plaintext: digest(data), size: Buffer.byteLength(data), cipher: digest(cipher),
  };
}

async function storeManifest(manifest, token = COLLECTOR) {
  return (await storeObject(JSON.stringify({ manifest: manifest.manifest_digest }), token)).cipher;
}

function manifestFor({ origin = ORIGIN, native = "native-1", chunks, artifacts = [], manifestObject }) {
  let offset = 0;
  const tiled = chunks.map((chunk) => {
    const entry = { offset, size: chunk.size, digest: chunk.digest };
    offset += chunk.size;
    return entry;
  });
  const sourceDigest = createHash("sha256").update(chunks.map((chunk) => chunk.digest).join("")).digest("hex");
  return {
    origin_host: origin,
    harness: "codex",
    native_id: native,
    source_path: `/synthetic/${native}.jsonl`,
    source_digest: sourceDigest,
    source_size: chunks.reduce((total, chunk) => total + chunk.size, 0),
    captured_at: 1762000000.5,
    chunks: tiled,
    artifacts,
    session_metadata: { cwd: "/synthetic", native },
    manifest_digest: digest(`manifest:${origin}:${native}:${sourceDigest}`),
    manifest_object: manifestObject,
  };
}

async function commit(manifest, baseRevision, token = COLLECTOR) {
  return await json("POST", "/api/v1/sync/generations", {
    protocol: 1, origin: manifest.origin_host, base_revision: baseRevision, manifest,
  }, { token });
}

async function generations(origin, token = COLLECTOR) {
  return await json("GET", `/api/v1/sync/generations?origin=${encodeURIComponent(origin)}`, null, { token });
}

async function count(table, where = "1 = 1", ...params) {
  return (await db.prepare(`SELECT count(*) AS count FROM ${table} WHERE ${where}`).bind(...params).first()).count;
}

function experience(id, origin, claim) {
  return {
    record: "experience", id, digest: digest(`record:${origin}:${id}`),
    project_key: "0123456789abcdef", project_label: "adversarial", kind: "method", claim,
    applies_when: "the adversarial suite runs", identifiers: `identifier-${id}`,
    fingerprint: `fingerprint-${id}`, basis: "deterministic", detector: "synthetic",
    revision: null, worktree: null, observed_at: 1000 - id, captured_at: 1000 - id,
    validity: "[]", tags: "", state: "retained", state_reason: null, state_at: null,
    sources: [{
      session: `codex:adv-${id}@${origin}`, position: 0, digest: digest(`source:${id}`), role: "trigger",
    }],
    links: [],
  };
}

async function learningDelta(origin, baseRevision, changes, token = COLLECTOR, seed = "delta") {
  return await json("POST", "/api/v1/sync/learning/deltas", {
    protocol: 1, origin, base_revision: baseRevision, digest: digest(seed), changes,
  }, { token });
}

function snapshotFor(chunks, objects, { seed = "snapshot" } = {}) {
  const dbDigest = digest(seed);
  const body = objects.map((object) => `${object.plaintext_digest} ${object.size}\n`).join("");
  return {
    id: dbDigest,
    created_at: 1762000000,
    schema_version: 10,
    db_digest: dbDigest,
    db_size: chunks.reduce((total, chunk) => total + chunk.size, 0),
    chunks,
    objects: objects.length,
    object_bytes: objects.reduce((total, object) => total + object.size, 0),
    objects_digest: digest(body),
  };
}

test("case 1: initial upload and full recovery", async () => {
  const { origin, token } = await pair("case-01");
  const data = Buffer.from("case 1 payload with a stable digest");
  const stored = await storeObject(data, token);
  assert.equal(stored.response.status, 200);
  const manifest = manifestFor({
    origin, chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  const committed = await commit(manifest, 0, token);
  assert.equal(committed.response.status, 200, JSON.stringify(committed.body));
  const page = await generations(origin, token);
  assert.equal(page.body.generations.length, 1);
  const downloaded = await send("GET", `/api/v1/sync/objects/${stored.cipher}`, { token });
  assert.equal(downloaded.status, 200);
  assert.deepEqual(Buffer.from(await downloaded.arrayBuffer()), ciphertext(data));
  const manifestObject = await send("GET", `/api/v1/sync/manifests/${manifest.manifest_object}`, { token });
  assert.equal(manifestObject.status, 200);
  assert.ok((await manifestObject.arrayBuffer()).byteLength > 0);
  assert.equal(await count("generation", "origin_host = ?", origin), 1);
  assert.equal(await count("remote_object", "origin_host = ?", origin), 2);
});

test("case 2: no-op sync and zero redundant writes", async () => {
  const { origin, token } = await pair("case-02");
  const data = Buffer.from("case 2 payload");
  const first = await storeObject(data, token);
  const second = await storeObject(data, token);
  assert.equal(second.response.status, 200);
  assert.equal(second.body.stored, false);
  assert.equal(await count("remote_object", "origin_host = ?", origin), 1);
  const manifest = manifestFor({
    origin, chunks: [{ digest: first.plaintext, size: first.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  assert.equal((await commit(manifest, 0, token)).response.status, 200);
  const duplicate = await commit(manifest, 0, token);
  assert.equal(duplicate.response.status, 200);
  assert.equal(duplicate.body.duplicate, true);
  assert.equal(duplicate.body.revision, 1);
  assert.equal(await count("generation", "origin_host = ?", origin), 1);
});

test("case 3: changed tail with minimal upload amplification", async () => {
  const { origin, token } = await pair("case-03");
  const head = await storeObject(Buffer.from("case 3 head"), token);
  const firstManifest = manifestFor({
    origin, native: "tail-1", chunks: [{ digest: head.plaintext, size: head.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  assert.equal((await commit(firstManifest, 0, token)).response.status, 200);
  const tailData = Buffer.from("case 3 tail");
  const missing = await json("POST", "/api/v1/sync/objects/missing", {
    protocol: 1, origin,
    objects: [{ plaintext_digest: head.plaintext, size: head.size },
      { plaintext_digest: digest(tailData), size: tailData.length }],
  }, { token });
  assert.equal(missing.response.status, 200);
  assert.deepEqual(missing.body.known.map((row) => row.plaintext_digest), [head.plaintext]);
  const tail = await storeObject(tailData, token);
  const secondManifest = manifestFor({
    origin, native: "tail-2",
    chunks: [{ digest: head.plaintext, size: head.size }, { digest: tail.plaintext, size: tail.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  assert.equal((await commit(secondManifest, 1, token)).response.status, 200);
  assert.equal(await count("generation", "origin_host = ?", origin), 2);
  assert.equal(await count("remote_object", "origin_host = ?", origin), 3);
});

test("case 4: interrupted uploads and retries", async () => {
  const { origin, token } = await pair("case-04");
  const data = Buffer.from("case 4 complete body");
  const cipher = ciphertext(data);
  const truncated = await send("PUT", `/api/v1/sync/objects/${digest(cipher)}`, {
    token,
    body: cipher.subarray(0, 5),
    headers: {
      "X-Plaintext-Digest": digest(data), "X-Plaintext-Size": String(data.length),
      "X-Encoding": "identity",
    },
  });
  assert.ok([400, 413].includes(truncated.status), String(truncated.status));
  assert.equal(await count("remote_object", "origin_host = ?", origin), 0);
  const retried = await storeObject(data, token);
  assert.equal(retried.response.status, 200);
  assert.equal(await count("remote_object", "origin_host = ?", origin), 1);
});

test("case 5: duplicate generation and learning delivery", async () => {
  const { origin, token } = await pair("case-05");
  const data = Buffer.from("case 5 payload");
  const stored = await storeObject(data, token);
  const manifest = manifestFor({
    origin, chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  assert.equal((await commit(manifest, 0, token)).response.status, 200);
  assert.equal((await commit(manifest, 0, token)).body.duplicate, true);
  const record = experience(1, origin, "case 5 duplicate learning record");
  const delta = await learningDelta(origin, 0, [record], token, "case-5");
  assert.equal(delta.response.status, 200);
  const replay = await learningDelta(origin, 0, [record], token, "case-5");
  assert.equal(replay.response.status, 200);
  assert.equal(replay.body.replayed, true);
  assert.equal(replay.body.applied, 0);
  assert.equal(await count("generation", "origin_host = ?", origin), 1);
  assert.equal(await count("learning_experience", "origin_host = ?", origin), 1);
});

test("case 6: colliding native IDs across origins", async () => {
  const first = await pair("case-06-a");
  const second = await pair("case-06-b");
  for (const [item, text] of [[first, "first origin"], [second, "second origin"]]) {
    const stored = await storeObject(Buffer.from(text), item.token);
    const manifest = manifestFor({
      origin: item.origin, native: "shared-native", chunks: [{ digest: stored.plaintext, size: stored.size }],
      manifestObject: await storeManifest(
        manifestFor({ origin: item.origin, chunks: [], manifestObject: "0".repeat(64) }), item.token),
    });
    assert.equal((await commit(manifest, 0, item.token)).response.status, 200);
  }
  const firstPage = await generations(first.origin, first.token);
  const secondPage = await generations(second.origin, second.token);
  assert.equal(firstPage.body.generations.length, 1);
  assert.equal(secondPage.body.generations.length, 1);
  assert.notEqual(firstPage.body.generations[0].manifest_digest, secondPage.body.generations[0].manifest_digest);
  assert.equal(await count("generation", "native_id = 'shared-native'"), 2);
});

test("case 7: concurrent collector updates", async () => {
  const { origin, token } = await pair("case-07");
  const chunks = [];
  for (const text of ["concurrent one", "concurrent two"]) {
    const stored = await storeObject(Buffer.from(text), token);
    chunks.push(manifestFor({
      origin, native: text.replaceAll(" ", "-"), chunks: [{ digest: stored.plaintext, size: stored.size }],
      manifestObject: await storeManifest(
        manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
    }));
  }
  const [left, right] = await Promise.all([commit(chunks[0], 0, token), commit(chunks[1], 0, token)]);
  assert.deepEqual([left.response.status, right.response.status].sort(), [200, 409]);
  const refused = left.response.status === 409 ? left : right;
  assert.equal(refused.body.error.code, "stale_revision");
  assert.equal(await count("generation", "origin_host = ?", origin), 1);
});

test("case 8: stale revision rejection", async () => {
  const { origin, token } = await pair("case-08");
  const stored = await storeObject(Buffer.from("case 8 payload"), token);
  const manifest = manifestFor({
    origin, native: "stale-1", chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  assert.equal((await commit(manifest, 0, token)).response.status, 200);
  const other = manifestFor({
    origin, native: "stale-2", chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  const stale = await commit(other, 0, token);
  assert.equal(stale.response.status, 409);
  assert.equal(stale.body.error.code, "stale_revision");
  assert.equal(await count("generation", "origin_host = ?", origin), 1);
});

test("case 9: tombstone-first processing", async () => {
  const { origin, token } = await pair("case-09");
  const stored = await storeObject(Buffer.from("case 9 payload"), token);
  const manifest = manifestFor({
    origin, native: "tombstoned-native", chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  const pushed = await json("POST", "/api/v1/sync/tombstones", {
    protocol: 1, origin, base_revision: 0,
    tombstones: [{ harness: "codex", native_id: "tombstoned-native", scope: "local", forgotten_at: 100 }],
  }, { token });
  assert.equal(pushed.response.status, 200);
  const refused = await commit(manifest, 1, token);
  assert.equal(refused.response.status, 410);
  assert.equal(refused.body.error.code, "tombstoned");
  assert.equal(await count("generation", "origin_host = ?", origin), 0);
  const page = await json("GET", `/api/v1/sync/tombstones?origin=${encodeURIComponent(origin)}`, null, { token });
  assert.equal(page.body.tombstones.length, 1);
});

test("case 10: no resurrection after restore or replay", async () => {
  const { origin, token } = await pair("case-10");
  const stored = await storeObject(Buffer.from("case 10 payload"), token);
  const manifest = manifestFor({
    origin, native: "forgotten-native", chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  await json("POST", "/api/v1/sync/tombstones", {
    protocol: 1, origin, base_revision: 0,
    tombstones: [{ harness: "codex", native_id: "forgotten-native", scope: "local", forgotten_at: 200 }],
  }, { token });
  const replay = await commit(manifest, 1, token);
  assert.equal(replay.response.status, 410);
  assert.equal(replay.body.error.code, "tombstoned");
  assert.equal((await generations(origin, token)).body.generations.length, 0);
  assert.equal(await count("generation", "origin_host = ?", origin), 0);
});

test("case 11: corrupt or missing R2 object detection", async () => {
  const { token } = await pair("case-11");
  const stored = await storeObject(Buffer.from("case 11 payload"), token);
  await bucket.delete(`v1/o/${stored.cipher}`);
  assert.equal(await bucket.head(`v1/o/${stored.cipher}`), null);
  const missing = await send("GET", `/api/v1/sync/objects/${stored.cipher}`, { token });
  assert.equal(missing.status, 404);
  const second = await storeObject(Buffer.from("case 11 corrupt me"), token);
  await bucket.put(`v1/o/${second.cipher}`, Buffer.alloc(ciphertext(Buffer.from("case 11 corrupt me")).length, 0x62));
  const corrupt = await send("GET", `/api/v1/sync/objects/${second.cipher}`, { token });
  assert.equal(corrupt.status, 200);
  const body = Buffer.from(await corrupt.arrayBuffer());
  assert.notEqual(digest(body), second.cipher);
  await bucket.put(`v1/o/${second.cipher}`, ciphertext(Buffer.from("case 11 corrupt me")));
});

test("case 12: missing-object manifest rejection", async () => {
  const { origin, token } = await pair("case-12");
  const manifest = manifestFor({
    origin, native: "missing-object",
    chunks: [{ digest: digest("never stored"), size: 12 }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  const refused = await commit(manifest, 0, token);
  assert.equal(refused.response.status, 409);
  assert.equal(refused.body.error.code, "missing_object");
  assert.equal(await count("generation", "origin_host = ?", origin), 0);
});

test("case 13: invalid durable acknowledgement rejection", async () => {
  const { origin, token } = await pair("case-13");
  const stored = await storeObject(Buffer.from("case 13 payload"), token);
  const manifest = manifestFor({
    origin, native: "ack-native", chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  assert.equal((await commit(manifest, 0, token)).response.status, 200);
  const before = await db.prepare(
    "SELECT manifest_object, revision FROM generation WHERE origin_host = ? AND manifest_digest = ?",
  ).bind(origin, manifest.manifest_digest).first();
  const tampered = {
    ...manifest,
    manifest_object: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "1".repeat(64) }), token),
  };
  const duplicate = await commit(tampered, 0, token);
  assert.equal(duplicate.response.status, 200);
  assert.equal(duplicate.body.duplicate, true);
  assert.equal(duplicate.body.manifest_digest, manifest.manifest_digest);
  const after = await db.prepare(
    "SELECT manifest_object, revision FROM generation WHERE origin_host = ? AND manifest_digest = ?",
  ).bind(origin, manifest.manifest_digest).first();
  assert.deepEqual(after, before);
  const malformed = await commit({ ...manifest, manifest_digest: "not-a-digest" }, 1, token);
  assert.equal(malformed.response.status, 400);
});

test("case 14: a failed request leaves committed state intact", async () => {
  const { origin, token } = await pair("case-14");
  const stored = await storeObject(Buffer.from("case 14 payload"), token);
  const manifest = manifestFor({
    origin, native: "offline-native", chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }), token),
  });
  assert.equal((await commit(manifest, 0, token)).response.status, 200);
  await assert.rejects(fetch("http://127.0.0.1:1/api/v1/status"));
  const page = await generations(origin, token);
  assert.equal(page.body.generations.length, 1);
  const object = await send("GET", `/api/v1/sync/objects/${stored.cipher}`, { token });
  assert.equal(object.status, 200);
});

test("case 15: unauthorized read and write rejection", async () => {
  const { origin } = await pair("case-15");
  const unknown = "unknown-token.access";
  assert.equal((await send("GET", "/api/v1/status")).status, 403);
  assert.equal((await send("GET", "/api/v1/status", { token: unknown })).status, 403);
  assert.equal((await send("PUT", "/api/v1/sync/objects/" + "a".repeat(64), {
    reader: true, body: Buffer.from("x"),
  })).status, 403);
  const forbidden = await json("POST", "/api/v1/sync/generations", {
    protocol: 1, origin, base_revision: 0, manifest: manifestFor({ origin, chunks: [], manifestObject: "0".repeat(64) }),
  }, { token: OTHER });
  assert.equal(forbidden.response.status, 403);
  assert.equal(await count("generation", "origin_host = ?", origin), 0);
});

test("case 16: authentication bypass attempts", async () => {
  const request = new Request("https://memories.test/api/v1/status");
  const absent = await worker.fetch(request, env, { access: { getIdentity: async () => null } });
  assert.equal(absent.status, 403);
  const forged = await worker.fetch(request, env, {
    access: { getIdentity: async () => ({ email: "attacker@example.test" }) },
  });
  assert.equal(forged.status, 403);
  const unknownToken = await worker.fetch(new Request("https://memories.test/api/v1/status", {
    headers: { "Cf-Access-Jwt-Assertion": "forged" },
  }), env, { access: { getIdentity: async () => ({ service_token_id: "not-enrolled.access" }) } });
  assert.equal(unknownToken.status, 403);
});

test("case 17: cross-origin data isolation", async () => {
  const first = await pair("case-17-a");
  const second = await pair("case-17-b");
  const stored = await storeObject(Buffer.from("case 17 payload"), first.token);
  const manifest = manifestFor({
    origin: first.origin, native: "isolated-native",
    chunks: [{ digest: stored.plaintext, size: stored.size }],
    manifestObject: await storeManifest(
      manifestFor({ origin: first.origin, chunks: [], manifestObject: "0".repeat(64) }), first.token),
  });
  assert.equal((await commit(manifest, 0, first.token)).response.status, 200);
  assert.equal((await send("GET", `/api/v1/sync/objects/${stored.cipher}`, { token: second.token })).status, 404);
  assert.equal((await send("GET", `/api/v1/sync/manifests/${manifest.manifest_object}`, {
    token: second.token,
  })).status, 404);
  assert.equal((await generations(first.origin, second.token)).response.status, 403);
  assert.equal((await json("GET", `/api/v1/sync/learning/state?origin=${encodeURIComponent(first.origin)}`,
    null, { token: second.token })).response.status, 403);
  assert.equal(await count("generation", "origin_host = ?", second.origin), 0);
});

test("case 18: untrusted HTML escaping", async () => {
  const { origin, token } = await pair("case-18");
  const hostile = `<script>alert("case 18")</script> & "quotes" 'apostrophe'`;
  const delta = await learningDelta(origin, 0, [experience(18, origin, hostile)], token, "case-18");
  assert.equal(delta.response.status, 200, JSON.stringify(delta.body));
  const page = await send("GET", "/search?state=all", { reader: true });
  const html = await page.text();
  assert.equal(page.status, 200);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&quot;/);
});

test("case 19: imported advice never gains authority", async () => {
  const { origin, token } = await pair("case-19");
  const record = experience(19, origin, "case 19 advisory record");
  const refused = await learningDelta(origin, 0, [{ ...record, authority: "instruction" }], token, "case-19-bad");
  assert.equal(refused.response.status, 400);
  assert.equal(await count("learning_experience", "origin_host = ?", origin), 0);
  assert.equal((await learningDelta(origin, 0, [record], token, "case-19-good")).response.status, 200);
  const columns = (await db.prepare(
    `SELECT name FROM pragma_table_info('learning_experience')
     UNION ALL SELECT name FROM pragma_table_info('learning_memory')`,
  ).all()).results.map((row) => row.name);
  for (const forbidden of ["authority", "instruction", "grant", "enabled"]) {
    assert.equal(columns.includes(forbidden), false, forbidden);
  }
  const page = await send("GET", `/m/${origin}/experience/19`, { reader: true });
  const html = await page.text();
  assert.equal(page.status, 200);
  assert.equal(/class="marker instruction"/.test(html), false);
  assert.match(html, /class="marker (reviewed|observed|advisory)"/);
});

test("case 20: encryption rotation replaces the ciphertext mapping", async () => {
  const { origin, token } = await pair("case-20");
  const data = Buffer.from("case 20 rotate this payload");
  const first = await storeObject(data, token);
  const replacement = ciphertext(Buffer.from("case 20 replacement ciphertext"));
  const stored = await send("PUT", `/api/v1/sync/objects/${digest(replacement)}`, {
    token,
    body: replacement,
    headers: {
      "X-Plaintext-Digest": first.plaintext, "X-Plaintext-Size": String(first.size), "X-Encoding": "identity",
    },
  });
  assert.equal(stored.status, 200);
  const swapped = await json("POST", `/api/v1/sync/objects/${first.plaintext}/replace`, {
    protocol: 1, origin, plaintext_digest: first.plaintext, plaintext_size: first.size,
    encoding: "identity", ciphertext_digest: digest(replacement), ciphertext_size: replacement.length,
  }, { token });
  assert.equal(swapped.response.status, 200, JSON.stringify(swapped.body));
  assert.equal((await send("GET", `/api/v1/sync/objects/${first.cipher}`, { token })).status, 404);
  const rotated = await send("GET", `/api/v1/sync/objects/${digest(replacement)}`, { token });
  assert.equal(rotated.status, 200);
  assert.deepEqual(Buffer.from(await rotated.arrayBuffer()), replacement);
});

test("case 21: consistent restore of canonical records", async () => {
  const { origin, token } = await pair("case-21");
  const chunkOne = await storeObject(Buffer.from("case 21 chunk one"), token);
  const chunkTwo = await storeObject(Buffer.from("case 21 chunk two"), token);
  const pinned = await storeObject(Buffer.from("case 21 pinned object"), token);
  const snapshot = snapshotFor(
    [
      { plaintext_digest: chunkOne.plaintext, size: chunkOne.size, ciphertext_digest: chunkOne.cipher },
      { plaintext_digest: chunkTwo.plaintext, size: chunkTwo.size, ciphertext_digest: chunkTwo.cipher },
    ],
    [{ plaintext_digest: pinned.plaintext, size: pinned.size }],
  );
  const page = await json("POST", `/api/v1/sync/snapshots/${snapshot.id}/objects`, {
    protocol: 1, origin, page: 0, objects: [{ plaintext_digest: pinned.plaintext, size: pinned.size }],
  }, { token });
  assert.equal(page.response.status, 200, JSON.stringify(page.body));
  const committed = await json("POST", "/api/v1/sync/snapshots", {
    protocol: 1, origin, base_revision: 0, snapshot,
  }, { token });
  assert.equal(committed.response.status, 200, JSON.stringify(committed.body));
  const detail = await json(
    "GET", `/api/v1/sync/snapshots/${snapshot.id}?origin=${encodeURIComponent(origin)}`, null, { token });
  assert.equal(detail.response.status, 200);
  assert.deepEqual(detail.body.chunks.map((chunk) => chunk.ordinal), [0, 1]);
  const objects = await json(
    "GET", `/api/v1/sync/snapshots/${snapshot.id}/objects?page=0`, null, { token });
  assert.equal(objects.response.status, 200, JSON.stringify(objects.body));
  assert.equal(objects.body.objects.length, 1);
  for (const chunk of detail.body.chunks) {
    assert.equal((await send("GET", `/api/v1/sync/objects/${chunk.ciphertext_digest}`, { token })).status, 200);
  }
  const verified = await json("POST", `/api/v1/sync/snapshots/${snapshot.id}/verify`, {
    protocol: 1, origin,
  }, { token });
  assert.equal(verified.response.status, 200);
  await bucket.delete(`v1/o/${detail.body.chunks[0].ciphertext_digest}`);
  assert.equal((await send("GET", `/api/v1/sync/objects/${detail.body.chunks[0].ciphertext_digest}`, {
    token,
  })).status, 404);
});

test("case 22: large and malformed request rejection", async () => {
  const { token } = await pair("case-22");
  const oversized = await send("POST", "/api/v1/sync/hello", {
    token,
    body: Buffer.alloc(MAX_JSON + 1, 0x20),
    headers: { "Content-Type": "application/json" },
  });
  assert.equal(oversized.status, 413);
  const malformed = await send("POST", "/api/v1/sync/hello", {
    token, body: "{", headers: { "Content-Type": "application/json" },
  });
  assert.equal(malformed.status, 400);
  const wrongMethod = await send("POST", "/api/v1/status", { token });
  assert.equal(wrongMethod.status, 405);
  const hugeObject = await send("PUT", `/api/v1/sync/objects/${"b".repeat(64)}`, {
    token,
    body: Buffer.from("small"),
    headers: {
      "X-Plaintext-Digest": "c".repeat(64), "X-Plaintext-Size": String(MAX_OBJECT_BYTES + 1), "X-Encoding": "identity",
    },
  });
  assert.equal(hugeObject.status, 400);
});

test("case 23: bounded memory and body limits", async () => {
  const { origin, token } = await pair("case-23");
  const eight = Buffer.alloc(MAX_OBJECT_BYTES, 0x61);
  const stored = await send("PUT", `/api/v1/sync/objects/${digest(eight)}`, {
    token,
    body: eight,
    headers: {
      "X-Plaintext-Digest": digest(eight), "X-Plaintext-Size": String(eight.length), "X-Encoding": "identity",
    },
  });
  assert.equal(stored.status, 200);
  assert.equal((await stored.json()).object.plaintext_size, eight.length);
  const over = await send("PUT", `/api/v1/sync/objects/${"d".repeat(64)}`, {
    token,
    body: Buffer.from("tiny"),
    headers: {
      "X-Plaintext-Digest": digest(eight), "X-Plaintext-Size": String(MAX_OBJECT_BYTES + 1),
      "X-Encoding": "identity",
    },
  });
  assert.equal(over.status, 400);
  assert.equal(await count("remote_object", "origin_host = ?", origin), 1);
});

test("case 24: collector concurrency and backpressure", async () => {
  const collectors = [];
  for (let index = 0; index < 10; index += 1) {
    collectors.push(await pair("case-24-" + String(index).padStart(2, "0")));
  }
  const commits = await Promise.all(collectors.map(async (item, index) => {
    const stored = await storeObject(Buffer.from(`case 24 payload ${index}`), item.token);
    const manifest = manifestFor({
      origin: item.origin, native: `load-${index}`,
      chunks: [{ digest: stored.plaintext, size: stored.size }],
      manifestObject: await storeManifest(
        manifestFor({ origin: item.origin, chunks: [], manifestObject: "0".repeat(64) }), item.token),
    });
    return { ...(await commit(manifest, 0, item.token)), manifest };
  }));
  assert.deepEqual(commits.map((item) => item.response.status), Array.from({ length: 10 }, () => 200));
  const replay = await Promise.all(commits.map(async (item, index) => {
    const page = await generations(collectors[index].origin, collectors[index].token);
    assert.equal(page.body.generations.length, 1);
    return page.body.generations[0];
  }));
  assert.equal(replay.length, 10);
  const total = await count("generation", "origin_host LIKE 'adversarial-case-24-%'");
  assert.equal(total, 10);
  const duplicated = await Promise.all(commits.map(async (item, index) =>
    await commit(item.manifest, 1, collectors[index].token)));
  assert.deepEqual(duplicated.map((item) => item.body.duplicate), Array.from({ length: 10 }, () => true));
  assert.equal(await count("generation", "origin_host LIKE 'adversarial-case-24-%'"), 10);
});
