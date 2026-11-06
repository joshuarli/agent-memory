import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { Miniflare } from "miniflare";

import { seedDatabase } from "../bench/seed.mjs";
import { loadWorker, request } from "./harness.mjs";

const BUNDLE = new URL("../.cloudflare/output/v0/workers/default/bundle/", import.meta.url);
const { decodeSearchFilters, searchStatement } = await import(new URL("memories.js", BUNDLE).href);
const worker = await loadWorker();
const COLLECTOR = "collector-client.access";
const OTHER_COLLECTOR = "other-client.access";
const ORIGIN = "origin-a";
const OTHER_ORIGIN = "origin-b";
const PROJECT = "0123456789abcdef";
const OTHER_PROJECT = "fedcba9876543210";

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

function source(session, position = 0, role = "trigger") {
  return { session, position, digest: digest(session + position), role };
}

function experience(id, overrides = {}) {
  return {
    record: "experience", id, digest: digest("experience:" + id), project_key: PROJECT, project_label: "usr",
    kind: "method", claim: `Synthetic experience ${id}.`,
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
    record: "memory", id, digest: digest("memory:" + id), project_key: PROJECT, project_label: "usr",
    action: `Synthetic memory ${id}.`, applicability: `Synthetic applicability ${id}.`,
    revision: null, validity: "[]", state: "approved", created_at: 1000 - id,
    sources: [source(`codex:native-${id}@origin-a`, 0, "signal")], links: [],
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

function collector(client = COLLECTOR) {
  return { access: { getIdentity: async () => ({ service_token_id: client }) } };
}

function reader() {
  return { access: { getIdentity: async () => ({ email: "reader@example.test" }) } };
}

async function seed(env) {
  const changes = [
    experience(1, { claim: "zebra reproduction uses the synthetic driver.", observed_at: 1000 }),
    experience(2, { state: "retired", observed_at: 900 }),
    experience(3, {
      observed_at: 800, claim: "Searching for no:cacheprovider needs a quoted phrase.",
      kind: "gotcha", project_key: OTHER_PROJECT,
    }),
    memory(4, { created_at: 950 }),
    memory(5, { state: "superseded", created_at: 700 }),
  ];
  changes[0].links = [{ older: 3, relation: "supersedes" }];
  const response = await worker.fetch(request("/api/v1/sync/learning/deltas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      protocol: 1, origin: ORIGIN, base_revision: 0, digest: digest("search"), changes,
    }),
  }), env, collector());
  assert.equal(response.status, 200, await response.text());
}

async function get(env, path, identity = collector(), method = "GET") {
  return await worker.fetch(request(path, { method }), env, identity);
}

function ids(body) {
  return body.memories.map((memory) => memory.record + ":" + memory.id);
}

test("search defaults to live records and filters every documented field", async (t) => {
  const { env } = await runtime(t);
  await seed(env);
  const page = await get(env, "/api/v1/memories?limit=20");
  const body = await page.json();
  assert.equal(page.status, 200, JSON.stringify(body));
  assert.deepEqual(ids(body), ["experience:1", "memory:4", "experience:3"]);
  assert.equal(body.memories[0].marker, "Observed");
  assert.equal(body.memories[1].marker, "Reviewed");
  assert.equal(body.memories[0].source_count, 1);

  const history = await (await get(env, "/api/v1/memories?state=all")).json();
  assert.deepEqual(ids(history), ["experience:1", "memory:4", "experience:2", "experience:3", "memory:5"]);
  const retired = await (await get(env, "/api/v1/memories?state=retired")).json();
  assert.deepEqual(ids(retired), ["experience:2"]);
  const memories = await (await get(env, "/api/v1/memories?type=memory")).json();
  assert.deepEqual(ids(memories), ["memory:4"]);
  const gotchas = await (await get(env, "/api/v1/memories?kind=gotcha")).json();
  assert.deepEqual(ids(gotchas), ["experience:3"]);
  const project = await (await get(env, `/api/v1/memories?project=${OTHER_PROJECT}`)).json();
  assert.deepEqual(ids(project), ["experience:3"]);
  const ranged = await (await get(env, "/api/v1/memories?since=850&until=1000")).json();
  assert.deepEqual(ids(ranged), ["experience:1", "memory:4"]);
  const origin = await (await get(env, "/api/v1/memories?origin=origin-a&state=all")).json();
  assert.equal(origin.memories.length, 5);

  const zebra = await (await get(env, "/api/v1/memories?q=zebra")).json();
  assert.deepEqual(ids(zebra), ["experience:1"]);
  const phrase = await (await get(env, "/api/v1/memories?q=no%3Acacheprovider")).json();
  assert.deepEqual(ids(phrase), ["experience:3"]);
  const multiple = await (await get(env, "/api/v1/memories?q=synthetic%20driver")).json();
  assert.deepEqual(ids(multiple), ["experience:1"]);
});

