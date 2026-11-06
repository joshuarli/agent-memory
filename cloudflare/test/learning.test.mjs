import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";

import { Miniflare } from "miniflare";

import { seedDatabase } from "../bench/seed.mjs";
import { loadWorker, request } from "./harness.mjs";

const worker = await loadWorker();
const FIXTURE = JSON.parse(readFileSync(new URL("../../tests/fixtures/learning_records.json", import.meta.url), "utf8"));
const COLLECTOR = "collector-client.access";
const OTHER_COLLECTOR = "other-client.access";
const ORIGIN = "origin-a";
const OTHER_ORIGIN = "origin-b";

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

function changeFor(name) {
  const entry = FIXTURE.cases.find((item) => item.name === name);
  assert.notEqual(entry, undefined, `fixture case ${name}`);
  return JSON.parse(JSON.stringify(entry.change));
}

function shifted(change, origin) {
  const copy = JSON.parse(JSON.stringify(change));
  if (Array.isArray(copy.sources)) {
    copy.sources = copy.sources.map((source) => ({
      ...source,
      session: source.session.slice(0, source.session.lastIndexOf("@") + 1) + origin,
    }));
  }
  return copy;
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

function postJson(env, path, value, client = COLLECTOR) {
  return worker.fetch(request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  }), env, collector(client));
}

function delta(origin, baseRevision, name, changes) {
  return {
    protocol: 1,
    origin,
    base_revision: baseRevision,
    digest: digest(name + ":" + baseRevision),
    changes,
  };
}

async function stateOf(db, origin) {
  return await db.prepare(
    "SELECT revision, epoch, digest, records FROM learning_state WHERE origin_host = ?",
  ).bind(origin).first();
}

async function projection(db, origin) {
  const experiences = await db.prepare(
    `SELECT remote_id, project_key, project_label, kind, claim, applies_when, identifiers, fingerprint,
            basis, detector, revision, worktree, observed_at, captured_at, validity, tags, state,
            state_reason, state_at
     FROM learning_experience WHERE origin_host = ? ORDER BY remote_id`,
  ).bind(origin).all();
  const memories = await db.prepare(
    `SELECT remote_id, project_key, project_label, action, applicability, revision, validity,
            state, created_at
     FROM learning_memory WHERE origin_host = ? ORDER BY remote_id`,
  ).bind(origin).all();
  const sources = await db.prepare(
    `SELECT record, remote_id, session, position, digest, role
     FROM learning_source WHERE origin_host = ? ORDER BY record, remote_id, session, position, role`,
  ).bind(origin).all();
  const links = await db.prepare(
    "SELECT newer, older, relation FROM learning_link WHERE origin_host = ? ORDER BY newer, older, relation",
  ).bind(origin).all();
  const search = await db.prepare(
    "SELECT record, remote_id, text FROM learning_search WHERE origin_host = ? ORDER BY record, remote_id",
  ).bind(origin).all();
  return {
    experiences: experiences.results,
    memories: memories.results,
    sources: sources.results.map((row) => ({
      ...row,
      session: row.session.slice(0, row.session.lastIndexOf("@")),
    })),
    links: links.results,
    search: search.results,
  };
}

async function ftsCount(db, term) {
  const row = await db.prepare(
    "SELECT count(*) AS count FROM memory_fts WHERE memory_fts MATCH ?",
  ).bind(`"${term}"`).first();
  return row.count;
}

test("the shared fixture accepts and rejects the same records in the worker validator", async (t) => {
  const { db, env } = await runtime(t);
  let revision = 0;
  for (const entry of FIXTURE.cases) {
    const response = await postJson(env, "/api/v1/sync/learning/deltas",
      delta(FIXTURE.origin, revision, entry.name, [entry.change]));
    const body = await response.json();
    assert.equal(response.status, entry.valid ? 200 : 400, `${entry.name}: ${JSON.stringify(body)}`);
    if (entry.valid) revision = body.revision;
  }
  const state = await stateOf(db, ORIGIN);
  assert.equal(state.revision, revision);
  assert.equal(state.records, 2);
});

