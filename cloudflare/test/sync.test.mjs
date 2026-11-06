import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { Miniflare } from "miniflare";

import { seedDatabase } from "../bench/seed.mjs";
import { loadWorker, request } from "./harness.mjs";
import { MAX_CIPHERTEXT_BYTES, MAX_JSON_BYTES, MAX_OBJECT_BYTES } from "../src/protocol.ts";

const worker = await loadWorker();
const COLLECTOR = "collector-client.access";
const OTHER_COLLECTOR = "other-client.access";
const ORIGIN = "origin-a";
const OTHER_ORIGIN = "origin-b";

function digest(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function runtime(t) {
  const miniflare = new Miniflare({
    modules: true,
    script: "export default { fetch() { return new Response('ok'); } }",
    compatibilityDate: "2026-08-06",
    d1Databases: ["DB"],
    r2Buckets: ["BUCKET"],
  });
  t.after(() => miniflare.dispose());
  const db = await miniflare.getD1Database("DB");
  await seedDatabase(db);
  const collector = db.prepare(
    "INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)",
  );
  await collector.bind(COLLECTOR, ORIGIN, "test", 0).run();
  await collector.bind(OTHER_COLLECTOR, OTHER_ORIGIN, "test", 0).run();
  return {
    db,
    bucket: await miniflare.getR2Bucket("BUCKET"),
    env: {
      READERS: "reader@example.test",
      ACCESS_TEAM_DOMAIN: "team.example.test",
      ACCESS_AUD: "test-audience",
      DB: db,
      BUCKET: await miniflare.getR2Bucket("BUCKET"),
    },
  };
}

function collector(client = COLLECTOR) {
  return { access: { getIdentity: async () => ({ service_token_id: client }) } };
}

const reader = { access: { getIdentity: async () => ({ email: "reader@example.test" }) } };

function postJson(path, value) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  });
}

function objectRequest(ciphertext, body, headers = {}) {
  return request(`/api/v1/sync/objects/${ciphertext}`, {
    method: "PUT",
    headers: {
      "X-Plaintext-Digest": digest("synthetic plaintext"),
      "X-Plaintext-Size": String(body.byteLength),
      "X-Encoding": "identity",
      ...headers,
    },
    body,
  });
}

test("hello binds the collector origin and is read-only", async (t) => {
  const { db, env } = await runtime(t);
  const response = await worker.fetch(postJson("/api/v1/sync/hello", { protocol: 1, origin: ORIGIN }), env, collector());
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.match(response.headers.get("server-timing"), /d1;dur=/);
  assert.deepEqual(await response.json(), {
    protocol: 1,
    store_id: "usr-memories",
    origin: ORIGIN,
    revision: 0,
    tombstone_watermark: 0,
    learning: { revision: 0, digest: null, records: 0 },
  });
  assert.equal((await db.prepare("SELECT count(*) AS count FROM origin_sync").first()).count, 0);
});

test("hello refuses a reader, an unbound origin, and an unsupported version", async (t) => {
  const { db, env } = await runtime(t);
  const readerResponse = await worker.fetch(postJson("/api/v1/sync/hello", { protocol: 1, origin: ORIGIN }), env, reader);
  assert.equal(readerResponse.status, 403);
  assert.equal((await readerResponse.json()).error.code, "forbidden");

  const unbound = await worker.fetch(postJson("/api/v1/sync/hello", { protocol: 1, origin: "origin-b" }), env, collector());
  assert.equal(unbound.status, 403);

  for (const body of [
    { protocol: 2, origin: ORIGIN },
    { protocol: "1", origin: ORIGIN },
    { origin: ORIGIN },
    { protocol: 1, origin: "../other" },
    { protocol: 1, origin: ORIGIN, path: "/private" },
  ]) {
    const response = await worker.fetch(postJson("/api/v1/sync/hello", body), env, collector());
    assert.equal(response.status, 400);
    assert.ok(["unsupported_version", "invalid_request"].includes((await response.json()).error.code));
  }
  assert.equal((await db.prepare("SELECT count(*) AS count FROM origin_sync").first()).count, 0);
});