test("cursor pagination visits every record exactly once", async (t) => {
  const { env } = await runtime(t);
  await seed(env);
  const seen = [];
  let cursor = null;
  for (let page = 0; page < 10; page += 1) {
    const query = "/api/v1/memories?state=all&limit=2" + (cursor === null ? "" : "&after=" + encodeURIComponent(cursor));
    const body = await (await get(env, query)).json();
    seen.push(...ids(body));
    cursor = body.next_cursor;
    if (cursor === null) break;
  }
  assert.deepEqual(seen, ["experience:1", "memory:4", "experience:2", "experience:3", "memory:5"]);
  assert.equal(cursor, null);
});

test("a page of twenty records issues at most two D1 queries beyond auth", async (t) => {
  const { env, calls } = await runtime(t);
  const changes = [];
  for (let id = 1; id <= 25; id += 1) {
    changes.push(experience(id, { observed_at: 2000 + id }));
  }
  const response = await worker.fetch(request("/api/v1/sync/learning/deltas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ protocol: 1, origin: ORIGIN, base_revision: 0, digest: digest("page"), changes }),
  }), env, collector());
  assert.equal(response.status, 200);
  calls.prepare = 0;
  const page = await get(env, "/api/v1/memories?limit=20&state=all", reader());
  assert.equal(page.status, 200);
  assert.equal((await page.json()).memories.length, 20);
  assert.equal(calls.prepare, 2);
  calls.prepare = 0;
  const collectorPage = await get(env, "/api/v1/memories?limit=20&state=all", collector());
  assert.equal(collectorPage.status, 200);
  assert.ok(calls.prepare <= 3, `collector page issued ${calls.prepare} queries`);
});

test("the query plan uses FTS and composite indexes without scanning filtered tables", async (t) => {
  const { db } = await runtime(t);
  const cases = [
    ["", ["SEARCH e USING INDEX learning_experience_state_observed", "SEARCH m USING INDEX learning_memory_state_created"]],
    ["?origin=origin-a", ["SEARCH e USING INDEX learning_experience_origin", "SEARCH m USING INDEX learning_memory_origin"]],
    ["?project=0123456789abcdef", ["SEARCH e USING INDEX learning_experience_project", "SEARCH m USING INDEX learning_memory_project"]],
    ["?q=zebra", ["SCAN memory_fts VIRTUAL TABLE INDEX", "SEARCH s USING INTEGER PRIMARY KEY"]],
  ];
  for (const [query, expected] of cases) {
    const filters = decodeSearchFilters(new URL("https://memories.test/api/v1/memories" + query));
    assert.notEqual(filters, null, query);
    const statement = searchStatement(filters);
    const plan = (await db.prepare("EXPLAIN QUERY PLAN " + statement.sql).bind(...statement.params).all())
      .results.map((row) => row.detail).join("\n");
    for (const fragment of expected) assert.ok(plan.includes(fragment), `${query}: ${fragment}\n${plan}`);
    assert.equal(/SCAN (e|m)\b/.test(plan), false, `${query}\n${plan}`);
  }
  const deep = decodeSearchFilters(new URL("https://memories.test/api/v1/memories?state=all&limit=20&after="
    + encodeURIComponent(Buffer.from(JSON.stringify(["1000", "experience", "1"])).toString("base64url"))));
  assert.notEqual(deep, null);
  const deepPlan = (await db.prepare("EXPLAIN QUERY PLAN " + searchStatement(deep).sql)
    .bind(...searchStatement(deep).params).all()).results.map((row) => row.detail).join("\n");
  assert.ok(deepPlan.includes("SEARCH e USING INDEX learning_experience_observed ((observed_at,remote_id)<(?,?))"), deepPlan);
});

