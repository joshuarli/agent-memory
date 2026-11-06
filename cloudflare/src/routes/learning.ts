import type { Principal } from "../auth.js";
import { json, methodNotAllowed } from "../errors.js";
import {
  ReconcileMismatch,
  StaleLearningRevision,
  abortReconcile,
  applyLearningDelta,
  applyReconcile,
  beginReconcile,
  commitReconcile,
  decodeLearningDelta,
  decodeReconcileRequest,
  readLearningKeys,
  readLearningState,
  readReconcileStage,
  type LearningState,
} from "../learning.js";
import { serverTiming } from "../observe.js";
import { MAX_PAGE, PROTOCOL, decodeCursor, decodeOrigin } from "../protocol.js";
import type { Env } from "../index.js";
import {
  boundOrigin,
  readJson,
  refuse,
  versionOrError,
  type SyncTiming,
} from "./shared.js";

const STATE_PATH = "/api/v1/sync/learning/state";
const KEYS_PATH = "/api/v1/sync/learning/keys";
const DELTAS_PATH = "/api/v1/sync/learning/deltas";
const RECONCILE_PATH = "/api/v1/sync/learning/reconcile";

function stateJson(origin: string, state: LearningState): Record<string, unknown> {
  return {
    protocol: PROTOCOL,
    origin,
    revision: state.revision,
    epoch: state.epoch,
    digest: state.digest,
    records: state.records,
  };
}

function conflictWithState(
  origin: string,
  state: LearningState,
  code: "stale_revision" | "conflict",
  message: string,
  timing: SyncTiming,
): Response {
  return json(409, { error: { code, message }, state: stateJson(origin, state) },
    false, { "Server-Timing": serverTiming(timing) });
}

async function readState(env: Env, origin: string): Promise<LearningState> {
  return await readLearningState(env.DB, origin);
}

async function state(
  request: Request,
  env: Env,
  principal: Principal,
  url: URL,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "GET") return methodNotAllowed(["GET"], { "Server-Timing": serverTiming(timing) });
  const origin = decodeOrigin(url.searchParams.get("origin"));
  if (origin === null) return refuse(400, "invalid_request", "origin is required", timing);
  const mismatch = boundOrigin(principal, origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  let answer: LearningState;
  try {
    answer = await readState(env, origin);
  } finally {
    timing.d1 += performance.now() - started;
  }
  return json(200, stateJson(origin, answer), false, { "Server-Timing": serverTiming(timing) });
}

