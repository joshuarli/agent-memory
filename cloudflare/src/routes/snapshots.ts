import type { Principal } from "../auth.js";
import {
  clearObjectsGcPending,
  commitSnapshot,
  deleteObjectIfGcEligible,
  deleteSnapshots,
  markObjectsGcPending,
  markSnapshotVerified,
  readKnownObjects,
  readSnapshot,
  readSnapshotChunks,
  readSnapshots,
  readUnreferencedObjects,
  type RemoteObjectRow,
  type SnapshotChunkRow,
  type SnapshotRow,
} from "../db.js";
import { json, methodNotAllowed } from "../errors.js";
import { reportError } from "../observe-channel.js";
import { serverTiming } from "../observe.js";
import {
  GC_GRACE_SECONDS,
  MAX_PAGE,
  PROTOCOL,
  decodeCursor,
  decodeDigest,
  decodeGcRequest,
  decodeSnapshotCommit,
  decodeSnapshotPage,
  decodeSnapshotPageBody,
  decodeSnapshotVerifyRequest,
  encodeCursor,
  encodeSnapshotPage,
  MAX_SNAPSHOT_PAGE_OBJECTS,
  MAX_SNAPSHOT_PAGES,
  snapshotPageCount,
  type SnapshotObjectRef,
} from "../protocol.js";
import type { Env } from "../index.js";
import {
  boundOrigin,
  readJson,
  refuse,
  versionOrError,
  type SyncTiming,
} from "./shared.js";

const SNAPSHOTS_PATH = "/api/v1/sync/snapshots";
const SNAPSHOT_PREFIX = "/api/v1/sync/snapshots/";
const GC_PATH = "/api/v1/sync/gc";
const DAY_SECONDS = 86_400;
const WEEK_SECONDS = 604_800;
const MAX_GC_SWEEP = 200;
const MAX_GC_PRUNE = 1;

function snapshotJson(row: SnapshotRow): Record<string, unknown> {
  return {
    id: row.id,
    origin_host: row.origin_host,
    created_at: row.created_at,
    schema_version: row.schema_version,
    db_digest: row.db_digest,
    db_size: row.db_size,
    objects: row.objects,
    object_bytes: row.object_bytes,
    objects_digest: row.objects_digest,
    verified_at: row.verified_at,
    revision: row.revision,
  };
}

function chunkJson(row: SnapshotChunkRow): Record<string, unknown> | null {
  if (row.encoding === null || row.ciphertext_size === null) return null;
  return {
    ordinal: row.ordinal,
    plaintext_digest: row.plaintext_digest,
    size: row.size,
    ciphertext_digest: row.ciphertext_digest,
    encoding: row.encoding,
    ciphertext_size: row.ciphertext_size,
  };
}

function objectJson(row: RemoteObjectRow): Record<string, unknown> {
  return {
    plaintext_digest: row.plaintext_digest,
    size: row.plaintext_size,
    ciphertext_digest: row.ciphertext_digest,
    encoding: row.encoding,
    ciphertext_size: row.ciphertext_size,
  };
}

const SNAPSHOT_PAGE_PREFIX = "v1/s/";

function snapshotPrefix(origin: string, id: string): string {
  return `${SNAPSHOT_PAGE_PREFIX}${origin}/${id}/`;
}

function snapshotPageKey(origin: string, id: string, page: number): string {
  return `${snapshotPrefix(origin, id)}${page}`;
}

function allPageKeys(origin: string, id: string): string[] {
  return Array.from({ length: MAX_SNAPSHOT_PAGES }, (_, page) => snapshotPageKey(origin, id, page));
}

async function readPage(env: Env, origin: string, id: string, page: number): Promise<readonly SnapshotObjectRef[] | null> {
  const stored = await env.BUCKET.get(snapshotPageKey(origin, id, page));
  return stored === null ? null : decodeSnapshotPageBody(await stored.text());
}

async function knownSizesMatch(env: Env, origin: string, objects: readonly SnapshotObjectRef[]): Promise<boolean> {
  const known = new Map((await readKnownObjects(env.DB, origin, objects.map((object) => object.plaintext_digest)))
    .map((row) => [row.plaintext_digest, row.plaintext_size]));
  return objects.every((object) => known.get(object.plaintext_digest) === object.size);
}

type PinnedList =
  | { readonly ok: true; readonly digests: readonly string[] }
  | { readonly ok: false; readonly message: string };