test("missing objects are scoped to the collector origin", async (t) => {
  const { db, env } = await runtime(t);
  const known = digest("known");
  const absent = digest("absent");
  const foreign = digest("foreign");
  const insert = db.prepare(
    "INSERT INTO remote_object(plaintext_digest, plaintext_size, encoding, ciphertext_digest, ciphertext_size, origin_host, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
  );
  await insert.bind(known, 5, "identity", digest("known-ciphertext"), 40, ORIGIN, 1).run();
  await insert.bind(foreign, 6, "identity", digest("foreign-ciphertext"), 41, OTHER_ORIGIN, 1).run();
  const response = await worker.fetch(postJson("/api/v1/sync/objects/missing", {
    protocol: 1,
    origin: ORIGIN,
    objects: [
      { plaintext_digest: known, size: 5 },
      { plaintext_digest: absent, size: 6 },
      { plaintext_digest: foreign, size: 6 },
    ],
  }), env, collector());
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.deepEqual(body.missing.map((object) => object.plaintext_digest), [absent, foreign]);
  assert.equal(body.known.length, 1);
  assert.equal(body.known[0].plaintext_digest, known);
  assert.equal(body.known[0].ciphertext_digest, digest("known-ciphertext"));
});

test("missing objects returns every known object across query chunks", async (t) => {
  const { db, env } = await runtime(t);
  const insert = db.prepare(
    "INSERT INTO remote_object(plaintext_digest, plaintext_size, encoding, ciphertext_digest, ciphertext_size, origin_host, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
  );
  const known = Array.from({ length: 181 }, (_, index) => ({
    plaintext_digest: digest("bulk-" + String(index)),
    size: index + 1,
  }));
  await db.batch(known.map((object) => insert.bind(
    object.plaintext_digest, object.size, "identity", digest("bulk-ciphertext-" + object.plaintext_digest),
    object.size + 40, ORIGIN, 1,
  )));
  const absent = { plaintext_digest: digest("bulk-absent"), size: 1 };
  const response = await worker.fetch(postJson("/api/v1/sync/objects/missing", {
    protocol: 1,
    origin: ORIGIN,
    objects: [...known, absent].map((object) => ({
      plaintext_digest: object.plaintext_digest,
      size: object.size,
    })),
  }), env, collector());
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.known.length, known.length);
  assert.deepEqual(body.missing.map((object) => object.plaintext_digest), [absent.plaintext_digest]);
});

test("wrong methods are refused with an Allow header", async (t) => {
  const { env } = await runtime(t);
  const hello = await worker.fetch(request("/api/v1/sync/hello"), env, collector());
  assert.equal(hello.status, 405);
  assert.equal(hello.headers.get("allow"), "POST");
  assert.equal((await hello.json()).error.code, "method_not_allowed");

  const missing = await worker.fetch(request("/api/v1/sync/objects/missing"), env, collector());
  assert.equal(missing.status, 405);
  assert.equal(missing.headers.get("allow"), "POST");

  const object = await worker.fetch(request(`/api/v1/sync/objects/${"a".repeat(64)}`, { method: "POST" }), env, collector());
  assert.equal(object.status, 405);
  assert.equal(object.headers.get("allow"), "GET, PUT");

  const invalidDigest = await worker.fetch(request("/api/v1/sync/objects/not-a-digest"), env, collector());
  assert.equal(invalidDigest.status, 404);
  const readerObject = await worker.fetch(request(`/api/v1/sync/objects/${"a".repeat(64)}`), env, reader);
  assert.equal(readerObject.status, 403);
});

