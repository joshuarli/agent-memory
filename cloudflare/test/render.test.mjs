import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { Miniflare } from "miniflare";

import { seedDatabase } from "../bench/seed.mjs";
import { loadWorker, request } from "./harness.mjs";

const worker = await loadWorker();
const COLLECTOR = "collector-client.access";
const ORIGIN = "origin-a";
const PROJECT = "0123456789abcdef";

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

function experience(id, overrides = {}) {
  return {
    record: "experience", id, digest: digest("experience:" + id), project_key: PROJECT,
    project_label: "usr", kind: "method", claim: `Synthetic experience ${id}.`,
    applies_when: `Synthetic applies ${id}.`, identifiers: `token-${id}`,
    fingerprint: `fingerprint-${id}`, basis: "deterministic", detector: "synthetic",
    revision: null, worktree: null, observed_at: 1000 - id, captured_at: 1000 - id,
    validity: "[]", tags: "", state: "retained", state_reason: null, state_at: null,
    sources: [{ session: `codex:native-${id}@origin-a`, position: 0, digest: digest("source:" + id), role: "trigger" }],
    links: [],
    ...overrides,
  };
}

function memory(id, overrides = {}) {
  return {
    record: "memory", id, digest: digest("memory:" + id), project_key: PROJECT,
    project_label: "usr", action: `Synthetic memory ${id}.`, applicability: `Synthetic applies ${id}.`,
    revision: null, validity: "[]", state: "approved", created_at: 1000 - id,
    sources: [{ session: `codex:native-m${id}@origin-a`, position: 0, digest: digest("source:m" + id), role: "signal" }],
    links: [],
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
  await db.prepare(
    "INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)",
  ).bind(COLLECTOR, ORIGIN, "test", 0).run();
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

function collector() {
  return { access: { getIdentity: async () => ({ service_token_id: COLLECTOR }) } };
}

function reader() {
  return { access: { getIdentity: async () => ({ email: "reader@example.test" }) } };
}

async function seed(env, changes) {
  const response = await worker.fetch(request("/api/v1/sync/learning/deltas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      protocol: 1, origin: ORIGIN, base_revision: 0, digest: digest("render"), changes,
    }),
  }), env, collector());
  assert.equal(response.status, 200, await response.text());
}

async function page(env, path, identity = reader(), method = "GET") {
  const response = await worker.fetch(request(path, { method }), env, identity);
  return { response, body: await response.text() };
}

function assertSafeHeaders(response) {
  assert.equal(response.headers.get("cache-control"), "no-store");
  const csp = response.headers.get("content-security-policy") ?? "";
  assert.match(csp, /default-src 'none'/);
  assert.equal(csp.includes("script-src"), false);
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
}

