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

function source(session) {
  return { session, position: 0, digest: digest(session), role: "trigger" };
}

function experience(id, overrides = {}) {
  return {
    record: "experience", id, digest: digest("experience:" + id), project_key: "0123456789abcdef",
    project_label: "usr", kind: "method", claim: `Synthetic experience ${id}.`,
    applies_when: `Synthetic applies ${id}.`, identifiers: `token-${id}`,
    fingerprint: `fingerprint-${id}`, basis: "deterministic", detector: "synthetic",
    revision: null, worktree: null, observed_at: 1000 - id, captured_at: 1000 - id,
    validity: "[]", tags: "", state: "retained", state_reason: null, state_at: null,
    sources: [source(`codex:native-${id}@origin-a`)], links: [],
    ...overrides,
  };
}

function memory(id, overrides = {}) {
  return {
    record: "memory", id, digest: digest("memory:" + id), project_key: "0123456789abcdef",
    project_label: "usr", action: `Synthetic memory ${id}.`, applicability: `Synthetic applies ${id}.`,
    revision: null, validity: "[]", state: "approved", created_at: 1000 - id,
    sources: [source(`codex:native-m${id}@origin-a`)], links: [],
    ...overrides,
  };
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

function post(env, path, value, client = COLLECTOR) {
  return worker.fetch(request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  }), env, collector(client));
}

async function applyDelta(env, baseRevision, name, changes, client = COLLECTOR, origin = ORIGIN) {
  return await post(env, "/api/v1/sync/learning/deltas", {
    protocol: 1, origin, base_revision: baseRevision, digest: digest(name + ":" + baseRevision), changes,
  }, client);
}

async function keyPage(env, cursor = null, limit = 100) {
  const query = `/api/v1/sync/learning/keys?origin=${ORIGIN}&limit=${limit}`
    + (cursor === null ? "" : "&after=" + encodeURIComponent(cursor));
  const response = await worker.fetch(request(query), env, collector());
  return { status: response.status, body: await response.json() };
}

async function search(env, term) {
  const response = await worker.fetch(request(`/api/v1/memories?state=all&q=${encodeURIComponent(term)}`), env,
    { access: { getIdentity: async () => ({ email: "reader@example.test" }) } });
  return (await response.json()).memories.map((row) => row.record + ":" + row.id);
}

test("the key index paginates both record types with digests", async (t) => {
  const { env } = await runtime(t);
  const records = [experience(1), experience(2), memory(3), memory(4)];
  const response = await applyDelta(env, 0, "keys", records);
  assert.equal(response.status, 200, await response.text());
  const seen = [];
  let cursor = null;
  for (let page = 0; page < 10; page += 1) {
    const { status, body } = await keyPage(env, cursor, 2);
    assert.equal(status, 200);
    seen.push(...body.keys);
    cursor = body.next_cursor;
    if (cursor === null) break;
  }
  assert.deepEqual(seen.map((key) => key.record + ":" + key.id),
    ["experience:1", "experience:2", "memory:3", "memory:4"]);
  assert.deepEqual(seen.map((key) => key.digest), records.map((record) => record.digest));
  const after = await keyPage(env, seen[1].digest);
  assert.equal(after.status, 400);
});

test("a staged reconciliation stays invisible until commit", async (t) => {
  const { env } = await runtime(t);
  assert.equal((await applyDelta(env, 0, "live", [experience(1), memory(2)])).status, 200);
  const live = await keyPage(env);
  assert.equal(live.body.revision, 1);
  assert.equal(live.body.epoch, 1);

  const staged = experience(9, { claim: "zzstagedonly replacement claim." });
  const full = [staged, memory(2, { action: "zzstagedonly replacement memory." })];
  const begin = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "begin", base_revision: 1,
    digest: digest("full"), records: full.length,
  });
  const begun = await begin.json();
  assert.equal(begin.status, 200, JSON.stringify(begun));
  assert.equal(begun.epoch, 2);
  assert.equal(begun.current, false);
  const applied = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "apply", epoch: 2, changes: full,
  });
  const appliedBody = await applied.json();
  assert.equal(applied.status, 200, JSON.stringify(appliedBody));
  assert.equal(appliedBody.staged, 2);

  assert.deepEqual(await search(env, "zzstagedonly"), []);
  const during = await keyPage(env);
  assert.equal(during.body.epoch, 1);
  assert.deepEqual(during.body.keys.map((key) => key.id), [1, 2]);
  const state = await worker.fetch(
    request(`/api/v1/sync/learning/state?origin=${ORIGIN}`), env, collector());
  assert.equal((await state.json()).epoch, 1);

  const commit = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "commit", epoch: 2, base_revision: 1,
    digest: digest("full"), records: 2,
  });
  const committed = await commit.json();
  assert.equal(commit.status, 200, JSON.stringify(committed));
  assert.deepEqual(
    { revision: committed.revision, epoch: committed.epoch, records: committed.records, applied: committed.applied },
    { revision: 2, epoch: 2, records: 2, applied: 2 },
  );
  assert.deepEqual((await search(env, "zzstagedonly")).sort(), ["experience:9", "memory:2"]);
  assert.deepEqual((await search(env, "Synthetic experience 1")), []);
  const after = await keyPage(env);
  assert.equal(after.body.epoch, 2);
  assert.deepEqual(after.body.keys.map((key) => key.record + ":" + key.id), ["experience:9", "memory:2"]);
  assert.deepEqual(after.body.keys.map((key) => key.digest), full.map((record) => record.digest));
});