test("a malformed or oversized JSON body is refused without a write", async (t) => {
  const { db, env } = await runtime(t);
  const malformed = await worker.fetch(request("/api/v1/sync/hello", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{",
  }), env, collector());
  assert.equal(malformed.status, 400);
  assert.equal((await malformed.json()).error.code, "invalid_request");

  const oversized = await worker.fetch(request("/api/v1/sync/hello", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: new Uint8Array(MAX_JSON_BYTES + 1),
  }), env, collector());
  assert.equal(oversized.status, 413);
  assert.equal((await oversized.json()).error.code, "payload_too_large");
  assert.equal((await db.prepare("SELECT count(*) AS count FROM origin_sync").first()).count, 0);
});

test("object upload is idempotent, checksum-verified, and origin-scoped", async (t) => {
  const { db, bucket, env } = await runtime(t);
  const ciphertext = Buffer.from("synthetic ciphertext bytes");
  const ciphertextDigest = digest(ciphertext);
  const first = await worker.fetch(objectRequest(ciphertextDigest, ciphertext), env, collector());
  assert.equal(first.status, 200);
  const firstBody = await first.json();
  assert.equal(firstBody.stored, true);
  assert.equal(firstBody.object.ciphertext_digest, ciphertextDigest);
  assert.notEqual(await bucket.head(`v1/o/${ciphertextDigest}`), null);
  assert.equal((await db.prepare("SELECT count(*) AS count FROM remote_object").first()).count, 1);

  const second = await worker.fetch(objectRequest(ciphertextDigest, ciphertext), env, collector());
  assert.equal(second.status, 200);
  assert.equal((await second.json()).stored, false);

  const read = await worker.fetch(request(`/api/v1/sync/objects/${ciphertextDigest}`), env, collector());
  assert.equal(read.status, 200);
  assert.equal(read.headers.get("x-plaintext-digest"), digest("synthetic plaintext"));
  assert.equal(read.headers.get("x-encoding"), "identity");
  assert.deepEqual(Buffer.from(await read.arrayBuffer()), ciphertext);

  const foreign = await worker.fetch(request(`/api/v1/sync/objects/${ciphertextDigest}`), env, collector(OTHER_COLLECTOR));
  assert.equal(foreign.status, 404);

  const corrupt = await worker.fetch(objectRequest(digest("different"), ciphertext), env, collector());
  assert.equal(corrupt.status, 400);
  assert.equal((await db.prepare("SELECT count(*) AS count FROM remote_object").first()).count, 1);

  const conflict = await worker.fetch(objectRequest(ciphertextDigest, ciphertext, {
    "X-Plaintext-Digest": digest("another plaintext"),
  }), env, collector());
  assert.equal(conflict.status, 409);
  assert.equal((await conflict.json()).error.code, "conflict");
  assert.equal((await db.prepare("SELECT count(*) AS count FROM remote_object").first()).count, 1);
});

test("object uploads stream without a content length and refuse an oversized body", async (t) => {
  const { env } = await runtime(t);
  const ciphertext = Buffer.from("synthetic ciphertext bytes");
  const ciphertextDigest = digest(ciphertext);
  const headers = {
    "X-Plaintext-Digest": digest("synthetic plaintext"),
    "X-Plaintext-Size": String(ciphertext.byteLength),
    "X-Encoding": "identity",
  };
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(ciphertext);
      controller.close();
    },
  });
  const chunked = await worker.fetch(new Request(`https://memories.test/api/v1/sync/objects/${ciphertextDigest}`, {
    method: "PUT",
    headers,
    body: stream,
    duplex: "half",
  }), env, collector());
  assert.equal(chunked.status, 200);
  assert.equal((await chunked.json()).stored, true);

  const oversizedDigest = digest("oversized ciphertext");
  const oversizedStream = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(MAX_CIPHERTEXT_BYTES + 1));
      controller.close();
    },
  });
  const oversized = await worker.fetch(new Request(`https://memories.test/api/v1/sync/objects/${oversizedDigest}`, {
    method: "PUT",
    headers: { ...headers, "X-Plaintext-Size": String(MAX_OBJECT_BYTES) },
    body: oversizedStream,
    duplex: "half",
  }), env, collector());
  assert.equal(oversized.status, 413);
  assert.equal((await oversized.json()).error.code, "payload_too_large");
});
