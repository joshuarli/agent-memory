import type { Principal } from "../auth.js";
import {
  insertRemoteObject,
  readKnownObjects,
  readObjectByCiphertext,
  readObjectByPlaintext,
  readOriginState,
  readStoreMeta,
  replaceRemoteObject,
  type OriginState,
  type RemoteObjectRow,
  type StoreMeta,
} from "../db.js";
import { json, methodNotAllowed } from "../errors.js";
import { reportError } from "../observe-channel.js";
import { readLearningState, type LearningState } from "../learning.js";
import { serverTiming } from "../observe.js";
import {
  MAX_CIPHERTEXT_BYTES,
  MAX_OBJECT_BYTES,
  PROTOCOL,
  decodeDigest,
  decodeHello,
  decodeMissingRequest,
  decodeObjectReplaceRequest,
} from "../protocol.js";
import type { Env } from "../index.js";
import {
  boundOrigin,
  contentLength,
  headers,
  readBounded,
  readJson,
  refuse,
  versionOrError,
  type SyncTiming,
} from "./shared.js";

const HELLO_PATH = "/api/v1/sync/hello";
const MISSING_PATH = "/api/v1/sync/objects/missing";
const OBJECT_PREFIX = "/api/v1/sync/objects/";
const REPLACE_SUFFIX = "/replace";
const ENCODINGS = new Set(["identity", "zlib"]);

function mapping(row: RemoteObjectRow): Record<string, unknown> {
  return {
    plaintext_digest: row.plaintext_digest,
    plaintext_size: row.plaintext_size,
    encoding: row.encoding,
    ciphertext_digest: row.ciphertext_digest,
    ciphertext_size: row.ciphertext_size,
  };
}