async function keys(
  request: Request,
  env: Env,
  principal: Principal,
  url: URL,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "GET") return methodNotAllowed(["GET"], { "Server-Timing": serverTiming(timing) });
  const origin = decodeOrigin(url.searchParams.get("origin"));
  if (origin === null) return refuse(400, "invalid_request", "origin is required", timing);
  const mismatch = boundOrigin(principal, origin, timing);
  if (mismatch !== null) return mismatch;
  const afterValue = url.searchParams.get("after");
  let after: { readonly record: "experience" | "memory"; readonly id: number } | null = null;
  if (afterValue !== null) {
    const parts = decodeCursor(afterValue);
    if (parts === null || parts.length !== 2) {
      return refuse(400, "invalid_request", "after is invalid", timing);
    }
    const [record, id] = parts;
    if ((record !== "experience" && record !== "memory") || id === undefined || !/^(0|[1-9][0-9]{0,15})$/.test(id)) {
      return refuse(400, "invalid_request", "after is invalid", timing);
    }
    after = { record, id: Number(id) };
  }
  const limitValue = url.searchParams.get("limit");
  const limit = limitValue === null ? 100 : Number(limitValue);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_PAGE) {
    return refuse(400, "invalid_request", "limit is invalid", timing);
  }
  const started = performance.now();
  try {
    const page = await readLearningKeys(env.DB, origin, after, limit);
    return json(200, {
      protocol: PROTOCOL,
      origin,
      revision: page.revision,
      epoch: page.epoch,
      keys: page.keys,
      next_cursor: page.next_cursor,
    }, false, { "Server-Timing": serverTiming(timing) });
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function deltas(
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
  const delta = decodeLearningDelta(body.value);
  if (delta === null) return refuse(400, "invalid_request", "invalid learning delta", timing);
  const mismatch = boundOrigin(principal, delta.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  try {
    const current = await readState(env, delta.origin);
    if (await readReconcileStage(env.DB, delta.origin) !== null) {
      return conflictWithState(delta.origin, current, "conflict",
        "a reconciliation is staged; retry after it commits or aborts", timing);
    }
    const applied = await applyLearningDelta(env.DB, delta, current);
    return json(200, {
      protocol: PROTOCOL,
      origin: delta.origin,
      revision: applied.state.revision,
      epoch: applied.state.epoch,
      digest: applied.state.digest,
      records: applied.state.records,
      applied: applied.applied,
      deleted: applied.deleted,
      replayed: applied.replayed,
    }, false, { "Server-Timing": serverTiming(timing) });
  } catch (error) {
    if (error instanceof StaleLearningRevision || String(error).includes("stale_revision")) {
      return conflictWithState(delta.origin, await readState(env, delta.origin), "stale_revision",
        "base revision is stale", timing);
    }
    throw error;
  } finally {
    timing.d1 += performance.now() - started;
  }
}

async function reconcile(
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
  const decoded = decodeReconcileRequest(body.value);
  if (decoded === null) return refuse(400, "invalid_request", "invalid reconciliation request", timing);
  const mismatch = boundOrigin(principal, decoded.origin, timing);
  if (mismatch !== null) return mismatch;
  const started = performance.now();
  try {
    if (decoded.action === "begin") {
      const begun = await beginReconcile(
        env.DB, decoded.origin, decoded.base_revision, decoded.digest, decoded.records);
      return json(200, {
        protocol: PROTOCOL,
        origin: decoded.origin,
        action: "begin",
        revision: begun.state.revision,
        epoch: begun.epoch,
        digest: begun.state.digest,
        records: begun.state.records,
        current: begun.current,
        resumed: begun.resumed,
      }, false, { "Server-Timing": serverTiming(timing) });
    }
    if (decoded.action === "apply") {
      const staged = await applyReconcile(env.DB, decoded.origin, decoded.epoch, decoded.changes);
      return json(200, {
        protocol: PROTOCOL,
        origin: decoded.origin,
        action: "apply",
        epoch: decoded.epoch,
        staged,
      }, false, { "Server-Timing": serverTiming(timing) });
    }
    if (decoded.action === "commit") {
      const committed = await commitReconcile(
        env.DB, decoded.origin, decoded.epoch, decoded.base_revision, decoded.digest, decoded.records);
      return json(200, {
        protocol: PROTOCOL,
        origin: decoded.origin,
        action: "commit",
        revision: committed.state.revision,
        epoch: committed.state.epoch,
        digest: committed.state.digest,
        records: committed.state.records,
        applied: committed.applied,
        replayed: committed.replayed,
      }, false, { "Server-Timing": serverTiming(timing) });
    }
    const aborted = await abortReconcile(env.DB, decoded.origin, decoded.epoch);
    return json(200, {
      protocol: PROTOCOL,
      origin: decoded.origin,
      action: "abort",
      epoch: decoded.epoch,
      aborted,
    }, false, { "Server-Timing": serverTiming(timing) });
  } catch (error) {
    if (error instanceof StaleLearningRevision || String(error).includes("stale_revision")) {
      return conflictWithState(decoded.origin, await readState(env, decoded.origin), "stale_revision",
        "base revision is stale", timing);
    }
    if (error instanceof ReconcileMismatch) {
      return conflictWithState(decoded.origin, await readState(env, decoded.origin), "conflict",
        error.message, timing);
    }
    throw error;
  } finally {
    timing.d1 += performance.now() - started;
  }
}

export async function handleLearning(
  request: Request,
  env: Env,
  principal: Principal,
  pathname: string,
  url: URL,
  timing: SyncTiming,
): Promise<Response | null> {
  if (pathname !== STATE_PATH && pathname !== KEYS_PATH && pathname !== DELTAS_PATH
      && pathname !== RECONCILE_PATH) return null;
  if (principal.kind !== "collector") return refuse(403, "forbidden", "collector token required", timing);
  if (pathname === STATE_PATH) return await state(request, env, principal, url, timing);
  if (pathname === KEYS_PATH) return await keys(request, env, principal, url, timing);
  if (pathname === DELTAS_PATH) return await deltas(request, env, principal, timing);
  return await reconcile(request, env, principal, timing);
}
