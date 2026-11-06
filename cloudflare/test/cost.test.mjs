import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { seedDatabase } from "../bench/seed.mjs";
import { createStack } from "../local/stack.mjs";
import { loadWorker, request } from "./harness.mjs";

const worker = await loadWorker();
const COLLECTOR = "collector-client.access";
const ORIGIN = "origin-a";

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

function collector() {
  return { access: { getIdentity: async () => ({ service_token_id: COLLECTOR }) } };
}

function counting(db) {
  const writes = [];
  const record = (meta) => writes.push(meta?.rows_written ?? 0);
  const statement = (target) => new Proxy(target, {
    get(object, property) {
      const value = Reflect.get(object, property);
      if (typeof value !== "function") return value;
      if (property === "bind") return (...args) => statement(value.apply(object, args));
      if (property === "run" || property === "all") {
        return async (...args) => {
          const resolved = await value.apply(object, args);
          record(resolved.meta);
          return resolved;
        };
      }
      return (...args) => value.apply(object, args);
    },
  });
  const wrapped = new Proxy(db, {
    get(target, property) {
      const value = Reflect.get(target, property);
      if (typeof value !== "function") return value;
      if (property === "prepare") return (query) => statement(value.call(target, query));
      if (property === "batch") {
        return async (statements) => {
          const results = await target.batch(statements);
          for (const result of results) record(result.meta);
          return results;
        };
      }
      return (...args) => value.apply(target, args);
    },
  });
  return { db: wrapped, writes, rows: () => writes.reduce((total, value) => total + value, 0) };
}

async function runtime(t) {
  const stack = await createStack({
    readers: "reader@example.test",
    teamDomain: "team.example.test",
    audience: "test-audience",
  });
  t.after(() => stack.dispose());
  const db = await stack.d1();
  await seedDatabase(db);
  await db.prepare(
    "INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)",
  ).bind(COLLECTOR, ORIGIN, "cost", 0).run();
  const counter = counting(db);
  const env = { ...await stack.env(), DB: counter.db };
  return { db, env, counter };
}

async function storeObject(env, data) {
  const ciphertext = Buffer.concat([Buffer.from("ciphertext:"), Buffer.from(data)]);
  const response = await worker.fetch(request(`/api/v1/sync/objects/${digest(ciphertext)}`, {
    method: "PUT",
    headers: {
      "X-Plaintext-Digest": digest(data),
      "X-Plaintext-Size": String(Buffer.byteLength(data)),
      "X-Encoding": "identity",
    },
    body: ciphertext,
  }), env, collector());
  assert.equal(response.status, 200, await response.clone().text());
  const plaintext = digest(data);
  return { digest: plaintext, plaintext, size: Buffer.byteLength(data), ciphertext: digest(ciphertext) };
}

async function storeManifest(env, manifest) {
  return (await storeObject(env, Buffer.from(JSON.stringify({ manifest: manifest.manifest_digest })))).ciphertext;
}

function manifestFor({ chunks, native = "native-1", version = "v1" }) {
  const sourceDigest = createHash("sha256").update(chunks.map((chunk) => chunk.digest).join("")).digest("hex");
  let offset = 0;
  const tiled = chunks.map((chunk) => {
    const entry = { offset, size: chunk.size, digest: chunk.digest };
    offset += chunk.size;
    return entry;
  });
  return {
    origin_host: ORIGIN,
    harness: "codex",
    native_id: native,
    source_path: `/synthetic/${native}-${version}.jsonl`,
    source_digest: sourceDigest,
    source_size: chunks.reduce((total, chunk) => total + chunk.size, 0),
    captured_at: 1762000000.5 + (version === "v1" ? 0 : 1),
    chunks: tiled,
    artifacts: [],
    session_metadata: { cwd: "/synthetic", native, version },
    manifest_digest: digest(`manifest:${ORIGIN}:${native}:${sourceDigest}:${version}`),
    manifest_object: undefined,
  };
}

