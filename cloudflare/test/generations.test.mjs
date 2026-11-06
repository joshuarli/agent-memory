import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { Miniflare } from "miniflare";

import { seedDatabase } from "../bench/seed.mjs";
import { loadWorker, request } from "./harness.mjs";

const worker = await loadWorker();
const COLLECTOR = "collector-client.access";
const OTHER_COLLECTOR = "other-client.access";
const ORIGIN = "origin-a";
const OTHER_ORIGIN = "origin-b";

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
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

function postJson(path, value) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  });
}

async function storeObject(env, data, plaintextDigest = digest(data), client = COLLECTOR) {
  const ciphertext = Buffer.concat([Buffer.from("ciphertext:"), Buffer.from(data)]);
  const ciphertextDigest = digest(ciphertext);
  const response = await worker.fetch(request(`/api/v1/sync/objects/${ciphertextDigest}`, {
    method: "PUT",
    headers: {
      "X-Plaintext-Digest": plaintextDigest,
      "X-Plaintext-Size": String(Buffer.byteLength(data)),
      "X-Encoding": "identity",
    },
    body: ciphertext,
  }), env, collector(client));
  assert.equal(response.status, 200, await response.clone().text());
  return { plaintext: plaintextDigest, ciphertext: ciphertextDigest };
}

function manifestFor({ origin = ORIGIN, harness = "codex", native = "native-1", chunks, artifacts = [], manifestObject }) {
  const sourceSize = chunks.reduce((total, chunk) => total + chunk.size, 0);
  const sourceDigest = createHash("sha256")
    .update(chunks.map((chunk) => chunk.digest).join(""))
    .digest("hex");
  let offset = 0;
  const tiled = chunks.map((chunk) => {
    const entry = { offset, size: chunk.size, digest: chunk.digest };
    offset += chunk.size;
    return entry;
  });
  return {
    origin_host: origin,
    harness,
    native_id: native,
    source_path: `/synthetic/sessions/${native}.jsonl`,
    source_digest: sourceDigest,
    source_size: sourceSize,
    captured_at: 1762000000.5,
    chunks: tiled,
    artifacts,
    session_metadata: { cwd: "/synthetic", native },
    manifest_digest: digest(`manifest:${origin}:${native}:${sourceDigest}`),
    manifest_object: manifestObject,
  };
}

async function storeManifest(env, manifest, client = COLLECTOR) {
  return (await storeObject(env, JSON.stringify({ manifest: manifest.manifest_digest }), undefined, client)).ciphertext;
}

async function commit(env, manifest, baseRevision, client = COLLECTOR) {
  const response = await worker.fetch(postJson("/api/v1/sync/generations", {
    protocol: 1,
    origin: manifest.origin_host,
    base_revision: baseRevision,
    manifest,
  }), env, collector(client));
  return { response, body: await response.json() };
}

function count(db, table) {
  return db.prepare(`SELECT count(*) AS count FROM ${table}`).first();
}

test("a generation commits after its objects and re-acks without a write", async (t) => {
  const { db, env } = await runtime(t);
  const first = Buffer.from("first synthetic chunk");
  const second = Buffer.from("second synthetic chunk");
  const chunks = [
    { digest: (await storeObject(env, first)).plaintext, size: first.byteLength },
    { digest: (await storeObject(env, second)).plaintext, size: second.byteLength },
  ];
  const artifact = Buffer.from("synthetic artifact");
  const artifactObject = await storeObject(env, artifact);
  const manifest = manifestFor({
    chunks,
    artifacts: [{ name: "summary.json", digest: artifactObject.plaintext, size: artifact.byteLength }],
    manifestObject: "pending",
  });
  manifest.manifest_object = await storeManifest(env, manifest);
  const committed = await commit(env, manifest, 0);
  assert.equal(committed.response.status, 200, JSON.stringify(committed.body));
  assert.equal(committed.body.duplicate, false);
  assert.equal(committed.body.revision, 1);
  assert.equal(committed.body.manifest_digest, manifest.manifest_digest);
  assert.equal((await count(db, "generation")).count, 1);
  assert.equal((await count(db, "generation_object")).count, 3);
  assert.equal((await db.prepare("SELECT revision FROM origin_sync WHERE origin_host = ?").bind(ORIGIN).first()).revision, 1);

  const duplicate = await commit(env, manifest, 99);
  assert.equal(duplicate.response.status, 200);
  assert.equal(duplicate.body.duplicate, true);
  assert.equal(duplicate.body.generation, committed.body.generation);
  assert.equal(duplicate.body.revision, 1);
  assert.equal((await count(db, "generation")).count, 1);
  assert.equal((await count(db, "generation_object")).count, 3);
  assert.equal((await db.prepare("SELECT revision FROM origin_sync WHERE origin_host = ?").bind(ORIGIN).first()).revision, 1);
});

