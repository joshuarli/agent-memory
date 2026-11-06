import { projectionDeletionStatements } from "./learning.js";

export interface StoreMeta {
  readonly id: string;
  readonly created_at: number;
}

export interface OriginState {
  readonly revision: number;
  readonly tombstone_watermark: number;
}

export interface GenerationRow {
  readonly id: number;
  readonly origin_host: string;
  readonly harness: string;
  readonly native_id: string;
  readonly manifest_digest: string;
  readonly source_digest: string;
  readonly source_size: number;
  readonly captured_at: number;
  readonly committed_at: number;
  readonly revision: number;
  readonly manifest_object: string;
}

export interface GenerationChunk {
  readonly digest: string;
  readonly size: number;
}

export interface NewGeneration {
  readonly origin_host: string;
  readonly harness: string;
  readonly native_id: string;
  readonly manifest_digest: string;
  readonly source_digest: string;
  readonly source_size: number;
  readonly captured_at: number;
  readonly committed_at: number;
  readonly manifest_object: string;
  readonly objects: readonly GenerationChunk[];
}

export interface TombstoneRow {
  readonly origin_host: string;
  readonly harness: string;
  readonly native_id: string;
  readonly scope: string;
  readonly forgotten_at: number;
  readonly revision: number;
}

export interface TombstoneKey {
  readonly harness: string;
  readonly native_id: string;
}

export interface NewTombstone extends TombstoneKey {
  readonly scope: string;
  readonly forgotten_at: number;
}

export interface RemoteObjectRow {
  readonly plaintext_digest: string;
  readonly plaintext_size: number;
  readonly encoding: string;
  readonly ciphertext_digest: string;
  readonly ciphertext_size: number;
}

export interface SnapshotRow {
  readonly id: string;
  readonly origin_host: string;
  readonly created_at: number;
  readonly schema_version: number;
  readonly db_digest: string;
  readonly db_size: number;
  readonly objects: number;
  readonly object_bytes: number;
  readonly objects_digest: string;
  readonly verified_at: number | null;
  readonly revision: number;
}

export interface SnapshotChunkRow {
  readonly ordinal: number;
  readonly plaintext_digest: string;
  readonly size: number;
  readonly ciphertext_digest: string;
  readonly encoding: string | null;
  readonly ciphertext_size: number | null;
}

export interface NewSnapshot {
  readonly origin_host: string;
  readonly id: string;
  readonly created_at: number;
  readonly schema_version: number;
  readonly db_digest: string;
  readonly db_size: number;
  readonly chunks: readonly {
    readonly plaintext_digest: string;
    readonly size: number;
    readonly ciphertext_digest: string;
  }[];
  readonly objects: number;
  readonly object_bytes: number;
  readonly objects_digest: string;
  readonly pinned: readonly string[];
}

export interface GcObjectRow {
  readonly plaintext_digest: string;
  readonly plaintext_size: number;
  readonly ciphertext_digest: string;
  readonly ciphertext_size: number;
  readonly gc_pending_at: number | null;
}

export interface NewRemoteObject {
  readonly plaintext_digest: string;
  readonly plaintext_size: number;
  readonly encoding: string;
  readonly ciphertext_digest: string;
  readonly ciphertext_size: number;
  readonly origin_host: string;
  readonly created_at: number;
}

const QUERY_CHUNK = 90;
const LIST_CHUNK = 500;

export async function readStoreMeta(db: D1Database): Promise<StoreMeta | null> {
  return await db.prepare("SELECT id, created_at FROM store_meta LIMIT 1").first<StoreMeta>();
}

export async function readOriginState(db: D1Database, origin: string): Promise<OriginState> {
  const sync = await db.prepare("SELECT revision FROM origin_sync WHERE origin_host = ?")
    .bind(origin)
    .first<{ readonly revision: number }>();
  const tombstone = await db.prepare("SELECT COALESCE(MAX(revision), 0) AS watermark FROM tombstone WHERE origin_host = ?")
    .bind(origin)
    .first<{ readonly watermark: number }>();
  return {
    revision: sync?.revision ?? 0,
    tombstone_watermark: tombstone?.watermark ?? 0,
  };
}

