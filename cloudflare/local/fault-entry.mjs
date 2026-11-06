import worker from "./index.js";

const counts = new Map();
const seen = { d1Batch: 0, r2Put: 0, r2Get: 0 };
const rows = { read: 0, written: 0, statements: 0 };
const r2 = { put: 0, get: 0, head: 0, list: 0, multipart: 0, delete: 0, bytesIn: 0, bytesOut: 0 };
let requests = 0;

const addMeta = (meta) => { if (!meta) return; rows.read += meta.rows_read ?? 0; rows.written += meta.rows_written ?? 0; };

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
  return limit > 0 && count === limit;
}

function countedStatement(statement) {
  return new Proxy(statement, {
    get(target, property) {
      const value = Reflect.get(target, property);
      if (typeof value !== "function") return value;
      return (...args) => {
        bump("d1." + String(property));
        const result = value.apply(target, args);
        if (property === "bind") return countedStatement(result);
        if (property === "first") rows.statements += 1;
        if (property !== "run" && property !== "all") return result;
        return result.then((resolved) => { rows.statements += 1; addMeta(resolved.meta); return resolved; });
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
        if (label === "d1" && property === "batch") return result.then((results) => {
          rows.statements += args[0].length;
          results.forEach((item) => addMeta(item.meta));
          return results;
        });
        if (label === "r2") {
          const method = String(property);
          if (method === "put") { r2.put += 1; r2.bytesIn += args[1]?.byteLength ?? 0; }
          else if (method === "get") return result.then((object) => { r2.get += 1; r2.bytesOut += object?.size ?? 0; return object; });
          else if (method === "head") r2.head += 1;
          else if (method === "list") r2.list += 1;
          else if (method === "createMultipartUpload") r2.multipart += 1;
          else if (method === "delete") r2.delete += 1;
        }
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

function reply(value, status = 200) {
  return Response.json(value, { status, headers: { "cache-control": "no-store" } });
}

export default {
  async fetch(request, env, context) {
    const url = new URL(request.url);
    if (url.pathname === "/__bench/status") return reply({ requests, counters: Object.fromEntries(counts), rows, r2 });
    if (url.pathname === "/__bench/reset") {
      counts.clear(); requests = 0; rows.read = 0; rows.written = 0; rows.statements = 0;
      for (const key of Object.keys(r2)) r2[key] = 0;
      return reply({ requests, counters: {}, rows, r2 });
    }
    requests += 1;
    const plan = limits(env);
    const headers = new Headers(request.headers);
    headers.set("Host", url.host);
    const wrapped = { ...env, DB: counted(env.DB, "d1", d1Faults(plan)), BUCKET: counted(env.BUCKET, "r2", r2Faults(plan)) };
    return await worker.fetch(new Request(request, { headers }), wrapped, context);
  },
};