test("a repeated identical reconciliation re-acks without a write", async (t) => {
  const { env } = await runtime(t);
  assert.equal((await applyDelta(env, 0, "live", [experience(1)])).status, 200);
  const full = [experience(5)];
  const fullDigest = digest("identical");
  const begin = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "begin", base_revision: 1,
    digest: fullDigest, records: 1,
  });
  assert.equal(begin.status, 200);
  const epoch = (await begin.json()).epoch;
  assert.equal((await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "apply", epoch, changes: full,
  })).status, 200);
  assert.equal((await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "commit", epoch, base_revision: 1,
    digest: fullDigest, records: 1,
  })).status, 200);

  const replayBegin = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "begin", base_revision: 2,
    digest: fullDigest, records: 1,
  });
  const replayed = await replayBegin.json();
  assert.equal(replayBegin.status, 200);
  assert.equal(replayed.current, true);
  assert.equal(replayed.revision, 2);
  assert.equal(replayed.epoch, 2);
  const replayCommit = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "commit", epoch: 2, base_revision: 2,
    digest: fullDigest, records: 1,
  });
  assert.equal(replayCommit.status, 200);
  assert.equal((await replayCommit.json()).replayed, true);
  assert.equal((await keyPage(env)).body.revision, 2);
});

test("a concurrent delta is refused while staging and succeeds after it ends", async (t) => {
  const { env } = await runtime(t);
  assert.equal((await applyDelta(env, 0, "live", [experience(1)])).status, 200);
  const begin = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "begin", base_revision: 1,
    digest: digest("stage"), records: 1,
  });
  const epoch = (await begin.json()).epoch;
  const concurrent = await applyDelta(env, 1, "concurrent", [experience(7)]);
  assert.equal(concurrent.status, 409);
  const refused = await concurrent.json();
  assert.equal(refused.error.code, "conflict");
  assert.equal(refused.state.revision, 1);
  assert.equal((await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "abort", epoch,
  })).status, 200);
  const retried = await applyDelta(env, 1, "concurrent", [experience(7)]);
  assert.equal(retried.status, 200, await retried.text());
  assert.equal((await keyPage(env)).body.revision, 2);
});

test("reconciliation fences a stale commit and a short stage", async (t) => {
  const { env } = await runtime(t);
  assert.equal((await applyDelta(env, 0, "live", [experience(1)])).status, 200);
  const begin = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "begin", base_revision: 1,
    digest: digest("short"), records: 2,
  });
  const epoch = (await begin.json()).epoch;
  assert.equal((await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "apply", epoch, changes: [experience(2)],
  })).status, 200);
  const short = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "commit", epoch, base_revision: 1,
    digest: digest("short"), records: 2,
  });
  assert.equal(short.status, 409);
  assert.equal((await short.json()).error.code, "conflict");
  const stale = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "commit", epoch, base_revision: 9,
    digest: digest("short"), records: 1,
  });
  assert.equal(stale.status, 409);
  assert.equal((await stale.json()).error.code, "stale_revision");
  assert.equal((await keyPage(env)).body.epoch, 1);
});

test("a tombstone invalidates an interrupted reconciliation", async (t) => {
  const { env } = await runtime(t);
  assert.equal((await applyDelta(env, 0, "live", [experience(1)])).status, 200);
  const begin = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "begin", base_revision: 1,
    digest: digest("tombstoned"), records: 1,
  });
  const epoch = (await begin.json()).epoch;
  const pushed = await post(env, "/api/v1/sync/tombstones", {
    protocol: 1, origin: ORIGIN, base_revision: 0,
    tombstones: [{ harness: "codex", native_id: "native-9", scope: "local", forgotten_at: 10 }],
  });
  assert.equal(pushed.status, 200, await pushed.text());
  const commit = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "commit", epoch, base_revision: 1,
    digest: digest("tombstoned"), records: 1,
  });
  assert.equal(commit.status, 409);
  assert.equal((await commit.json()).error.code, "conflict");
  assert.equal((await keyPage(env)).body.epoch, 1);
});

test("reconciliation is origin-scoped", async (t) => {
  const { env } = await runtime(t);
  assert.equal((await applyDelta(env, 0, "a", [experience(1)])).status, 200);
  const forbidden = await post(env, "/api/v1/sync/learning/reconcile", {
    protocol: 1, origin: ORIGIN, action: "begin", base_revision: 1,
    digest: digest("cross"), records: 0,
  }, OTHER_COLLECTOR);
  assert.equal(forbidden.status, 403);
  const keys = await worker.fetch(
    request(`/api/v1/sync/learning/keys?origin=${ORIGIN}`), env, collector(OTHER_COLLECTOR));
  assert.equal(keys.status, 403);
  const own = await worker.fetch(
    request(`/api/v1/sync/learning/keys?origin=${OTHER_ORIGIN}`), env, collector(OTHER_COLLECTOR));
  assert.equal(own.status, 200);
  assert.deepEqual((await own.json()).keys, []);
});
