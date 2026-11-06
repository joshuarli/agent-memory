import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { seedDatabase, statements } from "../bench/seed.mjs";
import { loadReleaseConfig } from "../local/config.mjs";
import { createStack } from "../local/stack.mjs";

const workerRoot = fileURLToPath(new URL("..", import.meta.url));
const scratchRoot = join(fileURLToPath(new URL("../../", import.meta.url)), ".work/tmp");
const migrations = new URL("../migrations/", import.meta.url);
const cfBinary = process.env.CF_BIN
  ?? (existsSync(join(workerRoot, "node_modules/.bin/cf")) ? join(workerRoot, "node_modules/.bin/cf") : "cf");
mkdirSync(scratchRoot, { recursive: true });

function migrationFiles() {
  return readdirSync(migrations).filter((name) => name.endsWith(".sql")).sort();
}

async function applyFiles(db, files) {
  for (const file of files) {
    const sql = readFileSync(new URL(file, migrations), "utf8");
    for (const statement of statements(sql)) await db.prepare(statement).run();
  }
}

function databaseFile(root) {
  const found = [];
  const walk = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.name.endsWith(".sqlite") && entry.name !== "metadata.sqlite") found.push(path);
    }
  };
  walk(root);
  assert.equal(found.length, 1, `expected one D1 database under ${root}, found ${found.length}`);
  return found[0];
}

function schemaRows(path) {
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    return db.prepare(
      "SELECT type, name, tbl_name, sql FROM sqlite_master "
      + "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' AND name != 'd1_migrations' "
      + "ORDER BY type, name",
    ).all().map((row) => ({
      type: row.type,
      name: row.name,
      tbl_name: row.tbl_name,
      sql: row.sql.replace(/\s+/g, " ").trim(),
    }));
  } finally {
    db.close();
  }
}