test("detail returns provenance and both link directions", async (t) => {
  const { env } = await runtime(t);
  await seed(env);
  const detail = await (await get(env, "/api/v1/memories/origin-a/experience/1")).json();
  assert.equal(detail.memory.record, "experience");
  assert.equal(detail.memory.claim, "zebra reproduction uses the synthetic driver.");
  assert.deepEqual(detail.sources.map((row) => row.session), ["codex:native-1@origin-a"]);
  assert.deepEqual(detail.links.map((row) => [row.direction, row.other, row.relation]), [["outgoing", 3, "supersedes"]]);
  const target = await (await get(env, "/api/v1/memories/origin-a/experience/3")).json();
  assert.deepEqual(target.links.map((row) => [row.direction, row.other, row.relation]), [["incoming", 1, "supersedes"]]);
  const memory = await (await get(env, "/api/v1/memories/origin-a/memory/4")).json();
  assert.deepEqual(memory.links, []);
  assert.equal(memory.memory.marker, "Reviewed");
  const missing = await get(env, "/api/v1/memories/origin-a/experience/999");
  assert.equal(missing.status, 404);
  assert.equal((await get(env, "/api/v1/memories/origin-a/insight/1")).status, 404);
});

test("a page above the D1 parameter limit still returns every source", async (t) => {
  const { env } = await runtime(t);
  const changes = Array.from({ length: 40 }, (_value, index) => experience(100 + index, {
    claim: `Synthetic full page ${index}.`,
    applies_when: `Synthetic full page applies ${index}.`,
  }));
  const seeded = await worker.fetch(request("/api/v1/sync/learning/deltas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      protocol: 1, origin: ORIGIN, base_revision: 0, digest: digest("full-page"), changes,
    }),
  }), env, collector());
  assert.equal(seeded.status, 200, await seeded.text());
  for (const limit of [34, 40, 100]) {
    const response = await get(env, `/api/v1/memories?state=all&limit=${limit}`);
    const body = await response.json();
    assert.equal(response.status, 200, `limit=${limit}: ${JSON.stringify(body)}`);
    assert.equal(body.memories.length, Math.min(limit, 40));
    assert.ok(body.memories.every((memory) => memory.source_count === 1));
  }
  const html = await get(env, "/search?state=all&limit=40", reader());
  assert.equal(html.status, 200);
});

test("invalid search filters are refused before a query", async (t) => {
  const { env, calls } = await runtime(t);
  await seed(env);
  calls.prepare = 0;
  for (const query of [
    "?limit=0", "?limit=101", "?limit=x", "?state=unknown", "?type=insight",
    "?kind=unknown", "?kind=method&type=memory", "?project=short", "?origin=bad%20origin",
    "?since=-1", "?since=5&until=4", "?after=not-a-cursor", "?q=%20%20",
  ]) {
    const response = await get(env, "/api/v1/memories" + query, reader());
    assert.equal(response.status, 400, query);
  }
  assert.equal(calls.prepare, 0);
  const wrongMethod = await get(env, "/api/v1/memories", reader(), "POST");
  assert.equal(wrongMethod.status, 405);
});

test("a collector reads only its own origin while a reader reads every origin", async (t) => {
  const { env } = await runtime(t);
  await seed(env);
  const own = await (await get(env, "/api/v1/memories?state=all", collector())).json();
  assert.equal(own.memories.length, 5);
  const forbidden = await get(env, "/api/v1/memories?origin=origin-b", collector());
  assert.equal(forbidden.status, 403);
  const all = await (await get(env, "/api/v1/memories?state=all", reader())).json();
  assert.equal(all.memories.length, 5);
  const other = await get(env, "/api/v1/memories/origin-b/experience/1", collector());
  assert.equal(other.status, 403);
});


