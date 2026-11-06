import { createServer } from "node:http";
import { existsSync } from "node:fs";

const BUNDLE = new URL("../.cloudflare/output/v0/workers/default/bundle/index.js", import.meta.url);
const READER = "bench-reader@example.test";

function fail(message) {
  console.error(message);
  process.exit(2);
}

if (!existsSync(BUNDLE)) fail("missing worker bundle; run `npm run build` in agent-memory/cloudflare");

let Miniflare;
try {
  ({ Miniflare } = await import("miniflare"));
} catch {
  fail("worker bench dependencies are missing; run `npm ci` in agent-memory/cloudflare");
}

const { seedDatabase } = await import("../bench/seed.mjs");

const miniflare = new Miniflare({
  modules: true,
  script: "export default { fetch() { return new Response('worker bench placeholder'); } };",
  compatibilityDate: "2026-08-06",
  d1Databases: ["DB"],
  r2Buckets: ["BUCKET"],
});

const counters = new Map();
let requests = 0;

function bump(key) {
  counters.set(key, (counters.get(key) ?? 0) + 1);
}

function countedStatement(statement) {
  return new Proxy(statement, {
    get(target, property) {
      const value = Reflect.get(target, property);
      if (typeof value !== "function") return value;
      return (...args) => {
        bump("d1." + String(property));
        const result = value.apply(target, args);
        return property === "bind" ? countedStatement(result) : result;
      };
    },
  });
}

function countedBinding(binding, label) {
  return new Proxy(binding, {
    get(target, property) {
      const value = Reflect.get(target, property);
      if (typeof value !== "function") return value;
      return (...args) => {
        bump(label + "." + String(property));
        const result = value.apply(target, args);
        return label === "d1" && result !== null && typeof result === "object" && typeof result.first === "function"
          ? countedStatement(result)
          : result;
      };
    },
  });
}

const db = await miniflare.getD1Database("DB");
await seedDatabase(db);
const env = {
  READERS: READER,
  DB: countedBinding(db, "d1"),
  BUCKET: countedBinding(await miniflare.getR2Bucket("BUCKET"), "r2"),
};

const { default: worker } = await import(BUNDLE.href);

function identityFor(header) {
  return header === "reader" ? { email: READER } : null;
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks);
}

function send(response, status, body) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  response.end(JSON.stringify(body));
}

const PROJECT_KEYS = Array.from({ length: 100 }, (_, index) => index.toString(16).padStart(16, "0"));

async function seedLearning(offset, count, total) {
  const digest = "a".repeat(64);
  const state = db.prepare(
    "INSERT INTO learning_state(origin_host, revision, epoch, digest, records, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
    + "ON CONFLICT(origin_host) DO UPDATE SET records = excluded.records",
  );
  await state.bind("origin-a", 1, 1, null, total, 0).run();
  const EXPERIENCE_ROWS = 4;
  const SOURCE_ROWS = 12;
  const SEARCH_ROWS = 19;
  for (let start = 0; start < count; start += SEARCH_ROWS) {
    const statements = [];
    const experiences = [];
    for (let id = offset + start + 1; id <= offset + Math.min(start + SEARCH_ROWS, count); id += 1) {
      experiences.push({
        id,
        project: PROJECT_KEYS[id % PROJECT_KEYS.length],
        claim: `${id % 100 === 0 ? "retentionmarker " : ""}synthetic projection record ${id}.`,
        applies: `Synthetic applicability for projection record ${id}.`,
        observed: 1_700_000_000 - id,
      });
    }
    for (let offset = 0; offset < experiences.length; offset += EXPERIENCE_ROWS) {
      const chunk = experiences.slice(offset, offset + EXPERIENCE_ROWS);
      const values = chunk.map(() => "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)").join(",");
      const params = [];
      for (const record of chunk) {
        params.push(
          "origin-a", 1, record.id, record.project, "bench", "method", record.claim, record.applies,
          `token-${record.id}`, `fingerprint-${record.id}`, "deterministic", "synthetic",
          null, null, record.observed, record.observed, "[]", "", "retained", null, null, 1, 0,
        );
      }
      statements.push(db.prepare(
        `INSERT INTO learning_experience(origin_host, epoch, remote_id, project_key, project_label, kind, claim,
           applies_when, identifiers, fingerprint, basis, detector, revision, worktree, observed_at, captured_at,
           validity, tags, state, state_reason, state_at, sync_revision, pending) VALUES ${values}`,
      ).bind(...params));
    }
    for (let offset = 0; offset < experiences.length; offset += SOURCE_ROWS) {
      const chunk = experiences.slice(offset, offset + SOURCE_ROWS);
      const values = chunk.map(() => "(?,?,?,?,?,?,?,?)").join(",");
      const params = [];
      for (const record of chunk) {
        params.push("origin-a", 1, "experience", record.id, `codex:bench-${record.id}@origin-a`, 0, digest, "trigger");
      }
      statements.push(db.prepare(
        "INSERT INTO learning_source(origin_host, epoch, record, remote_id, session, position, digest, role) "
        + `VALUES ${values}`,
      ).bind(...params));
    }
    const searchValues = experiences.map(() => "(?,?,?,?,?)").join(",");
    const searchParams = [];
    for (const record of experiences) {
      searchParams.push("origin-a", 1, "experience", record.id, `${record.claim}
${record.applies}`);
    }
    statements.push(db.prepare(
      "INSERT INTO learning_search(origin_host, epoch, record, remote_id, text) "
      + `VALUES ${searchValues}`,
    ).bind(...searchParams));
    await db.batch(statements);
  }
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname === "/__bench/status") {
      send(response, 200, { requests, counters: Object.fromEntries(counters) });
      return;
    }
    if (url.pathname === "/__bench/reset") {
      counters.clear();
      requests = 0;
      send(response, 200, { requests, counters: {} });
      return;
    }
    if (url.pathname === "/__bench/seed-learning") {
      const payload = JSON.parse((await readBody(request)).toString("utf8") || "{}");
      const count = Number(payload.records);
      const offset = Number(payload.offset ?? 0);
      const total = Number(payload.total ?? count);
      if (!Number.isSafeInteger(count) || count < 1 || !Number.isSafeInteger(offset) || offset < 0
          || !Number.isSafeInteger(total) || total < offset + count) {
        send(response, 400, { error: "records is invalid" });
        return;
      }
      await seedLearning(offset, count, total);
      send(response, 200, { records: count, offset, total });
      return;
    }
    const body = await readBody(request);
    const method = request.method ?? "GET";
    const forwarded = new Request(`http://127.0.0.1${request.url}`, {
      method,
      headers: request.headers,
      body: method === "GET" || method === "HEAD" || body.length === 0 ? undefined : body,
    });
    requests += 1;
    const result = await worker.fetch(forwarded, env, {
      access: { getIdentity: async () => identityFor(request.headers["x-bench-principal"]) },
    });
    response.writeHead(result.status, Object.fromEntries(result.headers));
    response.end(Buffer.from(await result.arrayBuffer()));
  } catch (error) {
    send(response, 500, { error: String(error) });
  }
});

server.listen(0, "127.0.0.1", () => {
  console.log(JSON.stringify({ port: server.address().port }));
});

async function shutdown() {
  server.close();
  await miniflare.dispose();
  process.exit(0);
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, shutdown);
}
