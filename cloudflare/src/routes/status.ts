import type { Principal } from "../auth.js";
import { readStoreMeta } from "../db.js";
import { json, methodNotAllowed } from "../errors.js";
import { serverTiming } from "../observe.js";
import { PROTOCOL } from "../protocol.js";
import type { OriginStatus, StatusCounts, StatusData } from "../render.js";
import type { Env } from "../index.js";
import { type SyncTiming } from "./shared.js";

const STATUS_PATH = "/api/v1/status";

const EMPTY_COUNTS: StatusCounts = {
  experiences: 0, memories: 0, projects: 0, origins: 0, tombstones: 0,
  last_change: null, last_verified: null,
  generations: 0, snapshots: 0, objects: 0, object_bytes: 0,
};

export async function readCounts(db: D1Database): Promise<StatusCounts> {
  return await db.prepare(
    `SELECT
       (SELECT count(*) FROM learning_experience e
        JOIN learning_state ls ON ls.origin_host = e.origin_host AND ls.epoch = e.epoch
        WHERE e.state = 'retained') AS experiences,
       (SELECT count(*) FROM learning_memory m
        JOIN learning_state ls ON ls.origin_host = m.origin_host AND ls.epoch = m.epoch
        WHERE m.state = 'approved') AS memories,
       (SELECT count(*) FROM (
          SELECT e.project_key FROM learning_experience e
          JOIN learning_state ls ON ls.origin_host = e.origin_host AND ls.epoch = e.epoch
          UNION
          SELECT m.project_key FROM learning_memory m
          JOIN learning_state ls ON ls.origin_host = m.origin_host AND ls.epoch = m.epoch
        )) AS projects,
       (SELECT count(*) FROM (
          SELECT origin_host FROM origin_sync
          UNION SELECT origin_host FROM learning_state
          UNION SELECT origin_host FROM collector
          LIMIT 201
        )) AS origins,
       (SELECT count(*) FROM tombstone) AS tombstones,
       (SELECT count(*) FROM generation g
        WHERE NOT EXISTS (
          SELECT 1 FROM tombstone t
          WHERE t.origin_host = g.origin_host AND t.harness = g.harness AND t.native_id = g.native_id
        )) AS generations,
       (SELECT count(*) FROM snapshot) AS snapshots,
       (SELECT count(*) FROM remote_object) AS objects,
       (SELECT COALESCE(SUM(ciphertext_size), 0) FROM remote_object) AS object_bytes,
       (SELECT max(last_change_at) FROM origin_sync) AS last_change,
       (SELECT max(verified_at) FROM snapshot WHERE verified_at IS NOT NULL) AS last_verified`,
  ).first<StatusCounts>() ?? EMPTY_COUNTS;
}

async function readOrigins(db: D1Database): Promise<readonly OriginStatus[]> {
  const result = await db.prepare(
    `WITH known AS (
       SELECT origin_host FROM origin_sync
       UNION SELECT origin_host FROM learning_state
       UNION SELECT origin_host FROM collector
     )
     SELECT k.origin_host AS origin,
       (SELECT c.label FROM collector c WHERE c.origin_host = k.origin_host
        ORDER BY c.created_at DESC, c.label LIMIT 1) AS label,
       COALESCE((SELECT os.revision FROM origin_sync os WHERE os.origin_host = k.origin_host), 0) AS revision,
       (SELECT os.last_change_at FROM origin_sync os WHERE os.origin_host = k.origin_host) AS last_change_at,
       COALESCE((SELECT ls.revision FROM learning_state ls WHERE ls.origin_host = k.origin_host), 0) AS learning_revision,
       COALESCE((SELECT ls.records FROM learning_state ls WHERE ls.origin_host = k.origin_host), 0) AS learning_records,
       (SELECT count(*) FROM generation g
        WHERE g.origin_host = k.origin_host
          AND NOT EXISTS (
            SELECT 1 FROM tombstone t
            WHERE t.origin_host = g.origin_host AND t.harness = g.harness AND t.native_id = g.native_id
          )) AS generations,
       (SELECT max(s.verified_at) FROM snapshot s WHERE s.origin_host = k.origin_host) AS last_verified
     FROM known k ORDER BY k.origin_host LIMIT 201`,
  ).all<OriginStatus>();
  return result.results;
}

export async function readStatus(db: D1Database): Promise<StatusData | null> {
  const store = await readStoreMeta(db);
  if (store === null) return null;
  const counts = await readCounts(db);
  const origins = await readOrigins(db);
  return {
    store_id: store.id,
    counts,
    origins,
    last_change: counts.last_change,
    last_verified_snapshot: counts.last_verified,
  };
}

export async function handleStatus(
  request: Request,
  env: Env,
  principal: Principal,
  pathname: string,
  timing: SyncTiming,
): Promise<Response | null> {
  if (pathname !== STATUS_PATH) return null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    return methodNotAllowed(["GET", "HEAD"], { "Server-Timing": serverTiming(timing) });
  }
  const started = performance.now();
  let status: Awaited<ReturnType<typeof readStatus>>;
  try {
    status = await readStatus(env.DB);
  } finally {
    timing.d1 += performance.now() - started;
  }
  if (status === null) {
    return json(503, { error: { code: "unavailable", message: "store is not provisioned" } },
      false, { "Server-Timing": serverTiming(timing) });
  }
  const origins = principal.kind === "collector"
    ? status.origins.filter((origin) => origin.origin === principal.origin_host)
    : status.origins;
  return json(200, {
    protocol: PROTOCOL,
    store_id: status.store_id,
    principal,
    ready: true,
    counts: {
      experiences: status.counts.experiences,
      memories: status.counts.memories,
      projects: status.counts.projects,
      origins: principal.kind === "collector" ? origins.length : status.counts.origins,
      tombstones: status.counts.tombstones,
      generations: status.counts.generations,
      snapshots: status.counts.snapshots,
      objects: status.counts.objects,
      object_bytes: status.counts.object_bytes,
    },
    origins,
    last_change: status.last_change,
    last_verified_snapshot: status.last_verified_snapshot,
  }, request.method === "HEAD", { "Server-Timing": serverTiming(timing) });
}
