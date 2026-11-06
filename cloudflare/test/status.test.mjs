import assert from "node:assert/strict";
import { generateKeyPairSync, sign as nodeSign } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";

import { Miniflare } from "miniflare";

import { seedDatabase } from "../bench/seed.mjs";
import { BUNDLE_PATH, context, environment, loadWorker, request } from "./harness.mjs";

const worker = await loadWorker();
const reader = context({ email: "reader@example.test" });

function assertSecurityHeaders(response) {
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
  assert.match(response.headers.get("content-security-policy"), /frame-ancestors 'none'/);
  assert.match(response.headers.get("server-timing"), /^d1;dur=\d+\.\d, r2;dur=\d+\.\d$/);
}

test("an Access identity is required before routing", async () => {
  const { env } = environment();
  for (const absent of [null, {}]) {
    const response = await worker.fetch(request("/api/v1/status"), env, absent);
    assert.equal(response.status, 403);
    assertSecurityHeaders(response);
    const body = await response.json();
    assert.equal(body.error.code, "forbidden");
  }
  const unknown = await worker.fetch(request("/does-not-exist"), env, {});
  assert.equal(unknown.status, 403);
});

test("an identity that throws while reading Access is refused", async () => {
  const { env } = environment();
  const hostile = { access: { getIdentity: () => { throw new Error("boom"); } } };
  const response = await worker.fetch(request("/api/v1/status"), env, hostile);
  assert.equal(response.status, 403);
});

test("a principal outside READERS is refused", async () => {
  const { env } = environment();
  for (const identity of [{ email: "stranger@example.test" }, { service_token_id: "token-id" }, { email: "reader@example.test", service_token_id: "token-id" }]) {
    const response = await worker.fetch(request("/api/v1/status"), env, context(identity));
    assert.equal(response.status, 403);
  }
});

const ZERO_COUNTS = {
  experiences: 0, memories: 0, projects: 0, origins: 0, tombstones: 0,
  generations: 0, snapshots: 0, objects: 0, object_bytes: 0,
};

test("a configured reader reads zero-data status without touching R2", async () => {
  const { env, calls } = environment({ READERS: " Reader@Example.test , other@example.test " });
  const response = await worker.fetch(request("/api/v1/status"), env, context({ email: "READER@example.test" }));
  assert.equal(response.status, 200);
  assertSecurityHeaders(response);
  assert.equal(response.headers.get("content-type"), "application/json; charset=utf-8");
  const body = await response.json();
  assert.equal(body.protocol, 1);
  assert.equal(body.store_id, "usr-memories");
  assert.deepEqual(body.principal, { kind: "reader", email: "reader@example.test" });
  assert.equal(body.ready, true);
  assert.deepEqual(body.counts, ZERO_COUNTS);
  assert.deepEqual(body.origins, []);
  assert.equal(body.last_change, null);
  assert.equal(body.last_verified_snapshot, null);
  assert.deepEqual(calls.bucket, { head: 0, get: 0, put: 0, delete: 0, list: 0, createMultipartUpload: 0 });
});

test("a bound collector token reads status for its origin", async () => {
  const { env } = environment({}, { collectors: { "token-id": { origin_host: "origin-a" } } });
  const response = await worker.fetch(request("/api/v1/status"), env, context({ service_token_id: "token-id" }));
  assert.equal(response.status, 200);
  assertSecurityHeaders(response);
  const body = await response.json();
  assert.deepEqual(body.principal, { kind: "collector", origin_host: "origin-a" });
  assert.deepEqual(body.counts, ZERO_COUNTS);
});