function assertNoExternalRequests(body) {
  assert.equal(/<(script|img|iframe|object|embed)\b/i.test(body), false);
  assert.equal(/\b(src|href)=["']https?:/i.test(body), false);
  assert.equal(/@import|url\(/i.test(body), false);
}

test("an empty database renders the explainer and the search box", async (t) => {
  const { env } = await runtime(t);
  const { response, body } = await page(env, "/");
  assert.equal(response.status, 200);
  assertSafeHeaders(response);
  assert.match(body, /No learning records are projected yet/);
  assert.match(body, /<form class="search"/);
  assert.match(body, /autofocus/);
  assertNoExternalRequests(body);
  assert.equal(body.includes("<script"), false);
  const head = await page(env, "/", reader(), "HEAD");
  assert.equal(head.response.status, 200);
  assert.equal(head.body, "");
});

test("a hostile record is escaped in the list and the detail page", async (t) => {
  const { env } = await runtime(t);
  await seed(env, [experience(1, {
    claim: `<script>alert("x")</script> & "quotes" 'apostrophe' \u0001 \u001b end.`,
    applies_when: `<img src=x onerror=alert(1)>`,
  })]);
  for (const path of ["/", "/search", "/search?state=all", "/m/origin-a/experience/1"]) {
    const { response, body } = await page(env, path);
    assert.equal(response.status, 200, path);
    assertSafeHeaders(response);
    assert.equal(body.includes("<script>"), false, path);
    assert.equal(body.includes("<img"), false, path);
    assert.match(body, /&lt;script&gt;/, path);
    assert.equal(body.includes("\u0001"), false, path);
    assert.equal(body.includes("\u001b"), false, path);
    assertNoExternalRequests(body);
  }
});

test("search shows live records by default and history on request", async (t) => {
  const { env } = await runtime(t);
  await seed(env, [
    experience(1, { claim: "Live experience one.", observed_at: 1000 }),
    experience(2, { claim: "Retired experience two.", state: "retired", observed_at: 900 }),
    memory(3, { action: "Approved memory three.", created_at: 950 }),
    memory(4, { action: "Superseded memory four.", state: "superseded", created_at: 800 }),
  ]);
  const live = await page(env, "/search");
  assert.match(live.body, /Live experience one\./);
  assert.match(live.body, /Approved memory three\./);
  assert.equal(live.body.includes("Retired experience two."), false);
  assert.equal(live.body.includes("Superseded memory four."), false);
  assert.match(live.body, /Reviewed/);
  assert.match(live.body, /Observed/);
  const history = await page(env, "/search?state=all");
  assert.match(history.body, /Retired experience two\./);
  assert.match(history.body, /Superseded memory four\./);
  assert.match(history.body, /Advisory/);
  const filtered = await page(env, `/search?project=${PROJECT}&type=memory&kind=`);
  assert.match(filtered.body, /Approved memory three\./);
  assert.equal(filtered.body.includes("Live experience one."), false);
});

test("search paginates and keeps the filter values", async (t) => {
  const { env } = await runtime(t);
  const changes = [];
  for (let id = 1; id <= 25; id += 1) {
    changes.push(experience(id, { claim: `Paged record ${id} ` + "x".repeat(280), observed_at: 2000 - id }));
  }
  await seed(env, changes);
  const first = await page(env, "/search?q=x");
  assert.equal(first.response.status, 200);
  assert.match(first.body, /20 results on this page\./);
  assert.match(first.body, /Next page/);
  assert.ok(first.body.length <= 64 * 1024, `page is ${first.body.length} bytes`);
  const next = first.body.match(/href="(\/search\?[^"]*after=[^"]*)"/);
  assert.notEqual(next, null);
  const secondUrl = next[1].replaceAll("&amp;", "&");
  const second = await page(env, secondUrl);
  assert.equal(second.response.status, 200, secondUrl + " " + second.body.slice(0, 200));
  assert.match(second.body, /Paged record 25/);
  assert.equal(second.body.includes("Paged record 1 "), false);
  assert.equal(/href="[^"]*after=[^"]*"/.test(second.body), false);
});

test("detail shows provenance, digests, and relationships without evidence links", async (t) => {
  const { env } = await runtime(t);
  const older = experience(1, { claim: "Older synthetic claim.", observed_at: 900 });
  const newer = experience(2, { claim: "Newer synthetic claim.", observed_at: 1000, links: [{ older: 1, relation: "supersedes" }] });
  await seed(env, [older, newer]);
  const { response, body } = await page(env, "/m/origin-a/experience/2");
  assert.equal(response.status, 200);
  assert.match(body, /Newer synthetic claim\./);
  assert.match(body, /codex:native-2@origin-a/);
  assert.match(body, new RegExp(newer.sources[0].digest));
  assert.match(body, /supersedes/);
  assert.match(body, /href="\/m\/origin-a\/experience\/1"/);
  assert.equal(body.includes("/api/v1/sync/"), false);
  assert.equal(body.includes("ciphertext"), false);
  const missing = await page(env, "/m/origin-a/experience/999");
  assert.equal(missing.response.status, 404);
  const memory = await page(env, "/m/origin-a/memory/99");
  assert.equal(memory.response.status, 404);
});

test("status reports counts without record text", async (t) => {
  const { env } = await runtime(t);
  await seed(env, [experience(1, { claim: "zzprivateclaim should not appear." }), memory(2)]);
  const { response, body } = await page(env, "/status");
  assert.equal(response.status, 200);
  assertSafeHeaders(response);
  assert.match(body, /1<\/b> retained experiences/);
  assert.match(body, /1<\/b> approved memories/);
  assert.equal(body.includes("zzprivateclaim"), false);
  assertNoExternalRequests(body);
});

test("browsing requires a reader and refuses writes", async (t) => {
  const { env } = await runtime(t);
  const forbidden = await page(env, "/", collector());
  assert.equal(forbidden.response.status, 403);
  const write = await page(env, "/", reader(), "POST");
  assert.equal(write.response.status, 405);
  const unknown = await page(env, "/m/not-a-route");
  assert.equal(unknown.response.status, 404);
});
