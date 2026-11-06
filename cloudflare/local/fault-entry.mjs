import worker from "./index.js";

const counts = new Map();
const seen = { d1Batch: 0, r2Put: 0, r2Get: 0 };
let requests = 0;

function bump(key) {
  counts.set(key, (counts.get(key) ?? 0) + 1);
}

function limits(env) {
  try {
    const value = JSON.parse(env.LOCAL_FAULTS ?? "{}");
    return {
      d1Batch: Number(value.d1_batch_every ?? 0),
      r2Put: Number(value.r2_put_every ?? 0),
      r2Get: Number(value.r2_get_every ?? 0),
    };
  } catch {
    return { d1Batch: 0, r2Put: 0, r2Get: 0 };
  }
}

function hit(limit, count) {
  return limit > 0 && count % limit === 0;
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

function counted(binding, label, hooks = {}) {
  return new Proxy(binding, {
    get(target, property) {
      const value = Reflect.get(target, property);
      if (typeof value !== "function") return value;
      return (...args) => {
        bump(`${label}.${String(property)}`);
        const replacement = hooks[property]?.(args);
        if (replacement !== undefined) return replacement;
        const result = value.apply(target, args);
        return property === "prepare" ? countedStatement(result) : result;
      };
    },
  });
}

function d1Faults(plan) {
  return {
    batch: () => {
      seen.d1Batch += 1;
      if (hit(plan.d1Batch, seen.d1Batch)) throw new Error("synthetic D1 batch fault");
    },
  };
}

function r2Faults(plan) {
  return {
    put: () => {
      seen.r2Put += 1;
      if (hit(plan.r2Put, seen.r2Put)) throw new Error("synthetic R2 put fault");
    },
    get: () => {
      seen.r2Get += 1;
      if (hit(plan.r2Get, seen.r2Get)) return null;
    },
  };
}

const PROJECT_KEYS = Array.from({ length: 100 }, (_value, index) => index.toString(16).padStart(16, "0"));
const DIGEST = "a".repeat(64);

function record(id) {
  return {
    id,
    project: PROJECT_KEYS[id % 100],
    claim: `${id % 100 === 0 ? "retentionmarker " : ""}synthetic projection record ${id}.`,
    applies: `Synthetic applicability for projection record ${id}.`,
    observed: 1_700_000_000 - id,
  };
}

async function seedLearning(db, offset, count, total) {
  await db.prepare(
    "INSERT INTO learning_state(origin_host, revision, epoch, digest, records, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
    + "ON CONFLICT(origin_host) DO UPDATE SET records = excluded.records",
  ).bind("origin-a", 1, 1, null, total, 0).run();
  for (let start = 0; start < count; start += 19) {
    const records = Array.from({ length: Math.min(19, count - start) }, (_value, index) => record(offset + start + index + 1));
    const statements = [];
    for (let index = 0; index < records.length; index += 4) {
      const chunk = records.slice(index, index + 4);
      const params = chunk.flatMap((item) => [
        "origin-a", 1, item.id, item.project, "bench", "method", item.claim, item.applies,
        `token-${item.id}`, `fingerprint-${item.id}`, "deterministic", "synthetic",
        null, null, item.observed, item.observed, "[]", "", "retained", null, null, 1, 0,
      ]);
      statements.push(db.prepare(
        "INSERT INTO learning_experience(origin_host, epoch, remote_id, project_key, project_label, kind, claim, "
        + "applies_when, identifiers, fingerprint, basis, detector, revision, worktree, observed_at, captured_at, "
        + "validity, tags, state, state_reason, state_at, sync_revision, pending) VALUES "
        + chunk.map(() => "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)").join(","),
      ).bind(...params));
    }
    for (let index = 0; index < records.length; index += 12) {
      const chunk = records.slice(index, index + 12);
      const params = chunk.flatMap((item) => [
        "origin-a", 1, "experience", item.id, `codex:bench-${item.id}@origin-a`, 0, DIGEST, "trigger",
      ]);
      statements.push(db.prepare(
        "INSERT INTO learning_source(origin_host, epoch, record, remote_id, session, position, digest, role) "
        + `VALUES ${chunk.map(() => "(?,?,?,?,?,?,?,?)").join(",")}`,
      ).bind(...params));
    }
    const searchParams = records.flatMap((item) => [
      "origin-a", 1, "experience", item.id, `${item.claim}\n${item.applies}`,
    ]);
    statements.push(db.prepare(
      "INSERT INTO learning_search(origin_host, epoch, record, remote_id, text) "
      + `VALUES ${records.map(() => "(?,?,?,?,?)").join(",")}`,
    ).bind(...searchParams));
    await db.batch(statements);
  }
}

function reply(value, status = 200) {
  return Response.json(value, { status, headers: { "cache-control": "no-store" } });
}

export default {
  async fetch(request, env, context) {
    const url = new URL(request.url);
    if (url.pathname === "/__bench/status") {
      return reply({ requests, counters: Object.fromEntries(counts) });
    }
    if (url.pathname === "/__bench/reset") {
      counts.clear();
      requests = 0;
      return reply({ requests, counters: {} });
    }
    if (url.pathname === "/__bench/seed-learning") {
      const payload = await request.json();
      const count = Number(payload.records);
      const offset = Number(payload.offset ?? 0);
      const total = Number(payload.total ?? count);
      if (!Number.isSafeInteger(count) || count < 1 || !Number.isSafeInteger(offset) || offset < 0
          || !Number.isSafeInteger(total) || total < offset + count) {
        return reply({ error: "records is invalid" }, 400);
      }
      await seedLearning(env.DB, offset, count, total);
      return reply({ records: count, offset, total });
    }
    requests += 1;
    const plan = limits(env);
    const headers = new Headers(request.headers);
    headers.set("Host", url.host);
    const wrapped = { ...env, DB: counted(env.DB, "d1", d1Faults(plan)), BUCKET: counted(env.BUCKET, "r2", r2Faults(plan)) };
    return await worker.fetch(new Request(request, { headers }), wrapped, context);
  },
};
