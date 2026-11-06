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
    requests += 1;
    const plan = limits(env);
    const headers = new Headers(request.headers);
    headers.set("Host", url.host);
    const wrapped = { ...env, DB: counted(env.DB, "d1", d1Faults(plan)), BUCKET: counted(env.BUCKET, "r2", r2Faults(plan)) };
    return await worker.fetch(new Request(request, { headers }), wrapped, context);
  },
};