async function commit(env, manifest, baseRevision) {
  const response = await worker.fetch(request("/api/v1/sync/generations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ protocol: 1, origin: ORIGIN, base_revision: baseRevision, manifest }),
  }), env, collector());
  return { response, body: await response.json() };
}

async function generation(env, chunks, native, version, baseRevision) {
  const manifest = manifestFor({ chunks, native, version });
  manifest.manifest_object = await storeManifest(env, manifest);
  return await commit(env, manifest, baseRevision);
}

const PAGE_OBJECTS = 10_000;

function snapshotFor(chunks, objects, seed) {
  const id = digest(seed);
  const pinned = [...objects].sort((left, right) => (left.plaintext < right.plaintext ? -1 : 1));
  return {
    id,
    created_at: 1762000000 + objects.length,
    schema_version: 10,
    db_digest: id,
    db_size: chunks.reduce((total, chunk) => total + chunk.size, 0),
    chunks: chunks.map((chunk) => ({
      plaintext_digest: chunk.plaintext,
      size: chunk.size,
      ciphertext_digest: chunk.ciphertext,
    })),
    objects: pinned.length,
    object_bytes: pinned.reduce((total, object) => total + object.size, 0),
    objects_digest: digest(pinned.map((object) => `${object.plaintext} ${object.size}\n`).join("")),
    pinned,
  };
}