test("a stale revision commits nothing", async (t) => {
  const { db, env } = await runtime(t);
  const firstData = Buffer.from("first generation");
  const first = manifestFor({ chunks: [{ digest: (await storeObject(env, firstData)).plaintext, size: firstData.byteLength }] });
  first.manifest_object = await storeManifest(env, first);
  assert.equal((await commit(env, first, 0)).response.status, 200);

  const secondData = Buffer.from("second generation");
  const second = manifestFor({
    native: "native-2",
    chunks: [{ digest: (await storeObject(env, secondData)).plaintext, size: secondData.byteLength }],
  });
  second.manifest_object = await storeManifest(env, second);
  const stale = await commit(env, second, 0);
  assert.equal(stale.response.status, 409);
  assert.equal(stale.body.error.code, "stale_revision");
  assert.equal((await count(db, "generation")).count, 1);
  assert.equal((await count(db, "generation_object")).count, 1);
  assert.equal((await db.prepare("SELECT revision FROM origin_sync WHERE origin_host = ?").bind(ORIGIN).first()).revision, 1);
});

test("a manifest that names an unstored object is refused", async (t) => {
  const { db, env } = await runtime(t);
  const missing = digest("never uploaded");
  const manifest = manifestFor({ chunks: [{ digest: missing, size: 12 }] });
  manifest.manifest_object = await storeManifest(env, manifest);
  const response = await commit(env, manifest, 0);
  assert.equal(response.response.status, 409);
  assert.equal(response.body.error.code, "missing_object");
  assert.equal((await count(db, "generation")).count, 0);
  assert.equal((await count(db, "origin_sync")).count, 0);

  const absentManifest = manifestFor({
    chunks: [{ digest: (await storeObject(env, Buffer.from("present chunk"))).plaintext, size: 13 }],
    manifestObject: digest("never uploaded manifest"),
  });
  const absent = await commit(env, absentManifest, 0);
  assert.equal(absent.response.status, 409);
  assert.equal(absent.body.error.code, "missing_object");
  assert.equal((await count(db, "generation")).count, 0);
});

test("tombstones commit first, are idempotent, and block later generations", async (t) => {
  const { db, env } = await runtime(t);
  const firstPush = await worker.fetch(postJson("/api/v1/sync/tombstones", {
    protocol: 1,
    origin: ORIGIN,
    base_revision: 0,
    tombstones: [{ harness: "codex", native_id: "native-1", scope: "global", forgotten_at: 100 }],
  }), env, collector());
  assert.equal(firstPush.status, 200);
  assert.deepEqual(await firstPush.json(), {
    protocol: 1,
    origin: ORIGIN,
    revision: 1,
    applied: [{ harness: "codex", native_id: "native-1" }],
    ignored: [],
  });

  const stalePush = await worker.fetch(postJson("/api/v1/sync/tombstones", {
    protocol: 1,
    origin: ORIGIN,
    base_revision: 0,
    tombstones: [{ harness: "claude", native_id: "native-stale", scope: "global", forgotten_at: 100 }],
  }), env, collector());
  assert.equal(stalePush.status, 409);
  assert.equal((await stalePush.json()).error.code, "stale_revision");
  assert.equal((await count(db, "tombstone")).count, 1);

  const repeated = await worker.fetch(postJson("/api/v1/sync/tombstones", {
    protocol: 1,
    origin: ORIGIN,
    base_revision: 9,
    tombstones: [{ harness: "codex", native_id: "native-1", scope: "global", forgotten_at: 100 }],
  }), env, collector());
  assert.equal(repeated.status, 200);
  const repeatedBody = await repeated.json();
  assert.deepEqual(repeatedBody.applied, []);
  assert.equal(repeatedBody.revision, 1);
  assert.equal((await db.prepare("SELECT revision FROM origin_sync WHERE origin_host = ?").bind(ORIGIN).first()).revision, 1);

  const data = Buffer.from("tombstoned generation");
  const manifest = manifestFor({ chunks: [{ digest: (await storeObject(env, data)).plaintext, size: data.byteLength }] });
  manifest.manifest_object = await storeManifest(env, manifest);
  const blocked = await commit(env, manifest, 1);
  assert.equal(blocked.response.status, 410);
  assert.equal(blocked.body.error.code, "tombstoned");
  assert.equal((await count(db, "generation")).count, 0);

  const newer = await worker.fetch(postJson("/api/v1/sync/tombstones", {
    protocol: 1,
    origin: ORIGIN,
    base_revision: 1,
    tombstones: [{ harness: "codex", native_id: "native-1", scope: "local", forgotten_at: 200 }],
  }), env, collector());
  assert.equal(newer.status, 200);
  assert.deepEqual((await newer.json()).applied, [{ harness: "codex", native_id: "native-1" }]);

  const older = await worker.fetch(postJson("/api/v1/sync/tombstones", {
    protocol: 1,
    origin: ORIGIN,
    base_revision: 2,
    tombstones: [{ harness: "codex", native_id: "native-1", scope: "global", forgotten_at: 150 }],
  }), env, collector());
  assert.equal(older.status, 200);
  assert.deepEqual((await older.json()).applied, []);

  const page = await worker.fetch(request(`/api/v1/sync/tombstones?origin=${ORIGIN}`), env, collector());
  assert.equal(page.status, 200);
  const body = await page.json();
  assert.equal(body.tombstones.length, 1);
  assert.equal(body.tombstones[0].scope, "local");
  assert.equal(body.tombstones[0].forgotten_at, 200);
  assert.equal(body.tombstones[0].revision, 2);
  assert.equal(body.watermark, 2);
});