async function readCommittedPages(
  env: Env,
  origin: string,
  snapshot: { readonly id: string; readonly objects: number; readonly object_bytes: number; readonly objects_digest: string },
): Promise<PinnedList> {
  const pages = snapshotPageCount(snapshot.objects);
  const digests: string[] = [];
  const bodies: string[] = [];
  let bytes = 0;
  for (let page = 0; page < pages; page += 1) {
    const objects = await readPage(env, origin, snapshot.id, page);
    if (objects === null) return { ok: false, message: `snapshot object page ${page} is not stored` };
    const expected = page < pages - 1 ? MAX_SNAPSHOT_PAGE_OBJECTS : snapshot.objects - MAX_SNAPSHOT_PAGE_OBJECTS * page;
    if (objects.length !== expected) return { ok: false, message: `snapshot object page ${page} has the wrong length` };
    for (const object of objects) {
      const previous = digests.at(-1);
      if (previous !== undefined && previous >= object.plaintext_digest) {
        return { ok: false, message: "snapshot objects are not in strictly ascending order" };
      }
      digests.push(object.plaintext_digest);
      bytes += object.size;
    }
    bodies.push(encodeSnapshotPage(objects));
  }
  if (bytes !== snapshot.object_bytes) return { ok: false, message: "snapshot object bytes do not match its pages" };
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(bodies.join("")));
  const hex = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  if (hex !== snapshot.objects_digest) return { ok: false, message: "snapshot objects digest does not match its pages" };
  return { ok: true, digests };
}

async function pinnedDigests(env: Env, origin: string, rows: readonly SnapshotRow[]): Promise<Set<string>> {
  const pinned = new Set<string>();
  for (const row of rows) {
    for (let page = 0; page < snapshotPageCount(row.objects); page += 1) {
      const objects = await readPage(env, origin, row.id, page);
      if (objects === null) throw new Error(`snapshot ${row.id} object page ${page} is not stored`);
      for (const object of objects) pinned.add(object.plaintext_digest);
    }
  }
  return pinned;
}

function limitParameter(value: string | null): number | null {
  if (value === null) return 100;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 1 && parsed <= MAX_PAGE ? parsed : null;
}

function retainedSnapshotIds(rows: readonly SnapshotRow[], now: number): Set<string> {
  const keep = new Set<string>();
  if (rows.length === 0) return keep;
  const sorted = [...rows].sort((a, b) => b.created_at - a.created_at || b.id.localeCompare(a.id));
  const newest = sorted[0];
  if (newest !== undefined) keep.add(newest.id);
  const verified = sorted.filter((row) => row.verified_at !== null);
  const newestVerified = verified[0];
  if (newestVerified !== undefined && now - newestVerified.created_at <= 4 * WEEK_SECONDS) {
    keep.add(newestVerified.id);
  }
  const days = new Map<number, SnapshotRow>();
  const weeks = new Map<number, SnapshotRow>();
  for (const row of sorted) {
    const day = Math.floor(row.created_at / DAY_SECONDS);
    const week = Math.floor(row.created_at / WEEK_SECONDS);
    if (!days.has(day)) days.set(day, row);
    if (!weeks.has(week)) weeks.set(week, row);
  }
  const today = Math.floor(now / DAY_SECONDS);
  const thisWeek = Math.floor(now / WEEK_SECONDS);
  for (const [day, row] of days) {
    if (today - day < 7) keep.add(row.id);
  }
  for (const [week, row] of weeks) {
    if (thisWeek - week < 4) keep.add(row.id);
  }
  return keep;
}