test("reader pagination and source counts distinguish identical records from different origins", async (t) => {
  const { env } = await runtime(t);
  for (const [origin, client, sources] of [
    [ORIGIN, COLLECTOR, [source("codex:first@origin-a")]],
    [OTHER_ORIGIN, OTHER_COLLECTOR, [source("codex:second@origin-b"), source("codex:third@origin-b")]],
  ]) {
    const response = await worker.fetch(request("/api/v1/sync/learning/deltas", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ protocol: 1, origin, base_revision: 0, digest: digest(origin),
        changes: [experience(1, { observed_at: 1000, sources })] }),
    }), env, collector(client));
    assert.equal(response.status, 200, await response.text());
  }
  const all = await (await get(env, "/api/v1/memories?limit=20", reader())).json();
  assert.deepEqual(all.memories.sort((a, b) => a.origin.localeCompare(b.origin))
    .map((row) => [row.origin, row.source_count]), [[ORIGIN, 1], [OTHER_ORIGIN, 2]]);
  const first = await (await get(env, "/api/v1/memories?limit=1", reader())).json();
  assert.ok(first.next_cursor);
  const second = await (await get(env, "/api/v1/memories?limit=1&after=" + encodeURIComponent(first.next_cursor), reader())).json();
  const rows = [...first.memories, ...second.memories].sort((a, b) => a.origin.localeCompare(b.origin));
  assert.deepEqual(rows.map((row) => [row.origin, row.source_count]), [[ORIGIN, 1], [OTHER_ORIGIN, 2]]);
  assert.equal(second.next_cursor, null);
});

test("MCP SDK client discovers tools and enforces collector scope", async (t) => {
  const { Client, StreamableHTTPClientTransport } = await import("@modelcontextprotocol/client");
  const { env } = await runtime(t);
  await seed(env);
  const endpoint = new URL("https://memories.17166969.xyz/mcp");
  async function connect(identity) {
    const client = new Client({ name: "test-client", version: "1" });
    const transport = new StreamableHTTPClientTransport(endpoint, {
      fetch: async (url, init) => await worker.fetch(new Request(url, init), env, identity),
    });
    await client.connect(transport);
    t.after(() => client.close());
    return client;
  }
  const [first, second] = await Promise.all([connect(collector()), connect(collector(OTHER_COLLECTOR))]);
  const tools = await first.listTools();
  assert.deepEqual(tools.tools.map((item) => item.name).sort(), ["memory_inspect", "memory_search", "server_status"]);
  const [own, other] = await Promise.all([
    first.callTool({ name: "memory_search", arguments: {} }),
    second.callTool({ name: "memory_search", arguments: {} }),
  ]);
  assert.equal(JSON.parse(own.content[0].text).memories.length, 3);
  assert.equal(JSON.parse(other.content[0].text).memories.length, 0);
  const inspected = await first.callTool({ name: "memory_inspect", arguments: { origin: ORIGIN, record: "experience", id: "1" } });
  const detail = JSON.parse(inspected.content[0].text);
  assert.equal(detail.memory.origin, ORIGIN);
  assert.equal(detail.sources.length, 1);
  assert.equal(detail.links.length, 1);
  const denied = await second.callTool({ name: "memory_inspect", arguments: { origin: ORIGIN, record: "experience", id: "1" } });
  assert.equal(denied.isError, true);
  const invalid = await first.callTool({ name: "memory_search", arguments: { limit: "101" } });
  assert.equal(invalid.isError, true);
  const status = await first.callTool({ name: "server_status", arguments: {} });
  assert.equal(JSON.parse(status.content[0].text).origin, ORIGIN);
  assert.equal(JSON.parse(status.content[0].text).sync.learning_revision, 1);
  const otherStatus = await second.callTool({ name: "server_status", arguments: {} });
  assert.equal(JSON.parse(otherStatus.content[0].text).origin, OTHER_ORIGIN);
  assert.equal(JSON.parse(otherStatus.content[0].text).sync.learning_revision, null);
  const extra = await worker.fetch(request("/api/v1/sync/learning/deltas", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ protocol: 1, origin: OTHER_ORIGIN, base_revision: 0, digest: digest("mcp-extra"),
      changes: [experience(6, { observed_at: 600, sources: [source("codex:native-6@origin-b")] })] }),
  }), env, collector(OTHER_COLLECTOR));
  assert.equal(extra.status, 200);
  const readerClient = await connect(reader());
  const readerSearch = await readerClient.callTool({ name: "memory_search", arguments: {} });
  assert.equal(JSON.parse(readerSearch.content[0].text).memories.length, 4);
  assert.equal(JSON.parse((await readerClient.callTool({ name: "server_status", arguments: {} })).content[0].text).scope, "reader");
  const firstPage = JSON.parse((await first.callTool({ name: "memory_search", arguments: { limit: "1" } })).content[0].text);
  assert.equal(firstPage.memories.length, 1);
  assert.ok(firstPage.next_cursor);
  const secondPage = JSON.parse((await first.callTool({ name: "memory_search", arguments: { limit: "1", after: firstPage.next_cursor } })).content[0].text);
  assert.equal(secondPage.memories.length, 1);
  assert.notEqual(firstPage.memories[0].id, secondPage.memories[0].id);
});