test("tombstone pages use an opaque cursor and only advance the watermark over returned rows", async (t) => {
  const { env } = await runtime(t);
  const entries = [
    { harness: "codex", native_id: "native-1", scope: "global", forgotten_at: 1 },
    { harness: "claude", native_id: "native-2", scope: "local", forgotten_at: 2 },
  ];
  const pushed = await worker.fetch(postJson("/api/v1/sync/tombstones", {
    protocol: 1, origin: ORIGIN, base_revision: 0, tombstones: entries,
  }), env, collector());
  assert.equal(pushed.status, 200);

  const first = await worker.fetch(request(`/api/v1/sync/tombstones?origin=${ORIGIN}&limit=1`), env, collector());
  const firstBody = await first.json();
  assert.equal(firstBody.tombstones.length, 1);
  assert.equal(firstBody.watermark, 1);
  assert.ok(firstBody.next_cursor);

  const second = await worker.fetch(request(
    `/api/v1/sync/tombstones?origin=${ORIGIN}&limit=1&after=${encodeURIComponent(firstBody.next_cursor)}`,
  ), env, collector());
  const secondBody = await second.json();
  assert.equal(secondBody.tombstones.length, 1);
  assert.equal(secondBody.watermark, 1);
  assert.equal(secondBody.next_cursor, null);
});

test("generation pages are keyed by id, scoped, and exclude tombstoned sessions", async (t) => {
  const { env } = await runtime(t);
  const manifests = [];
  for (const native of ["native-1", "native-2"]) {
    const data = Buffer.from(`generation ${native}`);
    const manifest = manifestFor({
      native,
      chunks: [{ digest: (await storeObject(env, data)).plaintext, size: data.byteLength }],
    });
    manifest.manifest_object = await storeManifest(env, manifest);
    const committed = await commit(env, manifest, manifests.length);
    assert.equal(committed.response.status, 200, JSON.stringify(committed.body));
    manifests.push(manifest);
  }
  const first = await worker.fetch(request(`/api/v1/sync/generations?origin=${ORIGIN}`), env, collector());
  const firstBody = await first.json();
  assert.deepEqual(firstBody.generations.map((row) => row.native_id), ["native-1", "native-2"]);
  assert.equal(firstBody.next_cursor, null);

  const page = await worker.fetch(request(
    `/api/v1/sync/generations?origin=${ORIGIN}&after=${firstBody.generations[0].id}&limit=1`,
  ), env, collector());
  const pageBody = await page.json();
  assert.deepEqual(pageBody.generations.map((row) => row.native_id), ["native-2"]);
  assert.equal(pageBody.next_cursor, null);

  const byManifest = await worker.fetch(request(
    `/api/v1/sync/generations?origin=${ORIGIN}&manifest=${manifests[0].manifest_digest}`,
  ), env, collector());
  assert.deepEqual((await byManifest.json()).generations.map((row) => row.native_id), ["native-1"]);

  const tombstone = await worker.fetch(postJson("/api/v1/sync/tombstones", {
    protocol: 1, origin: ORIGIN, base_revision: 2,
    tombstones: [{ harness: "codex", native_id: "native-1", scope: "global", forgotten_at: 100 }],
  }), env, collector());
  assert.equal(tombstone.status, 200);
  const afterTombstone = await worker.fetch(request(`/api/v1/sync/generations?origin=${ORIGIN}`), env, collector());
  assert.deepEqual((await afterTombstone.json()).generations.map((row) => row.native_id), ["native-2"]);
  const tombstonedManifest = await worker.fetch(request(
    `/api/v1/sync/generations?origin=${ORIGIN}&manifest=${manifests[0].manifest_digest}`,
  ), env, collector());
  assert.deepEqual((await tombstonedManifest.json()).generations, []);

  const foreign = await worker.fetch(request(`/api/v1/sync/generations?origin=${ORIGIN}`), env, collector(OTHER_COLLECTOR));
  assert.equal(foreign.status, 403);
  const forbiddenCommit = await commit(env, manifests[0], 0, OTHER_COLLECTOR);
  assert.equal(forbiddenCommit.response.status, 403);
  const manifestRead = await worker.fetch(request(
    `/api/v1/sync/manifests/${manifests[1].manifest_object}`,
  ), env, collector(OTHER_COLLECTOR));
  assert.equal(manifestRead.status, 404);

  const readable = await worker.fetch(request(
    `/api/v1/sync/manifests/${manifests[1].manifest_object}`,
  ), env, collector());
  assert.equal(readable.status, 200);
  assert.match(readable.headers.get("x-plaintext-digest"), /^[0-9a-f]{64}$/);
  assert.ok((await readable.arrayBuffer()).byteLength > 0);
});