export async function readKnownObjects(
  db: D1Database,
  origin: string,
  digests: readonly string[],
): Promise<readonly RemoteObjectRow[]> {
  const statements: D1PreparedStatement[] = [];
  for (let start = 0; start < digests.length; start += LIST_CHUNK) {
    statements.push(db.prepare(
      `SELECT plaintext_digest, plaintext_size, encoding, ciphertext_digest, ciphertext_size
       FROM remote_object WHERE origin_host = ? AND plaintext_digest IN (SELECT value FROM json_each(?))`,
    ).bind(origin, JSON.stringify(digests.slice(start, start + LIST_CHUNK))));
  }
  if (statements.length === 0) return [];
  const results = await db.batch<RemoteObjectRow>(statements);
  return results.flatMap((result) => result.results);
}

function clearGcPending(db: D1Database, origin: string, digests: readonly string[]): D1PreparedStatement[] {
  const statements: D1PreparedStatement[] = [];
  for (let start = 0; start < digests.length; start += LIST_CHUNK) {
    statements.push(db.prepare(
      `UPDATE remote_object SET gc_pending_at = NULL
       WHERE origin_host = ? AND gc_pending_at IS NOT NULL
         AND plaintext_digest IN (SELECT value FROM json_each(?))`,
    ).bind(origin, JSON.stringify(digests.slice(start, start + LIST_CHUNK))));
  }
  return statements;
}

export async function clearObjectsGcPending(db: D1Database, origin: string, digests: readonly string[]): Promise<void> {
  const statements = clearGcPending(db, origin, digests);
  if (statements.length > 0) await db.batch(statements);
}

export async function readObjectByCiphertext(
  db: D1Database,
  origin: string,
  ciphertext: string,
): Promise<RemoteObjectRow | null> {
  return await db.prepare(
    `SELECT plaintext_digest, plaintext_size, encoding, ciphertext_digest, ciphertext_size
     FROM remote_object WHERE origin_host = ? AND ciphertext_digest = ?`,
  ).bind(origin, ciphertext).first<RemoteObjectRow>();
}

export async function insertRemoteObject(db: D1Database, object: NewRemoteObject): Promise<void> {
  await db.prepare(
    `INSERT OR IGNORE INTO remote_object(
       plaintext_digest, plaintext_size, encoding, ciphertext_digest, ciphertext_size,
       origin_host, created_at
     ) VALUES(?, ?, ?, ?, ?, ?, ?)`,
  ).bind(
    object.plaintext_digest,
    object.plaintext_size,
    object.encoding,
    object.ciphertext_digest,
    object.ciphertext_size,
    object.origin_host,
    object.created_at,
  ).run();
}

export async function readObjectByPlaintext(
  db: D1Database,
  origin: string,
  plaintext: string,
): Promise<RemoteObjectRow | null> {
  return await db.prepare(
    `SELECT plaintext_digest, plaintext_size, encoding, ciphertext_digest, ciphertext_size
     FROM remote_object WHERE origin_host = ? AND plaintext_digest = ?`,
  ).bind(origin, plaintext).first<RemoteObjectRow>();
}

const GENERATION_COLUMNS =
  "id, origin_host, harness, native_id, manifest_digest, source_digest, source_size, captured_at, committed_at, revision, manifest_object";

export async function readGenerationByManifest(
  db: D1Database,
  origin: string,
  manifest: string,
): Promise<GenerationRow | null> {
  return await db.prepare(
    `SELECT ${GENERATION_COLUMNS} FROM generation WHERE origin_host = ? AND manifest_digest = ?`,
  ).bind(origin, manifest).first<GenerationRow>();
}

