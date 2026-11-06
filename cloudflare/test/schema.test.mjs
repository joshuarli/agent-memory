import assert from "node:assert/strict";
import test from "node:test";

import { seedDatabase } from "../bench/seed.mjs";
import { createStack } from "../local/stack.mjs";

const TABLES = [
  "collector", "generation", "generation_object", "learning_experience", "learning_fence", "learning_link",
  "learning_memory", "learning_reconcile", "learning_search", "learning_source", "learning_state", "memory_fts",
  "memory_fts_config", "memory_fts_data", "memory_fts_docsize", "memory_fts_idx", "origin_sync", "remote_object",
  "snapshot", "snapshot_chunk", "store_meta", "tombstone",
];
const INDEXES = [
  "generation_manifest_object", "generation_object_plaintext", "generation_origin_committed",
  "generation_origin_revision", "learning_experience_kind", "learning_experience_observed",
  "learning_experience_origin", "learning_experience_project", "learning_experience_state_observed",
  "learning_memory_created", "learning_memory_origin", "learning_memory_project", "learning_memory_state_created",
  "learning_source_record", "learning_source_session", "remote_object_ciphertext",
  "snapshot_chunk_ciphertext", "snapshot_chunk_plaintext", "snapshot_origin_created", "snapshot_origin_revision",
  "tombstone_origin_revision", "tombstone_origin_session",
];
const TRIGGERS = [
  "generation_revision_fence", "generation_tombstone_guard", "learning_fence_insert", "learning_fence_update",
  "learning_search_ad", "learning_search_ai", "learning_search_au", "learning_state_revision_fence_insert",
  "learning_state_revision_fence_update", "snapshot_revision_fence", "tombstone_revision_fence_insert",
  "tombstone_revision_fence_update",
];

async function database(t) {
  const stack = await createStack();
  t.after(() => stack.dispose());
  const db = await stack.d1();
  await seedDatabase(db);
  return db;
}

test("the full migration chain creates every table, index, trigger, and store row", async (t) => {
  const db = await database(t);
  const tables = await db.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
    + "AND name NOT LIKE '\\_cf\\_%' ESCAPE '\\' ORDER BY name",
  ).all();
  assert.deepEqual(tables.results.map((row) => row.name), TABLES);
  const indexes = await db.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%' ORDER BY name",
  ).all();
  assert.deepEqual(indexes.results.map((row) => row.name), INDEXES);
  const triggers = await db.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name",
  ).all();
  assert.deepEqual(triggers.results.map((row) => row.name), TRIGGERS);
  assert.equal((await db.prepare("SELECT id FROM store_meta").first()).id, "usr-memories");
  assert.equal((await db.prepare("SELECT count(*) AS count FROM store_meta").first()).count, 1);
  const objectColumns = (await db.prepare("PRAGMA table_info(remote_object)").all()).results.map((row) => row.name);
  assert.equal(objectColumns.includes("last_referenced_at"), false);
  assert.equal(objectColumns.includes("gc_pending_at"), true);
  const snapshotColumns = (await db.prepare("PRAGMA table_info(snapshot)").all()).results.map((row) => row.name);
  assert.equal(snapshotColumns.includes("objects_digest"), true);
  const legacy = await db.prepare(
    "SELECT count(*) AS count FROM sqlite_master WHERE type = 'table' AND name = 'snapshot_object'",
  ).first();
  assert.equal(legacy.count, 0);
});

test("constraints reject duplicate ciphertext digests and negative sizes", async (t) => {
  const db = await database(t);
  const object = db.prepare(
    "INSERT INTO remote_object(origin_host, plaintext_digest, plaintext_size, encoding, ciphertext_digest, "
    + "ciphertext_size, created_at, gc_pending_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
  );
  await object.bind("origin-a", "a".repeat(64), 10, "identity", "b".repeat(64), 8, 1, null).run();
  await assert.rejects(object.bind("origin-a", "c".repeat(64), 10, "identity", "b".repeat(64), 8, 1, null).run());
  await object.bind("origin-b", "d".repeat(64), 10, "identity", "b".repeat(64), 8, 1, null).run();
  await assert.rejects(object.bind("origin-a", "a".repeat(64), 10, "identity", "e".repeat(64), 8, 1, null).run());
  await assert.rejects(object.bind("origin-a", "f".repeat(64), -1, "identity", "g".repeat(64), 8, 1, null).run());
  await assert.rejects(object.bind("origin-a", "h".repeat(64), 10, "identity", "i".repeat(64), -1, 1, null).run());
  const generation = db.prepare(
    "INSERT INTO generation(origin_host, harness, native_id, manifest_digest, source_digest, source_size, "
    + "captured_at, committed_at, revision, manifest_object) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  );
  await generation.bind("origin-a", "codex", "s1", "j".repeat(64), "k".repeat(64), 1, 1, 1, 1, "b".repeat(64)).run();
  await assert.rejects(
    generation.bind("origin-a", "codex", "s2", "j".repeat(64), "l".repeat(64), 1, 1, 1, 1, "b".repeat(64)).run(),
  );
  await assert.rejects(
    generation.bind("origin-a", "codex", "s3", "j".repeat(64), "n".repeat(64), -1, 1, 1, 1, "b".repeat(64)).run(),
  );
  const snapshot = db.prepare(
    "INSERT INTO snapshot(id, origin_host, created_at, schema_version, db_digest, db_size, objects, object_bytes, "
    + "objects_digest, verified_at, revision) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  );
  await snapshot.bind("snap", "origin-a", 1, 10, "o".repeat(64), 1, 0, 0, "p".repeat(64), null, 1).run();
  await assert.rejects(
    snapshot.bind("snap-2", "origin-a", 1, 10, "q".repeat(64), -1, 0, 0, "r".repeat(64), null, 1).run(),
  );
  await assert.rejects(
    snapshot.bind("snap-3", "origin-a", 1, 10, "s".repeat(64), 1, 0, 0, null, null, 1).run(),
  );
});