async function database(t) {
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
  const calls = { prepare: 0 };
  const binding = new Proxy(db, {
    get(target, property) {
      if (property === "prepare") {
        return (...args) => {
          calls.prepare += 1;
          return target.prepare(...args);
        };
      }
      const value = Reflect.get(target, property);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  return {
    db,
    calls,
    env: {
      READERS: "reader@example.test",
      ACCESS_TEAM_DOMAIN: "team.example.test",
      ACCESS_AUD: "test-audience",
      DB: binding,
      BUCKET: await miniflare.getR2Bucket("BUCKET"),
    },
  };
}

test("status reports twenty origins with bounded queries and no private content", async (t) => {
  const { db, calls, env } = await database(t);
  const collector = db.prepare(
    "INSERT INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)",
  );
  const sync = db.prepare(
    "INSERT INTO origin_sync(origin_host, revision, last_change_at) VALUES(?, ?, ?)",
  );
  const learning = db.prepare(
    "INSERT INTO learning_state(origin_host, revision, epoch, digest, records, updated_at) VALUES(?, ?, 1, NULL, ?, ?)",
  );
  const experience = db.prepare(
    `INSERT INTO learning_experience(origin_host, epoch, remote_id, record_digest, project_key, project_label,
       kind, claim, applies_when, identifiers, fingerprint, basis, detector, observed_at, captured_at,
       validity, tags, state, sync_revision, pending)
     VALUES(?, 1, ?, ?, ?, ?, 'method', ?, 'applies', 'id', 'fingerprint', 'deterministic', 'synthetic',
            100, 100, '[]', '', 'retained', ?, 0)`,
  );
  for (let index = 0; index < 20; index += 1) {
    const origin = `origin-${String(index).padStart(2, "0")}`;
    await collector.bind(`token-${index}`, origin, `label-${index}`, index).run();
    await sync.bind(origin, 5 + index, 1000 + index).run();
    await learning.bind(origin, 1, 1, 900 + index).run();
    await experience.bind(
      origin, 1, "a".repeat(64), "0123456789abcdef", "usr",
      `zzprivateclaim ${index}`, 2,
    ).run();
  }
  calls.prepare = 0;
  const response = await worker.fetch(request("/api/v1/status"), env, context({ email: "reader@example.test" }));
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(body.origins.length, 20);
  assert.equal(body.counts.origins, 20);
  assert.equal(body.counts.experiences, 20);
  assert.equal(body.counts.projects, 1);
  assert.equal(body.origins[0].learning_records, 1);
  assert.ok(calls.prepare <= 10, `status issued ${calls.prepare} prepares`);
  const serialized = JSON.stringify(body);
  assert.equal(serialized.includes("zzprivateclaim"), false);
  assert.equal(/[0-9a-f]{64}/.test(serialized), false);
  assert.equal(serialized.includes("codex:"), false);
  const own = await worker.fetch(request("/api/v1/status"), env, context({ service_token_id: "token-0" }));
  const ownBody = await own.json();
  assert.equal(own.status, 200);
  assert.deepEqual(ownBody.origins.map((origin) => origin.origin), ["origin-00"]);
  assert.equal(ownBody.counts.origins, 1);
});

test("status counts only live generations and its D1 timing includes the query", async (t) => {
  const { db, env } = await database(t);
  await db.prepare("INSERT INTO origin_sync(origin_host, revision, last_change_at) VALUES(?, ?, ?)")
    .bind("origin-live", 0, 100).run();
  await db.prepare(
    `INSERT INTO remote_object(plaintext_digest, plaintext_size, encoding, ciphertext_digest,
       ciphertext_size, origin_host, created_at)
     VALUES(?, ?, 'identity', ?, ?, ?, ?)`,
  ).bind("a".repeat(64), 10, "b".repeat(64), 10, "origin-live", 100).run();
  await db.prepare(
    `INSERT INTO generation(origin_host, harness, native_id, manifest_digest, source_digest,
       source_size, captured_at, committed_at, revision, manifest_object)
     VALUES(?, 'codex', 'native-live', ?, ?, 10, 100, 100, 1, ?)`,
  ).bind("origin-live", "c".repeat(64), "d".repeat(64), "b".repeat(64)).run();
  await db.prepare("UPDATE origin_sync SET revision = 1 WHERE origin_host = ?").bind("origin-live").run();
  await db.prepare(
    "INSERT INTO tombstone(origin_host, harness, native_id, scope, forgotten_at, revision) VALUES(?, ?, ?, ?, ?, ?)",
  ).bind("origin-live", "codex", "native-live", "global", 200, 2).run();
  const response = await worker.fetch(request("/api/v1/status"), env, context({ email: "reader@example.test" }));
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(body.counts.generations, 0);
  assert.equal(body.origins[0].generations, 0);
  assert.equal(body.counts.origins, body.origins.length);
  assert.match(response.headers.get("server-timing"), /d1;dur=(?!0\.0)/);
});

test("an unknown or disabled collector token is refused", async () => {
  const { env } = environment({}, { collectors: { "disabled-id": { origin_host: "origin-a", disabled: true } } });
  for (const token of ["missing-id", "disabled-id"]) {
    const response = await worker.fetch(request("/api/v1/status"), env, context({ service_token_id: token }));
    assert.equal(response.status, 403);
  }
});

test("HEAD returns the status headers without a body", async () => {
  const { env } = environment();
  const response = await worker.fetch(request("/api/v1/status", { method: "HEAD" }), env, reader);
  assert.equal(response.status, 200);
  assertSecurityHeaders(response);
  assert.equal(await response.text(), "");
});

test("an unconfigured READERS list fails closed", async () => {
  const { env } = environment({ READERS: "" });
  const response = await worker.fetch(request("/api/v1/status"), env, reader);
  assert.equal(response.status, 403);
});

test("wrong methods and unknown routes return the JSON envelope", async () => {
  const { env } = environment();
  const wrong = await worker.fetch(request("/api/v1/status", { method: "POST" }), env, reader);
  assert.equal(wrong.status, 405);
  assert.equal(wrong.headers.get("allow"), "GET, HEAD");
  assert.equal((await wrong.json()).error.code, "method_not_allowed");
  const missing = await worker.fetch(request("/api/v1/unknown"), env, reader);
  assert.equal(missing.status, 404);
  assertSecurityHeaders(missing);
  assert.equal((await missing.json()).error.code, "not_found");
});

test("a missing or failing store reports unavailable", async () => {
  for (const options of [{ storeMeta: null }, { dbFailure: "prepare" }, { dbFailure: "query" }]) {
    const { env } = environment({}, options);
    const response = await worker.fetch(request("/api/v1/status"), env, reader);
    assert.equal(response.status, 503);
    assertSecurityHeaders(response);
    assert.equal((await response.json()).error.code, "unavailable");
  }
  const { env } = environment({}, { dbFailure: "query", collectors: { "token-id": { origin_host: "origin-a" } } });
  const response = await worker.fetch(request("/api/v1/status"), env, context({ service_token_id: "token-id" }));
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error.code, "unavailable");
});

function bundleSources(dir) {
  const sources = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) sources.push(...bundleSources(path));
    else if (path.endsWith(".js")) sources.push(readFileSync(path, "utf8"));
  }
  return sources;
}