test("the same harness and native id can exist at two origins", async (t) => {
  const { env } = await runtime(t);
  const data = Buffer.from("shared native id");
  const first = manifestFor({
    origin: ORIGIN,
    native: "shared-native",
    chunks: [{ digest: (await storeObject(env, data, undefined, COLLECTOR)).plaintext, size: data.byteLength }],
  });
  first.manifest_object = await storeManifest(env, first, COLLECTOR);
  const second = manifestFor({
    origin: OTHER_ORIGIN,
    native: "shared-native",
    chunks: [{ digest: (await storeObject(env, data, undefined, OTHER_COLLECTOR)).plaintext, size: data.byteLength }],
  });
  second.manifest_object = await storeManifest(env, second, OTHER_COLLECTOR);

  const a = await commit(env, first, 0, COLLECTOR);
  const b = await commit(env, second, 0, OTHER_COLLECTOR);
  assert.equal(a.response.status, 200, JSON.stringify(a.body));
  assert.equal(b.response.status, 200, JSON.stringify(b.body));
  assert.notEqual(a.body.generation, b.body.generation);

  const firstPage = await worker.fetch(request(
    `/api/v1/sync/generations?origin=${ORIGIN}`,
  ), env, collector(COLLECTOR));
  const secondPage = await worker.fetch(request(
    `/api/v1/sync/generations?origin=${OTHER_ORIGIN}`,
  ), env, collector(OTHER_COLLECTOR));
  assert.deepEqual((await firstPage.json()).generations.map((row) => row.origin_host), [ORIGIN]);
  assert.deepEqual((await secondPage.json()).generations.map((row) => row.origin_host), [OTHER_ORIGIN]);
});

function countingWrites(db) {
  const counter = { rows: 0 };
  const wrapped = new Proxy(db, {
    get(target, property) {
      if (property !== "batch") return Reflect.get(target, property).bind(target);
      return async (statements) => {
        const results = await target.batch(statements);
        for (const result of results) counter.rows += result.meta.rows_written;
        return results;
      };
    },
  });
  return { counter, wrapped };
}

test("a session's next generation writes rows only for objects the session has not referenced", async (t) => {
  const { db, env } = await runtime(t);
  const chunks = [];
  for (let index = 0; index < 40; index += 1) {
    const data = Buffer.from(`repeated chunk ${index}`);
    chunks.push({ digest: (await storeObject(env, data)).plaintext, size: data.byteLength });
  }
  const first = manifestFor({ chunks });
  first.manifest_object = await storeManifest(env, first);
  assert.equal((await commit(env, first, 0)).response.status, 200);
  assert.equal((await count(db, "generation_object")).count, 40);

  const appended = Buffer.from("appended chunk");
  const second = manifestFor({ chunks: [...chunks, { digest: (await storeObject(env, appended)).plaintext, size: appended.byteLength }] });
  second.manifest_object = await storeManifest(env, second);
  const { counter, wrapped } = countingWrites(db);
  const committed = await commit({ ...env, DB: wrapped }, second, 1);
  assert.equal(committed.response.status, 200, JSON.stringify(committed.body));
  assert.equal((await count(db, "generation_object")).count, 41);
  assert.ok(counter.rows < 15, `the second generation wrote ${counter.rows} rows for one new object`);
});
