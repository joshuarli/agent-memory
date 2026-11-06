import type { Principal } from "../auth.js";
import {
  pushTombstones,
  readOriginState,
  readTombstoneKeys,
  readTombstones,
  type NewTombstone,
  type TombstoneRow,
} from "../db.js";
import { json, methodNotAllowed } from "../errors.js";
import { serverTiming } from "../observe.js";
import {
  MAX_PAGE,
  PROTOCOL,
  decodeCursor,
  decodeTombstoneRequest,
  encodeCursor,
} from "../protocol.js";
import type { Env } from "../index.js";
import {
  boundOrigin,
  readJson,
  refuse,
  versionOrError,
  type SyncTiming,
} from "./shared.js";

const TOMBSTONES_PATH = "/api/v1/sync/tombstones";

function key(harness: string, native: string): string {
  return `${harness}:${native}`;
}

function limitParameter(value: string | null): number | null {
  if (value === null) return 100;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 1 && parsed <= MAX_PAGE ? parsed : null;
}

function cursorParameter(value: string | null): { readonly revision: number; readonly harness: string; readonly native_id: string } | null | undefined {
  if (value === null) return null;
  const parts = decodeCursor(value);
  if (parts === null || parts.length !== 3) return undefined;
  const [revisionPart, harness, native] = parts;
  if (revisionPart === undefined || harness === undefined || native === undefined) return undefined;
  if (!/^(0|[1-9][0-9]{0,15})$/.test(revisionPart)) return undefined;
  return { revision: Number(revisionPart), harness, native_id: native };
}

function tombstoneJson(row: TombstoneRow): Record<string, unknown> {
  return {
    harness: row.harness,
    native_id: row.native_id,
    scope: row.scope,
    forgotten_at: row.forgotten_at,
    revision: row.revision,
  };
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
  const after = cursorParameter(url.searchParams.get("after"));
  const limit = limitParameter(url.searchParams.get("limit"));
  if (after === undefined || limit === null) {
    return refuse(400, "invalid_request", "after or limit is invalid", timing);
  }
  const started = performance.now();
  try {
    const rows = await readTombstones(env.DB, origin, after, limit + 1);
    const page = rows.slice(0, limit);
    const last = page.at(-1);
    return json(200, {
      protocol: PROTOCOL,
      origin,
      tombstones: page.map(tombstoneJson),
      next_cursor: rows.length > limit && last !== undefined
        ? encodeCursor([String(last.revision), last.harness, last.native_id])
        : null,
      watermark: page.reduce((highest, row) => Math.max(highest, row.revision), 0),
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function push(
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
  const pushBody = decodeTombstoneRequest(body.value);
  if (pushBody === null) return refuse(400, "invalid_request", "invalid tombstone request", timing);
  const mismatch = boundOrigin(principal, pushBody.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  try {
    const existing = await readTombstoneKeys(env.DB, pushBody.origin, pushBody.tombstones);
    const applied: NewTombstone[] = [];
    const ignored: { readonly harness: string; readonly native_id: string }[] = [];
    for (const tombstone of pushBody.tombstones) {
      const row = existing.get(key(tombstone.harness, tombstone.native_id));
      if (row === undefined || tombstone.forgotten_at > row.forgotten_at) {
        applied.push(tombstone);
      } else {
        ignored.push({ harness: tombstone.harness, native_id: tombstone.native_id });
      }
    }
    if (applied.length === 0) {
      const state = await readOriginState(env.DB, pushBody.origin);
      return json(200, {
        protocol: PROTOCOL,
        origin: pushBody.origin,
        revision: state.revision,
        applied: [],
        ignored,
      }, false, { "Server-Timing": serverTiming(timing) });
    }
    let revision: number;
    try {
      revision = await pushTombstones(env.DB, pushBody.origin, applied, pushBody.base_revision);
    } catch (error) {
      if (String(error).includes("stale_revision")) {
        return refuse(409, "stale_revision", "base revision is stale", timing);
      }
      throw error;
    }
    return json(200, {
      protocol: PROTOCOL,
      origin: pushBody.origin,
      revision,
      applied: applied.map((tombstone) => ({
        harness: tombstone.harness,
        native_id: tombstone.native_id,
      })),
      ignored,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

export async function handleTombstones(
  request: Request,
  env: Env,
  principal: Principal,
  pathname: string,
  url: URL,
  timing: SyncTiming,
): Promise<Response | null> {
  if (pathname !== TOMBSTONES_PATH) return null;
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  if (request.method === "GET") return await list(request, env, principal, url, timing);
  if (request.method === "POST") return await push(request, env, principal, timing);
  return methodNotAllowed(["GET", "POST"], { "Server-Timing": serverTiming(timing) });
}