test("the deployed bundle carries no test hook", async () => {
  assert.doesNotMatch(bundleSources(dirname(BUNDLE_PATH)).join("\n"), /x-test-access|__TEST_ACCESS__/);
});

function encode(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function accessToken(privateKey, claims, header = { alg: "RS256", kid: "key-1", typ: "JWT" }) {
  const signingInput = `${encode(header)}.${encode(claims)}`;
  const signature = nodeSign("RSA-SHA256", Buffer.from(signingInput), privateKey).toString("base64url");
  return `${signingInput}.${signature}`;
}

function serviceClaims(domain, audience, overrides = {}) {
  return {
    type: "app",
    aud: [audience],
    exp: Math.floor(Date.now() / 1000) + 600,
    iat: Math.floor(Date.now() / 1000) - 10,
    iss: `https://${domain}`,
    sub: "",
    common_name: "integration-client.access",
    ...overrides,
  };
}

async function withKeyset(jwk, kid, action) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input) => {
    calls.push(String(input));
    return Response.json({ keys: [{ ...jwk, kid, alg: "RS256" }] });
  };
  try {
    return { result: await action(), calls };
  } finally {
    globalThis.fetch = original;
  }
}

test("a verified Access assertion authenticates a collector by its client id", async () => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = publicKey.export({ format: "jwk" });
  const domain = "team-integration.example.test";
  const audience = "integration-audience";
  const clientId = "integration-client.access";
  const assertion = accessToken(privateKey, serviceClaims(domain, audience));
  const { env, calls } = environment(
    { ACCESS_TEAM_DOMAIN: domain, ACCESS_AUD: audience },
    { collectors: { [clientId]: { origin_host: "origin-a" } } },
  );
  const exchange = await withKeyset(jwk, "key-1", () => worker.fetch(
    request("/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": assertion } }),
    env,
    {},
  ));
  assert.equal(exchange.result.status, 200);
  assert.deepEqual((await exchange.result.json()).principal, { kind: "collector", origin_host: "origin-a" });
  assert.deepEqual(exchange.calls, [`https://${domain}/cdn-cgi/access/certs`]);
  assert.ok(calls.db.prepare >= 2);
});

test("a forged or mis-scoped Access assertion is refused before D1", async () => {
  const real = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const attacker = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = real.publicKey.export({ format: "jwk" });
  const cases = [
    { domain: "forged-team.example.test", token: accessToken(attacker.privateKey, serviceClaims("forged-team.example.test", "aud")) },
    { domain: "scope-team.example.test", token: accessToken(real.privateKey, serviceClaims("scope-team.example.test", "other-audience")) },
  ];
  for (const item of cases) {
    const { env, calls } = environment({ ACCESS_TEAM_DOMAIN: item.domain, ACCESS_AUD: "aud" });
    const response = await withKeyset(jwk, "key-1", () => worker.fetch(
      request("/api/v1/status", { headers: { "Cf-Access-Jwt-Assertion": item.token } }),
      env,
      {},
    )).then((exchange) => exchange.result);
    assert.equal(response.status, 403);
    assert.equal((await response.json()).error.code, "forbidden");
    assert.deepEqual(calls.db, { prepare: 0, first: 0, batch: 0, exec: 0, dump: 0, withSession: 0 });
  }
});