async function deleteUncommittedPages(env: Env, origin: string, committed: ReadonlySet<string>, now: number): Promise<void> {
  const prefix = `${SNAPSHOT_PAGE_PREFIX}${origin}/`;
  const stale: string[] = [];
  let cursor: string | undefined;
  do {
    const listing = await env.BUCKET.list(cursor === undefined ? { prefix } : { prefix, cursor });
    for (const object of listing.objects) {
      const id = object.key.slice(prefix.length).split("/")[0];
      if (id !== undefined && !committed.has(id) && now - object.uploaded.getTime() / 1000 > GC_GRACE_SECONDS) {
        stale.push(object.key);
      }
    }
    cursor = listing.truncated ? listing.cursor : undefined;
  } while (cursor !== undefined);
  for (let start = 0; start < stale.length; start += 1_000) await env.BUCKET.delete(stale.slice(start, start + 1_000));
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
  const commitBody = decodeSnapshotCommit(body.value);
  if (commitBody === null) return refuse(400, "invalid_request", "invalid snapshot commit", timing);
  const mismatch = boundOrigin(principal, commitBody.origin, timing);
  if (mismatch !== null) return mismatch;
  const { snapshot } = commitBody;

  const started = performance.now();
  try {
    const duplicate = await readSnapshot(env.DB, commitBody.origin, snapshot.id);
    if (duplicate !== null) {
      if (duplicate.db_digest !== snapshot.db_digest || duplicate.db_size !== snapshot.db_size) {
        return refuse(409, "conflict", "snapshot id is bound to a different database", timing);
      }
      return json(200, {
        protocol: PROTOCOL,
        origin: commitBody.origin,
        snapshot: duplicate.id,
        revision: duplicate.revision,
        duplicate: true,
        chunks: null,
        objects: duplicate.objects,
        object_bytes: duplicate.object_bytes,
      }, false, { "Server-Timing": serverTiming(timing) });
    }

    const chunkDigests = snapshot.chunks.map((chunk) => chunk.plaintext_digest);
    const knownChunks = new Map((await readKnownObjects(env.DB, commitBody.origin, chunkDigests))
      .map((row) => [row.plaintext_digest, row]));
    for (const chunk of snapshot.chunks) {
      const known = knownChunks.get(chunk.plaintext_digest);
      if (known === undefined || known.plaintext_size !== chunk.size || known.ciphertext_digest !== chunk.ciphertext_digest) {
        return refuse(409, "missing_object", "snapshot chunk is not stored", timing);
      }
    }
    const pages = await readCommittedPages(env, commitBody.origin, snapshot);
    if (!pages.ok) return refuse(409, "missing_object", pages.message, timing);
    const pinnedKnown = await readKnownObjects(env.DB, commitBody.origin, pages.digests);
    if (pinnedKnown.length !== pages.digests.length) {
      return refuse(409, "missing_object", "snapshot object is not stored", timing);
    }
    const result = await commitSnapshot(env.DB, {
      origin_host: commitBody.origin,
      id: snapshot.id,
      created_at: snapshot.created_at,
      schema_version: snapshot.schema_version,
      db_digest: snapshot.db_digest,
      db_size: snapshot.db_size,
      chunks: snapshot.chunks,
      objects: snapshot.objects,
      object_bytes: snapshot.object_bytes,
      objects_digest: snapshot.objects_digest,
      pinned: pages.digests,
    }, commitBody.base_revision);
    return json(200, {
      protocol: PROTOCOL,
      origin: commitBody.origin,
      snapshot: snapshot.id,
      revision: result.revision,
      duplicate: false,
      chunks: snapshot.chunks.length,
      objects: snapshot.objects,
      object_bytes: snapshot.object_bytes,
    }, false, { "Server-Timing": serverTiming(timing) });
  } catch (error) {
    const message = String(error);
    if (message.includes("stale_revision")) {
      return refuse(409, "stale_revision", "base revision is stale", timing);
    }
    if (message.includes("UNIQUE constraint failed")) {
      return refuse(409, "conflict", "snapshot already exists", timing);
    }
    reportError("snapshots.commit", error);
    return refuse(503, "unavailable", "snapshot commit failed", timing);
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
  const limit = limitParameter(url.searchParams.get("limit"));
  if (limit === null) return refuse(400, "invalid_request", "limit is invalid", timing);
  const beforeValue = url.searchParams.get("before");
  let before: { readonly createdAt: number; readonly id: string } | null = null;
  if (beforeValue !== null) {
    const parts = decodeCursor(beforeValue);
    const createdAt = parts === null || parts.length !== 2 ? null : Number(parts[0]);
    const id = parts === null || parts.length !== 2 ? null : decodeDigest(parts[1]);
    if (createdAt === null || !Number.isFinite(createdAt) || id === null) {
      return refuse(400, "invalid_request", "before is invalid", timing);
    }
    before = { createdAt, id };
  }
  const started = performance.now();
  try {
    const rows = await readSnapshots(env.DB, origin, limit + 1, before);
    const page = rows.slice(0, limit);
    const last = page.at(-1);
    return json(200, {
      protocol: PROTOCOL,
      origin,
      snapshots: page.map(snapshotJson),
      next_cursor: rows.length > limit && last !== undefined
        ? encodeCursor([String(last.created_at), last.id])
        : null,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function detail(
  request: Request,
  env: Env,
  principal: Principal,
  id: string,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "GET") return methodNotAllowed(["GET"], { "Server-Timing": serverTiming(timing) });
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  const started = performance.now();
  try {
    const row = await readSnapshot(env.DB, principal.origin_host, id);
    if (row === null) return refuse(404, "not_found", "snapshot not found", timing);
    const chunks = await readSnapshotChunks(env.DB, principal.origin_host, id);
    const chunkJsonRows = chunks.map(chunkJson);
    if (chunkJsonRows.some((item) => item === null)) {
      return refuse(409, "missing_object", "snapshot references an object that is not stored", timing);
    }
    const chunkBytes = chunks.reduce((total, chunk) => total + chunk.size, 0);
    if (chunkBytes !== row.db_size || chunks.length === 0) {
      return refuse(503, "unavailable", "snapshot chunks do not cover the database", timing);
    }
    return json(200, {
      protocol: PROTOCOL,
      origin: principal.origin_host,
      snapshot: snapshotJson(row),
      chunks: chunkJsonRows,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function putPage(
  request: Request,
  env: Env,
  principal: Principal,
  id: string,
  timing: SyncTiming,
): Promise<Response> {
  const body = await readJson(request, timing);
  if (!body.ok) return body.response;
  const version = versionOrError(body.value, timing);
  if (version !== null) return version;
  const page = decodeSnapshotPage(body.value);
  if (page === null) return refuse(400, "invalid_request", "invalid snapshot object page", timing);
  const mismatch = boundOrigin(principal, page.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  try {
    const committed = await readSnapshot(env.DB, page.origin, id) !== null;
    if (!committed) {
      if (!await knownSizesMatch(env, page.origin, page.objects)) {
        return refuse(409, "missing_object", "snapshot object is not stored", timing);
      }
      await clearObjectsGcPending(env.DB, page.origin, page.objects.map((object) => object.plaintext_digest));
      await env.BUCKET.put(snapshotPageKey(page.origin, id, page.page), encodeSnapshotPage(page.objects));
    }
    return json(200, {
      protocol: PROTOCOL,
      origin: page.origin,
      snapshot: id,
      page: page.page,
      objects: page.objects.length,
      duplicate: committed,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function readObjectsPage(
  env: Env,
  principal: Extract<Principal, { readonly kind: "collector" }>,
  id: string,
  url: URL,
  timing: SyncTiming,
): Promise<Response> {
  const value = url.searchParams.get("page");
  const page = value === null || !/^(0|[1-9][0-9]*)$/.test(value) ? null : Number(value);
  if (page === null) return refuse(400, "invalid_request", "page is invalid", timing);
  const started = performance.now();
  try {
    const row = await readSnapshot(env.DB, principal.origin_host, id);
    if (row === null) return refuse(404, "not_found", "snapshot not found", timing);
    if (page >= snapshotPageCount(row.objects)) return refuse(404, "not_found", "snapshot object page not found", timing);
    const objects = await readPage(env, principal.origin_host, id, page);
    if (objects === null) return refuse(503, "unavailable", "snapshot object page is not stored", timing);
    const known = new Map((await readKnownObjects(env.DB, principal.origin_host, objects.map((object) => object.plaintext_digest)))
      .map((known) => [known.plaintext_digest, known]));
    const rows: Record<string, unknown>[] = [];
    for (const object of objects) {
      const stored = known.get(object.plaintext_digest);
      if (stored === undefined || stored.plaintext_size !== object.size) {
        return refuse(409, "missing_object", "snapshot references an object that is not stored", timing);
      }
      rows.push(objectJson(stored));
    }
    return json(200, {
      protocol: PROTOCOL,
      origin: principal.origin_host,
      snapshot: id,
      page,
      objects: rows,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function verify(
  request: Request,
  env: Env,
  principal: Principal,
  id: string,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "POST") return methodNotAllowed(["POST"], { "Server-Timing": serverTiming(timing) });
  const body = await readJson(request, timing);
  if (!body.ok) return body.response;
  const version = versionOrError(body.value, timing);
  if (version !== null) return version;
  const verifyBody = decodeSnapshotVerifyRequest(body.value);
  if (verifyBody === null) return refuse(400, "invalid_request", "invalid snapshot verification", timing);
  const mismatch = boundOrigin(principal, verifyBody.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  try {
    const existing = await readSnapshot(env.DB, verifyBody.origin, id);
    if (existing === null) return refuse(404, "not_found", "snapshot not found", timing);
    const verifiedAt = await markSnapshotVerified(env.DB, verifyBody.origin, id, Date.now() / 1000);
    return json(200, {
      protocol: PROTOCOL,
      origin: verifyBody.origin,
      snapshot: id,
      verified_at: verifiedAt,
      duplicate: existing.verified_at !== null,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function gc(
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
  const gcBody = decodeGcRequest(body.value);
  if (gcBody === null) return refuse(400, "invalid_request", "invalid gc request", timing);
  const mismatch = boundOrigin(principal, gcBody.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  try {
    const now = Date.now() / 1000;
    const snapshots = await readSnapshots(env.DB, gcBody.origin, 1_000);
    const retained = retainedSnapshotIds(snapshots, now);
    const pruned = snapshots.filter((row) => !retained.has(row.id)).map((row) => row.id);
    const pruneBatch = gcBody.apply ? pruned.slice(0, MAX_GC_PRUNE) : pruned;
    if (gcBody.apply && pruneBatch.length > 0) {
      await deleteSnapshots(env.DB, pruneBatch);
      await env.BUCKET.delete(pruneBatch.flatMap((id) => allPageKeys(gcBody.origin, id)));
    }
    const excluded = gcBody.apply ? pruneBatch : pruned;
    const live = snapshots.filter((row) => !excluded.includes(row.id));
    if (gcBody.apply) await deleteUncommittedPages(env, gcBody.origin, new Set(snapshots.map((row) => row.id)), now);
    const pinned = await pinnedDigests(env, gcBody.origin, live);
    const pending = (await readUnreferencedObjects(env.DB, gcBody.origin, excluded))
      .filter((row) => !pinned.has(row.plaintext_digest));
    const markable = pending.filter((row) => row.gc_pending_at === null);
    if (gcBody.apply && markable.length > 0) {
      await markObjectsGcPending(env.DB, gcBody.origin, now, markable.map((row) => row.plaintext_digest), excluded);
    }
    const cutoff = now - GC_GRACE_SECONDS;
    const sweepable = (await readUnreferencedObjects(env.DB, gcBody.origin, excluded, cutoff))
      .filter((row) => !pinned.has(row.plaintext_digest));
    const batch = gcBody.apply ? sweepable.slice(0, MAX_GC_SWEEP) : sweepable;
    let swept = 0;
    let sweptBytes = 0;
    const deletedKeys: string[] = [];
    for (const row of batch) {
      if (gcBody.apply) {
        const deleted = await deleteObjectIfGcEligible(env.DB, gcBody.origin, row.plaintext_digest, cutoff, excluded);
        if (!deleted) continue;
        deletedKeys.push(`v1/o/${row.ciphertext_digest}`);
      }
      swept += 1;
      sweptBytes += row.ciphertext_size;
    }
    if (deletedKeys.length > 0) await env.BUCKET.delete(deletedKeys);
    return json(200, {
      protocol: PROTOCOL,
      origin: gcBody.origin,
      apply: gcBody.apply,
      snapshots_retained: snapshots.length - pruneBatch.length,
      snapshots_pruned: pruneBatch.length,
      objects_marked: markable.length,
      bytes_marked: markable.reduce((total, row) => total + row.ciphertext_size, 0),
      objects_swept: swept,
      bytes_swept: sweptBytes,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

export async function handleSnapshots(
  request: Request,
  env: Env,
  principal: Principal,
  pathname: string,
  url: URL,
  timing: SyncTiming,
): Promise<Response | null> {
  if (pathname === SNAPSHOTS_PATH) {
    if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
    if (request.method === "POST") return await commit(request, env, principal, timing);
    if (request.method === "GET") return await list(request, env, principal, url, timing);
    return methodNotAllowed(["GET", "POST"], { "Server-Timing": serverTiming(timing) });
  }
  if (pathname === GC_PATH) {
    if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
    return await gc(request, env, principal, timing);
  }
  if (!pathname.startsWith(SNAPSHOT_PREFIX)) return null;
  const rest = pathname.slice(SNAPSHOT_PREFIX.length);
  const [id, action, ...extra] = rest.split("/");
  if (extra.length > 0 || id === undefined || action !== undefined && action !== "verify" && action !== "objects") {
    return refuse(404, "not_found", "route not found", timing);
  }
  const snapshotId = decodeDigest(id);
  if (snapshotId === null) return refuse(404, "not_found", "route not found", timing);
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  if (action === "verify") return await verify(request, env, principal, snapshotId, timing);
  if (action === "objects") {
    if (request.method === "POST") return await putPage(request, env, principal, snapshotId, timing);
    if (request.method === "GET") return await readObjectsPage(env, principal, snapshotId, url, timing);
    return methodNotAllowed(["GET", "POST"], { "Server-Timing": serverTiming(timing) });
  }
  return await detail(request, env, principal, snapshotId, timing);
}