test("a delta applies records, sources, links, and FTS text idempotently", async (t) => {
  const { db, env } = await runtime(t);
  const first = delta(ORIGIN, 0, "first", [changeFor("experience")]);
  const response = await postJson(env, "/api/v1/sync/learning/deltas", first);
  const body = await response.json();
  assert.equal(response.status, 200, JSON.stringify(body));
  assert.deepEqual(
    { revision: body.revision, epoch: body.epoch, records: body.records, applied: body.applied, replayed: body.replayed },
    { revision: 1, epoch: 1, records: 1, applied: 1, replayed: false },
  );
  const row = await db.prepare(
    "SELECT remote_id, project_key, project_label, state, pending, sync_revision FROM learning_experience WHERE origin_host = ?",
  ).bind(ORIGIN).first();
  assert.deepEqual({ ...row }, {
    remote_id: 7, project_key: "0123456789abcdef", project_label: "usr", state: "retained",
    pending: 0, sync_revision: 1,
  });
  assert.equal((await db.prepare(
    "SELECT count(*) AS count FROM learning_source WHERE origin_host = ? AND remote_id = 7",
  ).bind(ORIGIN).first()).count, 2);
  assert.equal((await db.prepare(
    "SELECT count(*) AS count FROM learning_link WHERE origin_host = ? AND newer = 7",
  ).bind(ORIGIN).first()).count, 1);
  assert.ok(await ftsCount(db, "repository") > 0);

  const again = await postJson(env, "/api/v1/sync/learning/deltas", first);
  const replay = await again.json();
  assert.equal(again.status, 200);
  assert.equal(replay.revision, 1);
  assert.equal(replay.replayed, true);
  assert.equal(replay.applied, 0);

  const stale = delta(ORIGIN, 0, "stale", [changeFor("memory")]);
  const refused = await postJson(env, "/api/v1/sync/learning/deltas", stale);
  const refusedBody = await refused.json();
  assert.equal(refused.status, 409);
  assert.equal(refusedBody.error.code, "stale_revision");
  assert.equal(refusedBody.state.revision, 1);
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_experience WHERE origin_host = ?",
  ).bind(ORIGIN).first().then((value) => value.count), 1);
  assert.equal((await db.prepare(
    "SELECT count(*) AS count FROM learning_memory WHERE origin_host = ?",
  ).bind(ORIGIN).first()).count, 0);
});

test("an incremental delta stream and one full delta produce the same projection", async (t) => {
  const { db, env } = await runtime(t);
  const experience = changeFor("experience");
  const memory = changeFor("memory");
  const updated = { ...experience, claim: "Updated claim for the same experience.", tags: "tests updated" };
  updated.digest = digest("updated:" + updated.claim);
  const response = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 0, "stream-1", [experience]));
  assert.equal(response.status, 200);
  const second = await postJson(env, "/api/v1/sync/learning/deltas", delta(ORIGIN, 1, "stream-2", [updated, memory]));
  assert.equal(second.status, 200);
  const third = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 2, "stream-3", [{ record: "memory", id: 9, deleted: true }]));
  assert.equal(third.status, 200);

  const full = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(OTHER_ORIGIN, 0, "full", [shifted(updated, OTHER_ORIGIN)]), OTHER_COLLECTOR);
  assert.equal(full.status, 200);
  assert.deepEqual(await projection(db, ORIGIN), await projection(db, OTHER_ORIGIN));
  assert.equal((await stateOf(db, ORIGIN)).records, 1);
  assert.equal((await stateOf(db, OTHER_ORIGIN)).records, 1);
});