async function hello(request: Request, env: Env, principal: Principal, timing: SyncTiming): Promise<Response> {
  if (request.method !== "POST") return methodNotAllowed(["POST"], { "Server-Timing": serverTiming(timing) });
  const body = await readJson(request, timing);
  if (!body.ok) return body.response;
  const version = versionOrError(body.value, timing);
  if (version !== null) return version;
  const helloBody = decodeHello(body.value);
  if (helloBody === null) return refuse(400, "invalid_request", "invalid hello request", timing);
  const mismatch = boundOrigin(principal, helloBody.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  let store: StoreMeta | null;
  let state: OriginState;
  let learning: LearningState;
  try {
    store = await readStoreMeta(env.DB);
    state = await readOriginState(env.DB, helloBody.origin);
    learning = await readLearningState(env.DB, helloBody.origin);
  } finally {
    timing.d1 += performance.now() - started;
  }
  if (store === null) return refuse(503, "unavailable", "store is not provisioned", timing);
  return json(200, {
    protocol: PROTOCOL,
    store_id: store.id,
    origin: helloBody.origin,
    revision: state.revision,
    tombstone_watermark: state.tombstone_watermark,
    learning: {
      revision: learning.revision,
      digest: learning.digest,
      records: learning.records,
    },
  }, false, { "Server-Timing": serverTiming(timing) });
}

async function missing(request: Request, env: Env, principal: Principal, timing: SyncTiming): Promise<Response> {
  if (request.method !== "POST") return methodNotAllowed(["POST"], { "Server-Timing": serverTiming(timing) });
  const body = await readJson(request, timing);
  if (!body.ok) return body.response;
  const version = versionOrError(body.value, timing);
  if (version !== null) return version;
  const missingBody = decodeMissingRequest(body.value);
  if (missingBody === null) return refuse(400, "invalid_request", "invalid missing-objects request", timing);
  const mismatch = boundOrigin(principal, missingBody.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  let known: readonly RemoteObjectRow[];
  try {
    known = await readKnownObjects(env.DB, missingBody.origin, missingBody.objects.map((object) => object.plaintext_digest));
  } finally {
    timing.d1 += performance.now() - started;
  }
  const present = new Set(known.map((row) => row.plaintext_digest));
  return json(200, {
    protocol: PROTOCOL,
    origin: missingBody.origin,
    missing: missingBody.objects.filter((object) => !present.has(object.plaintext_digest)),
    known: known.map(mapping),
  }, false, { "Server-Timing": serverTiming(timing) });
}

function plaintextSize(request: Request): number | null {
  const value = request.headers.get("X-Plaintext-Size");
  if (value === null) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 && parsed <= MAX_OBJECT_BYTES ? parsed : null;
}

async function putObject(
  request: Request,
  env: Env,
  principal: Principal,
  ciphertext: string,
  timing: SyncTiming,
): Promise<Response> {
  const declared = contentLength(request);
  if (declared !== null && declared > MAX_CIPHERTEXT_BYTES) {
    return refuse(413, "payload_too_large", "object body exceeds the limit", timing);
  }
  const plaintext = decodeDigest(request.headers.get("X-Plaintext-Digest"));
  const size = plaintextSize(request);
  const encoding = request.headers.get("X-Encoding");
  if (plaintext === null || size === null || encoding === null || !ENCODINGS.has(encoding)) {
    return refuse(400, "invalid_request", "object headers are invalid", timing);
  }
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  const existing = await readObjectByCiphertext(env.DB, principal.origin_host, ciphertext);
  if (existing !== null) {
    if (existing.plaintext_digest !== plaintext || existing.plaintext_size !== size || existing.encoding !== encoding) {
      return refuse(409, "conflict", "ciphertext is already bound to another object", timing);
    }
    const head = await env.BUCKET.head(`v1/o/${ciphertext}`);
    if (head !== null) {
      return json(200, { stored: false, object: mapping(existing) }, false, {
        "Server-Timing": serverTiming(timing),
      });
    }
  }
  const body = await readBounded(request, MAX_CIPHERTEXT_BYTES);
  if (!body.ok) {
    return body.tooLarge
      ? refuse(413, "payload_too_large", "object body exceeds the limit", timing)
      : refuse(400, "invalid_request", "content length is invalid", timing);
  }
  if (body.bytes.byteLength === 0) {
    return refuse(400, "invalid_request", "request body is required", timing);
  }
  const received = body.bytes.byteLength;
  const started = performance.now();
  try {
    await env.BUCKET.put(`v1/o/${ciphertext}`, body.bytes, { sha256: ciphertext });
  } catch {
    timing.r2 += performance.now() - started;
    return refuse(400, "invalid_request", "ciphertext failed storage validation", timing);
  }
  timing.r2 += performance.now() - started;
  await insertRemoteObject(env.DB, {
    plaintext_digest: plaintext,
    plaintext_size: size,
    encoding,
    ciphertext_digest: ciphertext,
    ciphertext_size: received,
    origin_host: principal.origin_host,
    created_at: Date.now() / 1000,
  });
  const stored = await readObjectByPlaintext(env.DB, principal.origin_host, plaintext);
  if (stored === null) {
    return refuse(409, "conflict", "ciphertext is already bound to another plaintext", timing);
  }
  return json(200, {
    stored: stored.ciphertext_digest === ciphertext,
    object: mapping(stored),
  }, false, { "Server-Timing": serverTiming(timing) });
}

async function replaceObject(
  request: Request,
  env: Env,
  principal: Principal,
  plaintext: string,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "POST") return methodNotAllowed(["POST"], { "Server-Timing": serverTiming(timing) });
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  const body = await readJson(request, timing);
  if (!body.ok) return body.response;
  const version = versionOrError(body.value, timing);
  if (version !== null) return version;
  const replaceBody = decodeObjectReplaceRequest(body.value);
  if (replaceBody === null || replaceBody.plaintext_digest !== plaintext) {
    return refuse(400, "invalid_request", "invalid object replacement", timing);
  }
  const mismatch = boundOrigin(principal, replaceBody.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  try {
    const existing = await readObjectByPlaintext(env.DB, principal.origin_host, plaintext);
    if (existing === null) return refuse(404, "not_found", "object not found", timing);
    if (existing.ciphertext_digest === replaceBody.ciphertext_digest) {
      if (existing.plaintext_size !== replaceBody.plaintext_size
          || existing.encoding !== replaceBody.encoding
          || existing.ciphertext_size !== replaceBody.ciphertext_size) {
        return refuse(409, "conflict", "ciphertext is already bound to another object", timing);
      }
      return json(200, {
        protocol: PROTOCOL,
        origin: principal.origin_host,
        object: mapping(existing),
        replaced: false,
      }, false, { "Server-Timing": serverTiming(timing) });
    }
    const other = await readObjectByCiphertext(env.DB, principal.origin_host, replaceBody.ciphertext_digest);
    if (other !== null && other.plaintext_digest !== plaintext) {
      return refuse(409, "conflict", "ciphertext is bound to another plaintext", timing);
    }
    const r2Started = performance.now();
    let stored: R2Object | null;
    try {
      stored = await env.BUCKET.head(`v1/o/${replaceBody.ciphertext_digest}`);
    } finally {
      timing.r2 += performance.now() - r2Started;
    }
    if (stored === null) return refuse(409, "missing_object", "replacement ciphertext is not stored", timing);
    await replaceRemoteObject(env.DB, {
      plaintext_digest: plaintext,
      plaintext_size: replaceBody.plaintext_size,
      encoding: replaceBody.encoding,
      ciphertext_digest: replaceBody.ciphertext_digest,
      ciphertext_size: replaceBody.ciphertext_size,
      origin_host: principal.origin_host,
      created_at: Date.now() / 1000,
    }, existing.ciphertext_digest);
    const shared = await env.DB.prepare(
      "SELECT 1 FROM remote_object WHERE ciphertext_digest = ? LIMIT 1",
    ).bind(existing.ciphertext_digest).first();
    if (shared === null) {
      const retiredStarted = performance.now();
      try {
        await env.BUCKET.delete(`v1/o/${existing.ciphertext_digest}`);
      } finally {
        timing.r2 += performance.now() - retiredStarted;
      }
    }
    const replaced = await readObjectByPlaintext(env.DB, principal.origin_host, plaintext);
    if (replaced === null || replaced.ciphertext_digest !== replaceBody.ciphertext_digest) {
      return refuse(409, "conflict", "object replacement did not take", timing);
    }
    return json(200, {
      protocol: PROTOCOL,
      origin: principal.origin_host,
      object: mapping(replaced),
      replaced: true,
    }, false, { "Server-Timing": serverTiming(timing) });
  } catch (error) {
    const message = String(error);
    if (message.includes("UNIQUE constraint failed") || message.includes("FOREIGN KEY constraint failed")) {
      return refuse(409, "conflict", "object replacement conflicts with existing state", timing);
    }
    reportError("sync.replace", error);
    return refuse(503, "unavailable", "object replacement failed", timing);
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function readObject(
  env: Env,
  principal: Principal,
  ciphertext: string,
  timing: SyncTiming,
): Promise<Response> {
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  const started = performance.now();
  let row: RemoteObjectRow | null;
  try {
    row = await readObjectByCiphertext(env.DB, principal.origin_host, ciphertext);
  } finally {
    timing.d1 += performance.now() - started;
  }
  if (row === null) return refuse(404, "not_found", "object not found", timing);
  const stored = await env.BUCKET.get(`v1/o/${ciphertext}`);
  if (stored === null) return refuse(404, "not_found", "object not found", timing);
  return new Response(stored.body, {
    status: 200,
    headers: headers(timing, {
      "Content-Type": "application/octet-stream",
      "Content-Length": String(row.ciphertext_size),
      "X-Plaintext-Digest": row.plaintext_digest,
      "X-Plaintext-Size": String(row.plaintext_size),
      "X-Encoding": row.encoding,
    }),
  });
}

export async function handleSync(
  request: Request,
  env: Env,
  principal: Principal,
  pathname: string,
  timing: SyncTiming,
): Promise<Response | null> {
  if (pathname === HELLO_PATH) {
    if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
    return await hello(request, env, principal, timing);
  }
  if (pathname === MISSING_PATH) {
    if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
    return await missing(request, env, principal, timing);
  }
  if (!pathname.startsWith(OBJECT_PREFIX)) return null;
  const rest = pathname.slice(OBJECT_PREFIX.length);
  if (rest.endsWith(REPLACE_SUFFIX)) {
    const plaintext = decodeDigest(rest.slice(0, -REPLACE_SUFFIX.length));
    if (plaintext === null) return refuse(404, "not_found", "route not found", timing);
    if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
    return await replaceObject(request, env, principal, plaintext, timing);
  }
  const ciphertext = decodeDigest(rest);
  if (ciphertext === null) return refuse(404, "not_found", "route not found", timing);
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  if (request.method === "PUT") return await putObject(request, env, principal, ciphertext, timing);
  if (request.method === "GET") return await readObject(env, principal, ciphertext, timing);
  return methodNotAllowed(["GET", "PUT"], { "Server-Timing": serverTiming(timing) });
}