async function commitSnapshot(env, snapshot, baseRevision) {
  const { pinned, ...wire } = snapshot;
  for (let start = 0; start < pinned.length; start += PAGE_OBJECTS) {
    const response = await worker.fetch(request(`/api/v1/sync/snapshots/${wire.id}/objects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        protocol: 1,
        origin: ORIGIN,
        page: start / PAGE_OBJECTS,
        objects: pinned.slice(start, start + PAGE_OBJECTS)
          .map((object) => ({ plaintext_digest: object.plaintext, size: object.size })),
      }),
    }), env, collector());
    assert.equal(response.status, 200, await response.clone().text());
  }
  const response = await worker.fetch(request("/api/v1/sync/snapshots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ protocol: 1, origin: ORIGIN, base_revision: baseRevision, snapshot: wire }),
  }), env, collector());
  return { response, body: await response.json() };
}

test("a duplicate generation commit writes zero rows", async (t) => {
  const { env, counter } = await runtime(t);
  const chunks = [await storeObject(env, Buffer.from("duplicate cost chunk"))];
  const first = await generation(env, chunks, "duplicate", "v1", 0);
  assert.equal(first.response.status, 200, JSON.stringify(first.body));
  counter.writes.length = 0;
  await storeObject(env, Buffer.from("upload budget"));
  assert.equal(counter.rows(), 5);
  counter.writes.length = 0;
  const hello = await worker.fetch(request("/api/v1/sync/hello", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ protocol: 1, origin: ORIGIN }),
  }), env, collector());
  assert.equal(hello.status, 200);
  const missing = await worker.fetch(request("/api/v1/sync/objects/missing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      protocol: 1, origin: ORIGIN,
      objects: [{ plaintext_digest: chunks[0].digest, size: chunks[0].size }],
    }),
  }), env, collector());
  assert.equal(missing.status, 200);
  const duplicate = await generation(env, chunks, "duplicate", "v1", 0);
  assert.equal(duplicate.response.status, 200, JSON.stringify(duplicate.body));
  assert.equal(duplicate.body.duplicate, true);
  assert.equal(counter.rows(), 0);
});

test("a generation commit writes generation_object rows only for new objects", async (t) => {
  const { db, env, counter } = await runtime(t);
  const chunks = [];
  for (let index = 0; index < 40; index += 1) {
    chunks.push(await storeObject(env, Buffer.from(`repeated cost chunk ${index}`)));
  }
  const first = await generation(env, chunks, "repeated", "v1", 0);
  assert.equal(first.response.status, 200, JSON.stringify(first.body));
  const appended = await storeObject(env, Buffer.from("new cost chunk"));
  const second = await generation(env, [...chunks, appended], "repeated", "v2", 1);
  assert.equal(second.response.status, 200, JSON.stringify(second.body));
  const rows = await db.prepare("SELECT count(*) AS count FROM generation_object").first();
  assert.equal(rows.count, 41);
  counter.writes.length = 0;
  const third = await generation(env, [...chunks, appended], "repeated", "v3", 2);
  assert.equal(third.response.status, 200, JSON.stringify(third.body));
  const unchanged = await db.prepare("SELECT count(*) AS count FROM generation_object").first();
  assert.equal(unchanged.count, 41);
  assert.equal(counter.rows(), 11);
});

test("a second generation with 10x retained objects stays under 1.5x", async (t) => {
  const { env, counter } = await runtime(t);
  const measure = async (count, baseRevision) => {
    const chunks = [];
    for (let index = 0; index < count; index += 1) {
      chunks.push(await storeObject(env, Buffer.from(`retained cost chunk ${count} ${index}`)));
    }
    const first = await generation(env, chunks, `retained-${count}`, "v1", baseRevision);
    assert.equal(first.response.status, 200, JSON.stringify(first.body));
    counter.writes.length = 0;
    const second = await generation(env, chunks, `retained-${count}`, "v2", baseRevision + 1);
    assert.equal(second.response.status, 200, JSON.stringify(second.body));
    return { rows: counter.rows(), revision: baseRevision + 2 };
  };
  const small = await measure(10, 0);
  const large = await measure(100, small.revision);
  assert.ok(large.rows < small.rows * 1.5, `10x retained wrote ${large.rows} versus ${small.rows}`);
  assert.equal(large.rows, 11);
});

test("a snapshot commit writes rows in proportion to chunks and pending objects", async (t) => {
  const { db, env, counter } = await runtime(t);
  const chunk = await storeObject(env, Buffer.from("snapshot cost chunk"));
  const pinned = [];
  for (let index = 0; index < 200; index += 1) {
    pinned.push({
      plaintext: digest(`pinned cost object ${index}`),
      size: 7,
      ciphertext: digest(`pinned cost ciphertext ${index}`),
    });
  }
  const insert = db.prepare(
    "INSERT INTO remote_object(origin_host, plaintext_digest, plaintext_size, encoding, ciphertext_digest, "
    + "ciphertext_size, created_at, gc_pending_at) VALUES(?, ?, ?, 'identity', ?, ?, 0, ?)",
  );
  await db.batch(pinned.map((object) => insert.bind(
    ORIGIN, object.plaintext, object.size, object.ciphertext, object.size, null)));
  counter.writes.length = 0;
  const committed = await commitSnapshot(env, snapshotFor([chunk], pinned, "cost snapshot"), 0);
  assert.equal(committed.response.status, 200, JSON.stringify(committed.body));
  assert.ok(counter.rows() < 20, `snapshot commit wrote ${counter.rows()} rows`);
  assert.equal(counter.rows(), 11);
});

test("the schema guard pins every table's index set", async (t) => {
  const { db } = await runtime(t);
  const rows = await db.prepare(
    "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%' "
    + "ORDER BY tbl_name, name",
  ).all();
  const indexed = {};
  for (const row of rows.results) {
    indexed[row.tbl_name] = [...(indexed[row.tbl_name] ?? []), row.name];
  }
  assert.deepEqual(indexed, {
    generation: ["generation_manifest_object", "generation_origin_committed", "generation_origin_revision"],
    generation_object: ["generation_object_plaintext"],
    learning_experience: [
      "learning_experience_kind", "learning_experience_observed", "learning_experience_origin",
      "learning_experience_project", "learning_experience_state_observed",
    ],
    learning_memory: [
      "learning_memory_created", "learning_memory_origin", "learning_memory_project",
      "learning_memory_state_created",
    ],
    learning_source: ["learning_source_record", "learning_source_session"],
    remote_object: ["remote_object_ciphertext", "remote_object_gc"],
    snapshot: ["snapshot_origin_created", "snapshot_origin_revision"],
    snapshot_chunk: ["snapshot_chunk_ciphertext", "snapshot_chunk_plaintext"],
    tombstone: ["tombstone_origin_revision", "tombstone_origin_session"],
  });
});