test("a tombstone removes every citing projection record and its FTS row", async (t) => {
  const { db, env } = await runtime(t);
  const gone = {
    ...changeFor("experience"), id: 21, claim: "zztombstoneonly claim.",
    applies_when: "zztombstoneonly applies when.", sources: [{
      session: "codex:native-1@origin-a", position: 0, digest: "d".repeat(64), role: "trigger",
    }],
  };
  const survives = {
    ...changeFor("experience"), id: 22, claim: "zzsurvives claim.",
    applies_when: "zzsurvives applies when.", links: [{ older: 21, relation: "supersedes" }], sources: [
      { session: "codex:native-1@origin-a", position: 1, digest: "e".repeat(64), role: "trigger" },
      { session: "codex:native-2@origin-a", position: 2, digest: "f".repeat(64), role: "outcome" },
    ],
  };
  const memoryGone = {
    ...changeFor("memory"), id: 23, action: "zzmemorygone action.",
    applicability: "zzmemorygone applies.", sources: [{
      session: "codex:native-1@origin-a", position: 3, digest: "1".repeat(64), role: "signal",
    }],
  };
  const response = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 0, "tombstone", [gone, survives, memoryGone]));
  assert.equal(response.status, 200, await response.text());
  assert.equal(await ftsCount(db, "zztombstoneonly"), 1);
  assert.equal(await ftsCount(db, "zzmemorygone"), 1);

  const pushed = await postJson(env, "/api/v1/sync/tombstones", {
    protocol: 1,
    origin: ORIGIN,
    base_revision: 0,
    tombstones: [{ harness: "codex", native_id: "native-1", scope: "local", forgotten_at: 100 }],
  });
  assert.equal(pushed.status, 200, await pushed.text());
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_experience WHERE origin_host = ? AND remote_id = 21",
  ).bind(ORIGIN).first().then((row) => row.count), 0);
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_memory WHERE origin_host = ? AND remote_id = 23",
  ).bind(ORIGIN).first().then((row) => row.count), 0);
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_experience WHERE origin_host = ? AND remote_id = 22",
  ).bind(ORIGIN).first().then((row) => row.count), 1);
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_source WHERE origin_host = ? AND remote_id = 22",
  ).bind(ORIGIN).first().then((row) => row.count), 1);
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_search WHERE origin_host = ?",
  ).bind(ORIGIN).first().then((row) => row.count), 1);
  assert.equal(await ftsCount(db, "zztombstoneonly"), 0);
  assert.equal(await ftsCount(db, "zzmemorygone"), 0);
  assert.equal(await ftsCount(db, "zzsurvives"), 1);
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_link WHERE origin_host = ?",
  ).bind(ORIGIN).first().then((row) => row.count), 0);
  assert.equal((await stateOf(db, ORIGIN)).digest, digest("tombstone:0"));
  assert.equal((await stateOf(db, ORIGIN)).records, 1);
});

test("a tombstoned source cannot be projected again by a delta or reconciliation", async (t) => {
  const { db, env } = await runtime(t);
  const record = {
    ...changeFor("experience"), id: 24, claim: "zzresurrect claim.",
    applies_when: "zzresurrect applies.", sources: [{
      session: "codex:native-9@origin-a", position: 0, digest: "9".repeat(64), role: "trigger",
    }],
  };
  assert.equal((await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 0, "before-forget", [record]))).status, 200);
  const pushed = await postJson(env, "/api/v1/sync/tombstones", {
    protocol: 1, origin: ORIGIN, base_revision: 0,
    tombstones: [{ harness: "codex", native_id: "native-9", scope: "global", forgotten_at: 100 }],
  });
  assert.equal(pushed.status, 200, await pushed.text());
  assert.equal(await ftsCount(db, "zzresurrect"), 0);

  const retry = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 0, "before-forget", [record]));
  assert.equal(retry.status, 200, await retry.clone().text());
  assert.equal((await retry.json()).replayed, true);
  const fresh = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 1, "after-forget", [record]));
  assert.equal(fresh.status, 200, await fresh.clone().text());
  assert.equal((await fresh.json()).applied, 0);
  assert.equal(await ftsCount(db, "zzresurrect"), 0);
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_experience WHERE origin_host = ? AND remote_id = 24",
  ).bind(ORIGIN).first().then((row) => row.count), 0);

  const begun = await postJson(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "begin", base_revision: 2,
    digest: digest("resurrect-reconcile"), records: 1,
  });
  assert.equal(begun.status, 200, await begun.clone().text());
  const epoch = (await begun.json()).epoch;
  const applied = await postJson(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "apply", epoch, changes: [record],
  });
  assert.equal(applied.status, 409, await applied.clone().text());
  assert.equal((await applied.json()).error.code, "conflict");
  assert.equal(await ftsCount(db, "zzresurrect"), 0);
});