function runCf(args) {
  return execFileSync(cfBinary, args, {
    cwd: workerRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function scratch() {
  return mkdtempSync(join(scratchRoot, "migrations-"));
}

async function pragma(db, table) {
  return (await db.prepare(`PRAGMA table_info(${table})`).all()).results;
}

async function count(db, table) {
  return (await db.prepare(`SELECT count(*) AS count FROM ${table}`).first()).count;
}

test("the migration runner and the local Cloudflare runner produce the same schema", async (t) => {
  const root = scratch();
  const stack = await createStack({ stateDir: join(root, "stack"), keepState: true });
  t.after(async () => {
    try {
      await stack.dispose();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  await seedDatabase(await stack.d1());
  const database = loadReleaseConfig().env.DB.id;
  const cfDirectory = join(root, "cf");
  const first = JSON.parse(runCf([
    "d1", "migrations", "apply", database, "--local", "--persist-to", cfDirectory,
  ]));
  assert.deepEqual(first.map((entry) => entry.name), migrationFiles());
  const second = JSON.parse(runCf([
    "d1", "migrations", "apply", database, "--local", "--persist-to", cfDirectory,
  ]));
  assert.deepEqual(second, []);
  await stack.dispose();
  assert.deepEqual(schemaRows(databaseFile(cfDirectory)), schemaRows(databaseFile(join(root, "stack"))));
});

test("an upgrade from 0007 keeps unrelated rows and removes only snapshot storage and last_referenced_at", async (t) => {
  const root = scratch();
  const stateDirectory = join(root, "state");
  const files = migrationFiles();
  const split = files.findIndex((name) => name.startsWith("0008"));
  assert.ok(split > 0);
  let active = await createStack({ stateDir: stateDirectory, keepState: true });
  t.after(async () => {
    try {
      await active.dispose();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  const db = await active.d1();
  await applyFiles(db, files.slice(0, split));
  const digest = (value) => value.repeat(64);

  await db.prepare("INSERT INTO origin_sync(origin_host, revision, last_change_at) VALUES(?, ?, ?)")
    .bind("origin-a", 0, 10).run();
  await db.prepare("INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)")
    .bind("token.access", "origin-a", "local", 1).run();
  const object = db.prepare(
    "INSERT INTO remote_object(origin_host, plaintext_digest, plaintext_size, encoding, ciphertext_digest, "
    + "ciphertext_size, created_at, last_referenced_at, gc_pending_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
  );
  await object.bind("origin-a", digest("a"), 10, "identity", digest("b"), 20, 1, 2, null).run();
  await object.bind("origin-a", digest("c"), 11, "identity", digest("d"), 21, 1, 2, null).run();
  await db.prepare(
    "INSERT INTO generation(id, origin_host, harness, native_id, manifest_digest, source_digest, source_size, "
    + "captured_at, committed_at, revision, manifest_object) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(1, "origin-a", "codex", "native-1", digest("e"), digest("f"), 10, 1, 1, 1, digest("b")).run();
  await db.prepare("INSERT INTO generation_object(generation, ordinal, plaintext_digest, size) VALUES(?, ?, ?, ?)")
    .bind(1, 0, digest("a"), 10).run();
  const snapshot = db.prepare(
    "INSERT INTO snapshot(id, origin_host, created_at, schema_version, db_digest, db_size, objects, object_bytes, "
    + "verified_at, revision) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  );
  await snapshot.bind("snap-1", "origin-a", 1, 10, digest("1"), 10, 2, 21, null, 1).run();
  await snapshot.bind("snap-2", "origin-a", 2, 10, digest("2"), 10, 2, 21, null, 1).run();
  const chunk = db.prepare(
    "INSERT INTO snapshot_chunk(snapshot, ordinal, plaintext_digest, size, ciphertext_digest) VALUES(?, ?, ?, ?, ?)",
  );
  await chunk.bind("snap-1", 0, digest("a"), 10, digest("b")).run();
  await chunk.bind("snap-2", 0, digest("c"), 11, digest("d")).run();
  const pinned = db.prepare("INSERT INTO snapshot_object(snapshot, plaintext_digest, size) VALUES(?, ?, ?)");
  await pinned.bind("snap-1", digest("a"), 10).run();
  await pinned.bind("snap-2", digest("c"), 11).run();
  await db.prepare(
    "INSERT INTO tombstone(origin_host, harness, native_id, scope, forgotten_at, revision) VALUES(?, ?, ?, ?, ?, ?)",
  ).bind("origin-a", "codex", "native-gone", "global", 3, 1).run();
  await db.prepare(
    "INSERT INTO learning_state(origin_host, revision, epoch, digest, records, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
  ).bind("origin-a", 1, 1, null, 1, 1).run();
  await db.prepare("INSERT INTO learning_fence(singleton, changed) VALUES(1, 1)").run();
  await db.prepare(
    "INSERT INTO learning_experience(origin_host, epoch, remote_id, record_digest, project_key, project_label, kind, "
    + "claim, applies_when, identifiers, fingerprint, basis, detector, revision, worktree, observed_at, captured_at, "
    + "validity, tags, state, state_reason, state_at, sync_revision, pending) "
    + "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(
    "origin-a", 1, 1, digest("9"), "0123456789abcdef", "usr", "method", "claim", "applies", "id", "fp",
    "deterministic", "synthetic", null, null, 1, 1, "[]", "", "retained", null, null, 1, 0,
  ).run();
  await db.prepare(
    "INSERT INTO learning_memory(origin_host, epoch, remote_id, record_digest, project_key, project_label, action, "
    + "applicability, revision, validity, state, created_at, sync_revision, pending) "
    + "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(
    "origin-a", 1, 1, digest("8"), "0123456789abcdef", "usr", "action", "applicability", null, "[]", "approved", 1, 1, 0,
  ).run();
  await db.prepare(
    "INSERT INTO learning_source(origin_host, epoch, record, remote_id, session, position, digest, role) "
    + "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind("origin-a", 1, "experience", 1, "codex:native-1@origin-a", 0, digest("7"), "trigger").run();
  await db.prepare("INSERT INTO learning_link(origin_host, epoch, newer, older, relation) VALUES(?, ?, ?, ?, ?)")
    .bind("origin-a", 1, 1, 2, "supersedes").run();
  await db.prepare("INSERT INTO learning_search(origin_host, epoch, record, remote_id, text) VALUES(?, ?, ?, ?, ?)")
    .bind("origin-a", 1, "experience", 1, "claim applies").run();

  const before = await pragma(db, "remote_object");
  await active.dispose();
  assert.equal(existsSync(stateDirectory), true);

  active = await createStack({ stateDir: stateDirectory, keepState: true });
  const upgraded = await active.d1();
  await applyFiles(upgraded, files.slice(split));

  assert.equal(await count(upgraded, "snapshot"), 0);
  assert.equal(await count(upgraded, "snapshot_chunk"), 0);
  const legacy = await upgraded.prepare(
    "SELECT count(*) AS count FROM sqlite_master WHERE type = 'table' AND name = 'snapshot_object'",
  ).first();
  assert.equal(legacy.count, 0);
  assert.equal(await count(upgraded, "remote_object"), 2);
  assert.equal(await count(upgraded, "generation"), 1);
  assert.equal(await count(upgraded, "generation_object"), 1);
  assert.equal(await count(upgraded, "tombstone"), 1);
  assert.equal(await count(upgraded, "collector"), 1);
  assert.equal(await count(upgraded, "origin_sync"), 1);
  assert.equal(await count(upgraded, "learning_experience"), 1);
  assert.equal(await count(upgraded, "learning_memory"), 1);
  assert.equal(await count(upgraded, "learning_source"), 1);
  assert.equal(await count(upgraded, "learning_link"), 1);
  assert.equal(await count(upgraded, "learning_search"), 1);
  const objectRow = await upgraded.prepare(
    "SELECT origin_host, plaintext_digest, plaintext_size, encoding, ciphertext_digest, ciphertext_size, "
    + "created_at, gc_pending_at FROM remote_object WHERE plaintext_digest = ?",
  ).bind(digest("a")).first();
  assert.deepEqual({ ...objectRow }, {
    origin_host: "origin-a", plaintext_digest: digest("a"), plaintext_size: 10, encoding: "identity",
    ciphertext_digest: digest("b"), ciphertext_size: 20, created_at: 1, gc_pending_at: null,
  });
  const after = await pragma(upgraded, "remote_object");
  const normalize = (columns) => columns.map(({ cid: _cid, ...rest }) => rest);
  assert.deepEqual(normalize(after), normalize(before).filter((column) => column.name !== "last_referenced_at"));
  const snapshotColumns = await pragma(upgraded, "snapshot");
  assert.ok(snapshotColumns.some((column) => column.name === "objects_digest"));
});