test("MCP rejects unauthorized identities, hostile origins and oversized requests", async (t) => {
  const { env } = await runtime(t);
  const url = "https://memories.17166969.xyz/mcp";
  const init = { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" }, body: "{}" };
  assert.equal((await worker.fetch(new Request(url, init), env, {})).status, 403);
  assert.equal((await worker.fetch(new Request(url, { ...init, headers: { ...init.headers, Origin: "https://evil.example" } }), env, reader())).status, 403);
  assert.equal((await worker.fetch(new Request("https://evil.example/mcp", init), env, reader())).status, 403);
  const large = await worker.fetch(new Request(url, { ...init, body: "x".repeat(33000) }), env, reader());
  assert.equal(large.status, 413);
  assert.equal(large.headers.get("cache-control"), "no-store");
});

test("MCP schema, protocol and database failures do not expose internals", async (t) => {
  const { Client, StreamableHTTPClientTransport } = await import("@modelcontextprotocol/client");
  const { env } = await runtime(t);
  const url = "https://memories.17166969.xyz/mcp";
  const identity = reader();
  const post = async (body, headers = {}) => await worker.fetch(new Request(url, { method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...headers }, body,
  }), env, identity);
  const malformed = await post("{");
  assert.ok(malformed.status >= 400 || (await malformed.text()).includes("error"));
  const unsupported = await post(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }),
    { "MCP-Protocol-Version": "2099-01-01" });
  assert.ok(unsupported.status >= 400 || (await unsupported.text()).includes("error"));
  const unknown = await post(JSON.stringify({ jsonrpc: "2.0", id: 9, method: "unknown/method", params: {} }),
    { "MCP-Protocol-Version": "2025-11-25" });
  assert.match(await unknown.text(), /-32601/);
  const listed = await post(JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }),
    { "MCP-Protocol-Version": "2025-11-25" });
  assert.equal(listed.status, 200);
  assert.equal(listed.headers.get("cache-control"), "no-store");
  const client = new Client({ name: "test-client", version: "1" });
  await client.connect(new StreamableHTTPClientTransport(new URL(url), {
    fetch: async (target, init) => await worker.fetch(new Request(target, init), env, identity),
  }));
  t.after(() => client.close());
  const schema = await client.callTool({ name: "memory_search", arguments: { q: 123 } });
  assert.equal(schema.isError, true);
  const failingEnv = { ...env, DB: { prepare() { throw new Error("private database detail"); } } };
  const failing = new Client({ name: "failing-client", version: "1" });
  await failing.connect(new StreamableHTTPClientTransport(new URL(url), {
    fetch: async (target, init) => await worker.fetch(new Request(target, init), failingEnv, identity),
  }));
  t.after(() => failing.close());
  const outcome = await failing.callTool({ name: "memory_search", arguments: {} });
  assert.equal(outcome.isError, true);
  assert.doesNotMatch(JSON.stringify(outcome), /private database detail/);
});