test("the same native id at two origins never shares a projection row", async (t) => {
  const { db, env } = await runtime(t);
  const record = {
    ...changeFor("experience"), id: 30, claim: "zzcollision shared claim.",
    applies_when: "zzcollision shared applies.",
    sources: [{ session: "codex:native-1@origin-a", position: 0, digest: "2".repeat(64), role: "trigger" }],
  };
  const first = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 0, "collision-a", [record]));
  assert.equal(first.status, 200, await first.text());
  const second = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(OTHER_ORIGIN, 0, "collision-b", [shifted(record, OTHER_ORIGIN)]), OTHER_COLLECTOR);
  assert.equal(second.status, 200, await second.text());
  assert.equal(await db.prepare(
    "SELECT count(*) AS count FROM learning_experience WHERE remote_id = 30",
  ).first().then((row) => row.count), 2);
  assert.equal(await ftsCount(db, "zzcollision"), 2);

  const pushed = await postJson(env, "/api/v1/sync/tombstones", {
    protocol: 1,
    origin: ORIGIN,
    base_revision: 0,
    tombstones: [{ harness: "codex", native_id: "native-1", scope: "local", forgotten_at: 50 }],
  });
  assert.equal(pushed.status, 200, await pushed.text());
  const remaining = await db.prepare(
    "SELECT origin_host FROM learning_experience WHERE remote_id = 30",
  ).all();
  assert.deepEqual(remaining.results.map((row) => row.origin_host), [OTHER_ORIGIN]);
  assert.equal(await ftsCount(db, "zzcollision"), 1);
});

test("a projection record cannot carry authority fields", async (t) => {
  const { db, env } = await runtime(t);
  const record = { ...changeFor("experience"), authority: "instruction" };
  const response = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 0, "authority", [record]));
  assert.equal(response.status, 400);
  assert.equal((await db.prepare("SELECT count(*) AS count FROM learning_state").first()).count, 0);
  const columns = await db.prepare(
    `SELECT name FROM pragma_table_info('learning_experience')
     UNION ALL SELECT name FROM pragma_table_info('learning_memory')`,
  ).all();
  const names = new Set(columns.results.map((row) => row.name));
  for (const forbidden of ["authority", "instruction", "grant", "enabled"]) {
    assert.equal(names.has(forbidden), false, forbidden);
  }
});

test("hello and the state route report the committed learning revision", async (t) => {
  const { env } = await runtime(t);
  const response = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 0, "hello", [changeFor("experience")]));
  assert.equal(response.status, 200);
  const hello = await postJson(env, "/api/v1/sync/hello", { protocol: 1, origin: ORIGIN });
  const helloBody = await hello.json();
  assert.deepEqual(helloBody.learning, { revision: 1, digest: (await response.clone().json()).digest, records: 1 });
  const state = await worker.fetch(
    request(`/api/v1/sync/learning/state?origin=${ORIGIN}`), env, collector());
  const stateBody = await state.json();
  assert.equal(state.status, 200);
  assert.deepEqual(
    { origin: stateBody.origin, revision: stateBody.revision, epoch: stateBody.epoch, records: stateBody.records },
    { origin: ORIGIN, revision: 1, epoch: 1, records: 1 },
  );
});

test("a collector cannot write another origin's projection", async (t) => {
  const { db, env } = await runtime(t);
  const response = await postJson(env, "/api/v1/sync/learning/deltas",
    delta(ORIGIN, 0, "cross-origin", [changeFor("experience")]), OTHER_COLLECTOR);
  assert.equal(response.status, 403);
  assert.equal((await db.prepare("SELECT count(*) AS count FROM learning_state").first()).count, 0);
});
