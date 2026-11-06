import type { Principal } from "../auth.js";
import {
  commitGeneration,
  readGenerationByManifest,
  readGenerations,
  readObjectByCiphertext,
  readObjectSizes,
  readTombstone,
  type GenerationRow,
  type NewGeneration,
} from "../db.js";
import { json, methodNotAllowed } from "../errors.js";
import { reportError } from "../observe-channel.js";
import { serverTiming } from "../observe.js";
import {
  MAX_PAGE,
  PROTOCOL,
  decodeDigest,
  decodeGenerationCommit,
  type GenerationManifest,
} from "../protocol.js";
import type { Env } from "../index.js";
import {
  boundOrigin,
  contentLength,
  headers,
  readJson,
  refuse,
  versionOrError,
  type SyncTiming,
} from "./shared.js";

const GENERATIONS_PATH = "/api/v1/sync/generations";
const MANIFESTS_PREFIX = "/api/v1/sync/manifests/";

function pageParameter(value: string | null): number | null {
  if (value === null) return 100;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 1 && parsed <= MAX_PAGE ? parsed : null;
}

function afterParameter(value: string | null): number | null {
  if (value === null) return 0;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

function generationJson(row: GenerationRow): Record<string, unknown> {
  return {
    id: row.id,
    origin_host: row.origin_host,
    harness: row.harness,
    native_id: row.native_id,
    manifest_digest: row.manifest_digest,
    source_digest: row.source_digest,
    source_size: row.source_size,
    captured_at: row.captured_at,
    committed_at: row.committed_at,
    revision: row.revision,
    manifest_object: row.manifest_object,
  };
}

function objectSizes(manifest: GenerationManifest): Map<string, number> | null {
  const sizes = new Map<string, number>();
  for (const object of [...manifest.chunks, ...manifest.artifacts]) {
    const known = sizes.get(object.digest);
    if (known !== undefined && known !== object.size) return null;
    sizes.set(object.digest, object.size);
  }
  return sizes;
}

function commitFailure(error: unknown, timing: SyncTiming): Response {
  const message = String(error);
  if (message.includes("stale_revision")) {
    return refuse(409, "stale_revision", "base revision is stale", timing);
  }
  if (message.includes("tombstoned")) {
    return refuse(410, "tombstoned", "session is tombstoned", timing);
  }
  reportError("generations.commit", error);
  return refuse(503, "unavailable", "generation commit failed", timing);
}

async function commit(
  request: Request,
  env: Env,
  principal: Principal,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "POST") return methodNotAllowed(["POST"], { "Server-Timing": serverTiming(timing) });
  const body = await readJson(request, timing);
  if (!body.ok) return body.response;
  const version = versionOrError(body.value, timing);
  if (version !== null) return version;
  const commitBody = await decodeGenerationCommit(body.value);
  if (commitBody === null) return refuse(400, "invalid_request", "invalid generation manifest", timing);
  const mismatch = boundOrigin(principal, commitBody.origin, timing);
  if (mismatch !== null) return mismatch;
  const { manifest } = commitBody;

  const started = performance.now();
  try {
    const tombstone = await readTombstone(env.DB, commitBody.origin, manifest.harness, manifest.native_id);
    if (tombstone !== null) {
      return refuse(410, "tombstoned", "session is tombstoned", timing);
    }
    const duplicate = await readGenerationByManifest(env.DB, commitBody.origin, manifest.manifest_digest);
    if (duplicate !== null) {
      return json(200, {
        protocol: PROTOCOL,
        origin: commitBody.origin,
        generation: duplicate.id,
        manifest_digest: duplicate.manifest_digest,
        revision: duplicate.revision,
        duplicate: true,
      }, false, { "Server-Timing": serverTiming(timing) });
    }
    const sizes = objectSizes(manifest);
    if (sizes === null) return refuse(400, "invalid_request", "object sizes conflict", timing);
    const digests = [...sizes.keys()];
    const recorded = await readObjectSizes(env.DB, commitBody.origin, digests);
    for (const [digest, size] of sizes) {
      if (recorded.get(digest) !== size) {
        return refuse(409, "missing_object", "manifest references an object that is not stored", timing);
      }
    }
    const manifestObject = await readObjectByCiphertext(env.DB, commitBody.origin, manifest.manifest_object);
    if (manifestObject === null) {
      return refuse(409, "missing_object", "manifest object is not stored", timing);
    }
    const committedAt = Date.now() / 1000;
    const generation: NewGeneration = {
      origin_host: commitBody.origin,
      harness: manifest.harness,
      native_id: manifest.native_id,
      manifest_digest: manifest.manifest_digest,
      source_digest: manifest.source_digest,
      source_size: manifest.source_size,
      captured_at: manifest.captured_at,
      committed_at: committedAt,
      manifest_object: manifest.manifest_object,
      objects: [
        ...manifest.chunks.map((chunk) => ({ digest: chunk.digest, size: chunk.size })),
        ...manifest.artifacts.map((artifact) => ({ digest: artifact.digest, size: artifact.size })),
      ],
    };
    const result = await commitGeneration(env.DB, generation, commitBody.base_revision);
    return json(200, {
      protocol: PROTOCOL,
      origin: commitBody.origin,
      generation: result.id,
      manifest_digest: manifest.manifest_digest,
      revision: result.revision,
      duplicate: false,
    }, false, { "Server-Timing": serverTiming(timing) });
  } catch (error) {
    const message = String(error);
    if (message.includes("UNIQUE constraint failed") || message.includes("stale_revision")) {
      const duplicate = await readGenerationByManifest(env.DB, commitBody.origin, manifest.manifest_digest);
      if (duplicate !== null) {
        return json(200, {
          protocol: PROTOCOL,
          origin: commitBody.origin,
          generation: duplicate.id,
          manifest_digest: duplicate.manifest_digest,
          revision: duplicate.revision,
          duplicate: true,
        }, false, { "Server-Timing": serverTiming(timing) });
      }
    }
    return commitFailure(error, timing);
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function list(
  request: Request,
  env: Env,
  principal: Principal,
  url: URL,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "GET") return methodNotAllowed(["GET"], { "Server-Timing": serverTiming(timing) });
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  const origin = url.searchParams.get("origin");
  if (origin === null) return refuse(400, "invalid_request", "origin is required", timing);
  const mismatch = boundOrigin(principal, origin, timing);
  if (mismatch !== null) return mismatch;
  const manifest = url.searchParams.get("manifest");
  const started = performance.now();
  try {
    if (manifest !== null) {
      const digest = decodeDigest(manifest);
      if (digest === null) return refuse(400, "invalid_request", "manifest digest is invalid", timing);
      const row = await readGenerationByManifest(env.DB, origin, digest);
      if (row !== null) {
        const hidden = await readTombstone(env.DB, origin, row.harness, row.native_id);
        if (hidden !== null) {
          return json(200, {
            protocol: PROTOCOL,
            origin,
            generations: [],
            next_cursor: null,
          }, false, { "Server-Timing": serverTiming(timing) });
        }
      }
      return json(200, {
        protocol: PROTOCOL,
        origin,
        generations: row === null ? [] : [generationJson(row)],
        next_cursor: null,
      }, false, { "Server-Timing": serverTiming(timing) });
    }
    const after = afterParameter(url.searchParams.get("after"));
    const limit = pageParameter(url.searchParams.get("limit"));
    if (after === null || limit === null) {
      return refuse(400, "invalid_request", "after or limit is invalid", timing);
    }
    const rows = await readGenerations(env.DB, origin, after, limit + 1);
    const page = rows.slice(0, limit);
    const last = page.at(-1);
    return json(200, {
      protocol: PROTOCOL,
      origin,
      generations: page.map(generationJson),
      next_cursor: rows.length > limit && last !== undefined ? String(last.id) : null,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function manifest(
  request: Request,
  env: Env,
  principal: Principal,
  ciphertext: string,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "GET") return methodNotAllowed(["GET"], { "Server-Timing": serverTiming(timing) });
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  const started = performance.now();
  let row;
  try {
    row = await readObjectByCiphertext(env.DB, principal.origin_host, ciphertext);
  } finally {
    timing.d1 += performance.now() - started;
  }
  if (row === null) return refuse(404, "not_found", "manifest not found", timing);
  const r2Started = performance.now();
  try {
    const stored = await env.BUCKET.get(`v1/o/${ciphertext}`);
    if (stored === null) return refuse(404, "not_found", "manifest not found", timing);
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
  } finally {
    timing.r2 += performance.now() - r2Started;
  }
}

export async function handleGenerations(
  request: Request,
  env: Env,
  principal: Principal,
  pathname: string,
  url: URL,
  timing: SyncTiming,
): Promise<Response | null> {
  if (pathname === GENERATIONS_PATH) {
    if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
    if (request.method === "POST") return await commit(request, env, principal, timing);
    if (request.method === "GET") return await list(request, env, principal, url, timing);
    return methodNotAllowed(["GET", "POST"], { "Server-Timing": serverTiming(timing) });
  }
  if (!pathname.startsWith(MANIFESTS_PREFIX)) return null;
  const ciphertext = decodeDigest(pathname.slice(MANIFESTS_PREFIX.length));
  if (ciphertext === null) return refuse(404, "not_found", "route not found", timing);
  const declared = contentLength(request);
  if (declared !== null && declared > 0) return refuse(400, "invalid_request", "manifest reads take no body", timing);
  return await manifest(request, env, principal, ciphertext, timing);
}