export async function readObjectSizes(
  db: D1Database,
  origin: string,
  digests: readonly string[],
): Promise<ReadonlyMap<string, number>> {
  const statements: D1PreparedStatement[] = [];
  for (let start = 0; start < digests.length; start += QUERY_CHUNK) {
    const chunk = digests.slice(start, start + QUERY_CHUNK);
    const placeholders = chunk.map(() => "?").join(",");
    statements.push(db.prepare(
      `SELECT plaintext_digest, plaintext_size FROM remote_object
       WHERE origin_host = ? AND plaintext_digest IN (${placeholders})`,
    ).bind(origin, ...chunk));
  }
  if (statements.length === 0) return new Map();
  const sizes = new Map<string, number>();
  const results = await db.batch<{ readonly plaintext_digest: string; readonly plaintext_size: number }>(statements);
  for (const result of results) {
    for (const row of result.results) sizes.set(row.plaintext_digest, row.plaintext_size);
  }
  return sizes;
}

export async function commitGeneration(
  db: D1Database,
  generation: NewGeneration,
  baseRevision: number,
): Promise<{ readonly id: number; readonly revision: number }> {
  const revision = baseRevision + 1;
  const statements: D1PreparedStatement[] = [
    db.prepare("INSERT OR IGNORE INTO origin_sync(origin_host, revision, last_change_at) VALUES(?, 0, NULL)")
      .bind(generation.origin_host),
    db.prepare(
      `INSERT INTO generation(
         origin_host, harness, native_id, manifest_digest, source_digest, source_size,
         captured_at, committed_at, revision, manifest_object
       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      generation.origin_host,
      generation.harness,
      generation.native_id,
      generation.manifest_digest,
      generation.source_digest,
      generation.source_size,
      generation.captured_at,
      generation.committed_at,
      revision,
      generation.manifest_object,
    ),
  ];
  const firstReferences = new Map<string, { readonly ordinal: number; readonly size: number }>();
  generation.objects.forEach((object, ordinal) => {
    if (!firstReferences.has(object.digest)) firstReferences.set(object.digest, { ordinal, size: object.size });
  });
  const unreferenced = [...firstReferences].map(([digest, object]) => ({ digest, ...object }));
  for (let start = 0; start < unreferenced.length; start += LIST_CHUNK) {
    statements.push(
      db.prepare(
        `INSERT INTO generation_object(generation, ordinal, plaintext_digest, size)
         SELECT (SELECT id FROM generation WHERE origin_host = ?1 AND manifest_digest = ?2),
                listed.value ->> '$.ordinal', listed.value ->> '$.digest', listed.value ->> '$.size'
         FROM json_each(?5) listed
         WHERE NOT EXISTS (
           SELECT 1 FROM generation_object go JOIN generation g ON g.id = go.generation
           WHERE g.origin_host = ?1 AND g.harness = ?3 AND g.native_id = ?4
             AND go.plaintext_digest = listed.value ->> '$.digest'
         )`,
      ).bind(
        generation.origin_host,
        generation.manifest_digest,
        generation.harness,
        generation.native_id,
        JSON.stringify(unreferenced.slice(start, start + LIST_CHUNK)),
      ),
    );
  }
  statements.push(...clearGcPending(db, generation.origin_host, [generation.manifest_digest, ...firstReferences.keys()]));
  statements.push(
    db.prepare("UPDATE origin_sync SET revision = revision + 1, last_change_at = ? WHERE origin_host = ?")
      .bind(generation.committed_at, generation.origin_host),
  );
  const results = await db.batch(statements);
  const inserted = results[1];
  const id = inserted?.meta.last_row_id ?? 0;
  if (id <= 0) throw new Error("generation insert did not return an id");
  return { id, revision };
}

export async function readGenerations(
  db: D1Database,
  origin: string,
  after: number,
  limit: number,
): Promise<readonly GenerationRow[]> {
  const result = await db.prepare(
    `SELECT ${GENERATION_COLUMNS} FROM generation g
     WHERE g.origin_host = ? AND g.id > ?
       AND NOT EXISTS (
         SELECT 1 FROM tombstone t
         WHERE t.origin_host = g.origin_host AND t.harness = g.harness AND t.native_id = g.native_id
       )
     ORDER BY g.id LIMIT ?`,
  ).bind(origin, after, limit).all<GenerationRow>();
  return result.results;
}

export async function readTombstone(
  db: D1Database,
  origin: string,
  harness: string,
  native: string,
): Promise<TombstoneRow | null> {
  return await db.prepare(
    `SELECT origin_host, harness, native_id, scope, forgotten_at, revision
     FROM tombstone WHERE origin_host = ? AND harness = ? AND native_id = ?`,
  ).bind(origin, harness, native).first<TombstoneRow>();
}

export async function readTombstones(
  db: D1Database,
  origin: string,
  after: { readonly revision: number; readonly harness: string; readonly native_id: string } | null,
  limit: number,
): Promise<readonly TombstoneRow[]> {
  if (after === null) {
    const result = await db.prepare(
      `SELECT origin_host, harness, native_id, scope, forgotten_at, revision
       FROM tombstone WHERE origin_host = ? ORDER BY revision, harness, native_id LIMIT ?`,
    ).bind(origin, limit).all<TombstoneRow>();
    return result.results;
  }
  const result = await db.prepare(
    `SELECT origin_host, harness, native_id, scope, forgotten_at, revision
     FROM tombstone WHERE origin_host = ?
       AND (revision > ? OR (revision = ? AND (harness > ? OR (harness = ? AND native_id > ?))))
     ORDER BY revision, harness, native_id LIMIT ?`,
  ).bind(origin, after.revision, after.revision, after.harness, after.harness, after.native_id, limit)
    .all<TombstoneRow>();
  return result.results;
}

export async function readTombstoneKeys(
  db: D1Database,
  origin: string,
  keys: readonly TombstoneKey[],
): Promise<ReadonlyMap<string, TombstoneRow>> {
  const rows = new Map<string, TombstoneRow>();
  const PAIRS_PER_QUERY = 40;
  for (let start = 0; start < keys.length; start += PAIRS_PER_QUERY) {
    const chunk = keys.slice(start, start + PAIRS_PER_QUERY);
    const clauses = chunk.map(() => "(harness = ? AND native_id = ?)").join(" OR ");
    const params: string[] = [];
    for (const key of chunk) params.push(key.harness, key.native_id);
    const result = await db.prepare(
      `SELECT origin_host, harness, native_id, scope, forgotten_at, revision
       FROM tombstone WHERE origin_host = ? AND (${clauses})`,
    ).bind(origin, ...params).all<TombstoneRow>();
    for (const row of result.results) rows.set(`${row.harness}:${row.native_id}`, row);
  }
  return rows;
}

export async function pushTombstones(
  db: D1Database,
  origin: string,
  tombstones: readonly NewTombstone[],
  baseRevision: number,
): Promise<number> {
  const revision = baseRevision + 1;
  const statements: D1PreparedStatement[] = [
    db.prepare("INSERT OR IGNORE INTO origin_sync(origin_host, revision, last_change_at) VALUES(?, 0, NULL)")
      .bind(origin),
  ];
  for (const tombstone of tombstones) {
    statements.push(
      db.prepare(
        `INSERT INTO tombstone(origin_host, harness, native_id, scope, forgotten_at, revision)
         VALUES(?, ?, ?, ?, ?, ?)
         ON CONFLICT(origin_host, harness, native_id) DO UPDATE SET
           scope = excluded.scope, forgotten_at = excluded.forgotten_at, revision = excluded.revision
         WHERE excluded.forgotten_at > tombstone.forgotten_at`,
      ).bind(origin, tombstone.harness, tombstone.native_id, tombstone.scope, tombstone.forgotten_at, revision),
      db.prepare(
        `DELETE FROM generation_object WHERE generation IN (
           SELECT id FROM generation WHERE origin_host = ? AND harness = ? AND native_id = ?)`,
      ).bind(origin, tombstone.harness, tombstone.native_id),
      db.prepare(
        "DELETE FROM generation WHERE origin_host = ? AND harness = ? AND native_id = ?",
      ).bind(origin, tombstone.harness, tombstone.native_id),
    );
  }
  const learning = await db.prepare("SELECT epoch FROM learning_state WHERE origin_host = ?")
    .bind(origin).first<{ readonly epoch: number }>();
  if (learning !== null) {
    statements.push(...projectionDeletionStatements(
      db, origin, learning.epoch,
      tombstones.map((tombstone) => `${tombstone.harness}:${tombstone.native_id}@${origin}`),
    ));
    statements.push(db.prepare("DELETE FROM learning_reconcile WHERE origin_host = ?").bind(origin));
  }
  statements.push(
    db.prepare("UPDATE origin_sync SET revision = revision + 1, last_change_at = ? WHERE origin_host = ?")
      .bind(Date.now() / 1000, origin),
  );
  await db.batch(statements);
  return revision;
}

const SNAPSHOT_COLUMNS =
  "id, origin_host, created_at, schema_version, db_digest, db_size, objects, object_bytes, objects_digest, verified_at, revision";

export async function readSnapshots(
  db: D1Database,
  origin: string,
  limit: number,
  before: { readonly createdAt: number; readonly id: string } | null = null,
): Promise<readonly SnapshotRow[]> {
  const filter = before === null ? "" : " AND (created_at < ? OR (created_at = ? AND id < ?))";
  const params: (string | number)[] = before === null
    ? [origin, limit]
    : [origin, before.createdAt, before.createdAt, before.id, limit];
  const result = await db.prepare(
    `SELECT ${SNAPSHOT_COLUMNS} FROM snapshot WHERE origin_host = ?${filter}
     ORDER BY created_at DESC, id DESC LIMIT ?`,
  ).bind(...params).all<SnapshotRow>();
  return result.results;
}

export async function readSnapshot(
  db: D1Database,
  origin: string,
  id: string,
): Promise<SnapshotRow | null> {
  return await db.prepare(
    `SELECT ${SNAPSHOT_COLUMNS} FROM snapshot WHERE origin_host = ? AND id = ?`,
  ).bind(origin, id).first<SnapshotRow>();
}

export async function readSnapshotChunks(
  db: D1Database,
  origin: string,
  id: string,
): Promise<readonly SnapshotChunkRow[]> {
  const result = await db.prepare(
    `SELECT sc.ordinal, sc.plaintext_digest, sc.size, sc.ciphertext_digest,
            ro.encoding, ro.ciphertext_size
     FROM snapshot_chunk sc
     LEFT JOIN remote_object ro
       ON ro.origin_host = ? AND ro.ciphertext_digest = sc.ciphertext_digest
     WHERE sc.snapshot = ? ORDER BY sc.ordinal`,
  ).bind(origin, id).all<SnapshotChunkRow>();
  return result.results;
}

export async function commitSnapshot(
  db: D1Database,
  snapshot: NewSnapshot,
  baseRevision: number,
): Promise<{ readonly revision: number }> {
  const revision = baseRevision + 1;
  const now = Date.now() / 1000;
  const statements: D1PreparedStatement[] = [
    db.prepare("INSERT OR IGNORE INTO origin_sync(origin_host, revision, last_change_at) VALUES(?, 0, NULL)")
      .bind(snapshot.origin_host),
    db.prepare(
      `INSERT INTO snapshot(
         id, origin_host, created_at, schema_version, db_digest, db_size, objects, object_bytes, objects_digest,
         verified_at, revision
       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)`,
    ).bind(
      snapshot.id,
      snapshot.origin_host,
      snapshot.created_at,
      snapshot.schema_version,
      snapshot.db_digest,
      snapshot.db_size,
      snapshot.objects,
      snapshot.object_bytes,
      snapshot.objects_digest,
      revision,
    ),
  ];
  const CHUNKS_PER_STATEMENT = 20;
  for (let start = 0; start < snapshot.chunks.length; start += CHUNKS_PER_STATEMENT) {
    const chunk = snapshot.chunks.slice(start, start + CHUNKS_PER_STATEMENT);
    const values = chunk.map(() => "(?, ?, ?, ?, ?)").join(",");
    const params: (string | number)[] = [];
    chunk.forEach((item, index) => {
      params.push(snapshot.id, start + index, item.plaintext_digest, item.size, item.ciphertext_digest);
    });
    statements.push(
      db.prepare(
        `INSERT INTO snapshot_chunk(snapshot, ordinal, plaintext_digest, size, ciphertext_digest)
         VALUES ${values}`,
      ).bind(...params),
    );
  }
  const referenced = [...new Set([...snapshot.chunks.map((chunk) => chunk.plaintext_digest), ...snapshot.pinned])];
  statements.push(...clearGcPending(db, snapshot.origin_host, referenced));
  statements.push(
    db.prepare("UPDATE origin_sync SET revision = revision + 1, last_change_at = ? WHERE origin_host = ?")
      .bind(now, snapshot.origin_host),
  );
  await db.batch(statements);
  return { revision };
}

export async function markSnapshotVerified(
  db: D1Database,
  origin: string,
  id: string,
  verifiedAt: number,
): Promise<number | null> {
  const existing = await readSnapshot(db, origin, id);
  if (existing === null) return null;
  if (existing.verified_at !== null) return existing.verified_at;
  await db.prepare(
    "UPDATE snapshot SET verified_at = ? WHERE origin_host = ? AND id = ?",
  ).bind(verifiedAt, origin, id).run();
  return verifiedAt;
}

export async function deleteSnapshots(
  db: D1Database,
  ids: readonly string[],
): Promise<void> {
  if (ids.length === 0) return;
  const statements: D1PreparedStatement[] = [];
  const GROUP = 50;
  for (let start = 0; start < ids.length; start += GROUP) {
    const chunk = ids.slice(start, start + GROUP);
    const placeholders = chunk.map(() => "?").join(",");
    statements.push(db.prepare(`DELETE FROM snapshot_chunk WHERE snapshot IN (${placeholders})`).bind(...chunk));
    statements.push(db.prepare(`DELETE FROM snapshot WHERE id IN (${placeholders})`).bind(...chunk));
  }
  await db.batch(statements);
}

function liveObjectsCte(excludedSnapshots: readonly string[]): { readonly sql: string; readonly params: string[] } {
  const exclusion = excludedSnapshots.length === 0
    ? ""
    : " WHERE s.id NOT IN (SELECT value FROM json_each(?))";
  return {
    sql: `WITH live_generation AS (
            SELECT g.id, g.origin_host, g.harness, g.native_id, g.manifest_object
            FROM generation g
            WHERE NOT EXISTS (
              SELECT 1 FROM tombstone t
              WHERE t.origin_host = g.origin_host AND t.harness = g.harness AND t.native_id = g.native_id
            )
          ),
          live_snapshot AS (
            SELECT s.id, s.origin_host FROM snapshot s${exclusion}
          )`,
    params: excludedSnapshots.length === 0 ? [] : [JSON.stringify(excludedSnapshots)],
  };
}

function unreferencedSql(): string {
  return `NOT EXISTS (
            SELECT 1 FROM live_generation g
            WHERE g.origin_host = ro.origin_host AND g.manifest_object = ro.ciphertext_digest
          )
          AND NOT EXISTS (
            SELECT 1 FROM generation_object go JOIN live_generation g ON g.id = go.generation
            WHERE g.origin_host = ro.origin_host AND go.plaintext_digest = ro.plaintext_digest
          )
          AND NOT EXISTS (
            SELECT 1 FROM snapshot_chunk sc JOIN live_snapshot s ON s.id = sc.snapshot
            WHERE s.origin_host = ro.origin_host AND sc.ciphertext_digest = ro.ciphertext_digest
          )
`;
}

export async function readUnreferencedObjects(
  db: D1Database,
  origin: string,
  excludedSnapshots: readonly string[] = [],
  pendingBefore: number | null = null,
): Promise<readonly GcObjectRow[]> {
  const cte = liveObjectsCte(excludedSnapshots);
  const pending = pendingBefore === null
    ? ""
    : " AND ro.gc_pending_at IS NOT NULL AND ro.gc_pending_at <= ?";
  const params: (string | number)[] = [...cte.params];
  params.push(origin);
  if (pendingBefore !== null) params.push(pendingBefore);
  const result = await db.prepare(
    `${cte.sql}
     SELECT ro.plaintext_digest, ro.plaintext_size, ro.ciphertext_digest, ro.ciphertext_size, ro.gc_pending_at
     FROM remote_object ro
     WHERE ro.origin_host = ?${pending} AND ${unreferencedSql()}
     ORDER BY ro.plaintext_digest`,
  ).bind(...params).all<GcObjectRow>();
  return result.results;
}

export async function markObjectsGcPending(
  db: D1Database,
  origin: string,
  now: number,
  digests: readonly string[],
  excludedSnapshots: readonly string[] = [],
): Promise<void> {
  const cte = liveObjectsCte(excludedSnapshots);
  const statements: D1PreparedStatement[] = [];
  for (let start = 0; start < digests.length; start += LIST_CHUNK) {
    statements.push(db.prepare(
      `${cte.sql}
       UPDATE remote_object AS ro SET gc_pending_at = ?
       WHERE ro.origin_host = ? AND ro.gc_pending_at IS NULL
         AND ro.plaintext_digest IN (SELECT value FROM json_each(?))
         AND ${unreferencedSql()}`,
    ).bind(...cte.params, now, origin, JSON.stringify(digests.slice(start, start + LIST_CHUNK))));
  }
  if (statements.length > 0) await db.batch(statements);
}

export async function deleteObjectIfGcEligible(
  db: D1Database,
  origin: string,
  plaintext: string,
  cutoff: number,
  excludedSnapshots: readonly string[] = [],
): Promise<boolean> {
  const cte = liveObjectsCte(excludedSnapshots);
  const result = await db.prepare(
    `${cte.sql}
     DELETE FROM remote_object AS ro
     WHERE ro.origin_host = ? AND ro.plaintext_digest = ?
       AND ro.gc_pending_at IS NOT NULL AND ro.gc_pending_at <= ?
       AND ${unreferencedSql()}`,
  ).bind(...cte.params, origin, plaintext, cutoff).run();
  return (result.meta.changes ?? 0) > 0;
}

export async function replaceRemoteObject(
  db: D1Database,
  object: NewRemoteObject,
  oldCiphertext: string,
): Promise<void> {
  await db.batch([
    db.prepare("PRAGMA defer_foreign_keys = ON"),
    db.prepare(
      "UPDATE generation SET manifest_object = ? WHERE origin_host = ? AND manifest_object = ?",
    ).bind(object.ciphertext_digest, object.origin_host, oldCiphertext),
    db.prepare(
      `UPDATE snapshot_chunk SET ciphertext_digest = ?
       WHERE ciphertext_digest = ?
         AND snapshot IN (SELECT id FROM snapshot WHERE origin_host = ?)`,
    ).bind(object.ciphertext_digest, oldCiphertext, object.origin_host),
    db.prepare(
      `UPDATE remote_object
       SET encoding = ?, ciphertext_digest = ?, ciphertext_size = ?, gc_pending_at = NULL
       WHERE origin_host = ? AND plaintext_digest = ?`,
    ).bind(
      object.encoding,
      object.ciphertext_digest,
      object.ciphertext_size,
      object.origin_host,
      object.plaintext_digest,
    ),
  ]);
}
