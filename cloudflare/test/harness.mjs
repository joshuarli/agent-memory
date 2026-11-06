import { existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";

export const BUNDLE_PATH = fileURLToPath(
  new URL("../.cloudflare/output/v0/workers/default/bundle/index.js", import.meta.url),
);

export async function loadWorker() {
  if (!existsSync(BUNDLE_PATH)) {
    throw new Error("missing worker bundle; run `npm run build` in agent-memory/cloudflare");
  }
  const module = await import(pathToFileURL(BUNDLE_PATH).href);
  return module.default;
}

function fakeD1(storeMeta, failure, collectors) {
  const calls = { prepare: 0, first: 0, batch: 0, exec: 0, dump: 0, withSession: 0 };
  function statement(query) {
    let bound;
    return {
      first: async () => {
        calls.first += 1;
        if (failure === "query") throw new Error("d1 query failed");
        if (query.includes("FROM collector")) {
          const row = collectors[bound];
          return row === undefined || row.disabled ? null : { origin_host: row.origin_host };
        }
        if (query.includes("FROM store_meta")) return storeMeta;
        return null;
      },
      all: async () => ({ results: [] }),
      run: async () => ({ success: true, meta: {} }),
      bind(value) {
        bound = value;
        return this;
      },
    };
  }
  return {
    calls,
    binding: {
      prepare(query) {
        calls.prepare += 1;
        if (failure === "prepare") throw new Error("d1 prepare failed");
        return statement(query);
      },
      async batch() {
        calls.batch += 1;
        return [];
      },
      async exec() {
        calls.exec += 1;
        return { count: 0, duration: 0 };
      },
      async dump() {
        calls.dump += 1;
        return new ArrayBuffer(0);
      },
      withSession() {
        calls.withSession += 1;
        throw new Error("unexpected D1 session");
      },
    },
  };
}

function countingR2() {
  const calls = { head: 0, get: 0, put: 0, delete: 0, list: 0, createMultipartUpload: 0 };
  const binding = {};
  for (const method of Object.keys(calls)) {
    binding[method] = async () => {
      calls[method] += 1;
      throw new Error(`BUCKET.${method} is not expected in this test`);
    };
  }
  return { binding, calls };
}

export function environment(overrides = {}, options = {}) {
  const storeMeta = options.storeMeta === undefined ? { id: "usr-memories", created_at: 0 } : options.storeMeta;
  const db = fakeD1(storeMeta, options.dbFailure, options.collectors ?? {});
  const bucket = countingR2();
  return {
    env: {
      READERS: "reader@example.test",
      ACCESS_TEAM_DOMAIN: "team.example.test",
      ACCESS_AUD: "test-audience",
      DB: db.binding,
      BUCKET: bucket.binding,
      ...overrides,
    },
    calls: { db: db.calls, bucket: bucket.calls },
  };
}

export function context(identity) {
  return { access: { getIdentity: () => identity } };
}

export function request(path, init = {}) {
  return new Request(`https://memories.test${path}`, init);
}
