import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { Miniflare } from "miniflare";

const MIGRATION = new URL("../migrations/0001_init.sql", import.meta.url);
const TABLES = [
  "collector", "generation", "generation_object", "origin_sync", "remote_object",
  "snapshot", "snapshot_chunk", "snapshot_object", "store_meta", "tombstone",
];
const INDEXES = ["generation_origin_committed", "remote_object_ciphertext", "tombstone_origin_session"];

function statements(sql) {
  return sql.split(";").map((statement) => statement.trim()).filter(Boolean);
}

async function apply(db, sql) {
  for (const statement of statements(sql)) {
    await db.prepare(statement).run();
  }
}

async function database(t) {
  const miniflare = new Miniflare({
    modules: true,
    script: "export default { fetch() { return new Response('ok'); } };",
    compatibilityDate: "2026-08-06",
    d1Databases: ["DB"],
  });
  t.after(() => miniflare.dispose());
  const db = await miniflare.getD1Database("DB");
  await apply(db, readFileSync(MIGRATION, "utf8"));
  return db;
}

test("the initial migration is idempotent and creates every table and index", async (t) => {
  const db = await database(t);
  await apply(db, readFileSync(MIGRATION, "utf8"));
  const tables = await db.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_cf\\_%' ESCAPE '\\' ORDER BY name",
  ).all();
  assert.deepEqual(tables.results.map((row) => row.name), TABLES);
  const indexes = await db.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%' ORDER BY name",
  ).all();
  assert.deepEqual(indexes.results.map((row) => row.name), INDEXES);
  const store = await db.prepare("SELECT id FROM store_meta").first();
  assert.equal(store.id, "usr-memories");
  assert.equal((await db.prepare("SELECT count(*) AS count FROM store_meta").first()).count, 1);
});

test("constraints reject duplicate ciphertext digests and negative sizes", async (t) => {
  const db = await database(t);
  const object = db.prepare(
    "INSERT INTO remote_object(plaintext_digest, plaintext_size, encoding, ciphertext_digest, ciphertext_size, origin_host, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
  );
  await object.bind("a".repeat(64), 10, "zlib", "b".repeat(64), 8, "origin-a", 1).run();
  await assert.rejects(object.bind("c".repeat(64), 10, "zlib", "b".repeat(64), 8, "origin-a", 1).run());
  await assert.rejects(object.bind("d".repeat(64), -1, "zlib", "e".repeat(64), 8, "origin-a", 1).run());
  await assert.rejects(object.bind("f".repeat(64), 10, "zlib", "g".repeat(64), -1, "origin-a", 1).run());
  const generation = db.prepare(
    "INSERT INTO generation(origin_host, harness, native_id, manifest_digest, source_digest, source_size, captured_at, committed_at, revision, manifest_object) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  );
  await assert.rejects(generation.bind("origin-a", "codex", "s1", "h".repeat(64), "i".repeat(64), -1, 1, 1, 1, "b".repeat(64)).run());
  const snapshot = db.prepare(
    "INSERT INTO snapshot(id, origin_host, created_at, schema_version, db_digest, db_size, objects, object_bytes, verified_at, revision) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  );
  await assert.rejects(snapshot.bind("snap", "origin-a", 1, 9, "j".repeat(64), -1, 0, 0, null, 1).run());
});
