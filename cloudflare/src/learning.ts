import { HARNESSES, encodeCursor, isRecord } from "./protocol.js";

export const MAX_LEARNING_CHANGES = 200;
export const MAX_LEARNING_TEXT = 65_536;
export const MAX_LEARNING_SOURCES = 1_000;

const EXPERIENCE_KINDS = new Set(["success", "failure", "decision", "gotcha", "method"]);
const EXPERIENCE_BASES = new Set(["deterministic", "reviewed"]);
const EXPERIENCE_STATES = new Set(["retained", "retired", "superseded", "withdrawn"]);
const MEMORY_STATES = new Set(["approved", "superseded"]);
const EXPERIENCE_ROLES = new Set(["trigger", "outcome", "context"]);
const LINK_RELATIONS = new Set(["supersedes", "contradicts"]);
const HARNESS_SET = new Set<string>(HARNESSES);
const DIGEST = /^[0-9a-f]{64}$/;
const PROJECT_KEY = /^[0-9a-f]{16}$/;

export interface LearningSource {
  readonly session: string;
  readonly position: number;
  readonly digest: string;
  readonly role: string;
}

export interface LearningLink {
  readonly older: number;
  readonly relation: "supersedes" | "contradicts";
}

export interface LearningExperience {
  readonly record: "experience";
  readonly id: number;
  readonly digest: string;
  readonly project_key: string;
  readonly project_label: string | null;
  readonly kind: "success" | "failure" | "decision" | "gotcha" | "method";
  readonly claim: string;
  readonly applies_when: string;
  readonly identifiers: string;
  readonly fingerprint: string;
  readonly basis: "deterministic" | "reviewed";
  readonly detector: string;
  readonly revision: string | null;
  readonly worktree: string | null;
  readonly observed_at: number;
  readonly captured_at: number;
  readonly validity: string;
  readonly tags: string;
  readonly state: "retained" | "retired" | "superseded" | "withdrawn";
  readonly state_reason: string | null;
  readonly state_at: number | null;
  readonly sources: readonly LearningSource[];
  readonly links: readonly LearningLink[];
}

export interface LearningMemory {
  readonly record: "memory";
  readonly id: number;
  readonly digest: string;
  readonly project_key: string;
  readonly project_label: string | null;
  readonly action: string;
  readonly applicability: string;
  readonly revision: string | null;
  readonly validity: string;
  readonly state: "approved" | "superseded";
  readonly created_at: number;
  readonly sources: readonly LearningSource[];
  readonly links: readonly [];
}

export interface LearningDeletion {
  readonly record: "experience" | "memory";
  readonly id: number;
  readonly deleted: true;
}

export type LearningRecord = LearningExperience | LearningMemory;
export type LearningChange = LearningRecord | LearningDeletion;

export interface LearningDelta {
  readonly protocol: 1;
  readonly origin: string;
  readonly base_revision: number;
  readonly digest: string;
  readonly changes: readonly LearningChange[];
}

export interface LearningState {
  readonly revision: number;
  readonly epoch: number;
  readonly digest: string | null;
  readonly records: number;
}

export interface LearningApply {
  readonly state: LearningState;
  readonly applied: number;
  readonly deleted: number;
  readonly replayed: boolean;
}

export class StaleLearningRevision extends Error {}

type Optional<T> = { readonly ok: true; readonly value: T | null } | { readonly ok: false };

function keysMatch(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const actual = Object.keys(value);
  if (actual.length < required.length || actual.length > required.length + optional.length) return false;
  if (!required.every((key) => Object.hasOwn(value, key))) return false;
  return actual.every((key) => required.includes(key) || optional.includes(key));
}

function decodeText(value: unknown): string | null {
  return typeof value === "string" && value.length <= MAX_LEARNING_TEXT ? value : null;
}

function decodeCount(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function decodeNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function optionalText(value: Record<string, unknown>, key: string): Optional<string> {
  if (!Object.hasOwn(value, key) || value[key] === null) return { ok: true, value: null };
  const text = decodeText(value[key]);
  return text === null ? { ok: false } : { ok: true, value: text };
}

function optionalNumber(value: Record<string, unknown>, key: string): Optional<number> {
  if (!Object.hasOwn(value, key) || value[key] === null) return { ok: true, value: null };
  const number = decodeNumber(value[key]);
  return number === null ? { ok: false } : { ok: true, value: number };
}

export function decodeDigestField(value: unknown): string | null {
  return typeof value === "string" && DIGEST.test(value) ? value : null;
}

export function decodeProjectKey(value: unknown): string | null {
  return typeof value === "string" && PROJECT_KEY.test(value) ? value : null;
}

export function decodeProjectLabel(value: unknown): string | null {
  if (typeof value !== "string" || value.length === 0 || value.length > 128) return null;
  for (const character of value) {
    if (character.codePointAt(0)! < 32) return null;
  }
  return value;
}

export function decodeMirrorSession(value: unknown, origin: string): string | null {
  const session = decodeText(value);
  if (session === null) return null;
  const at = session.lastIndexOf("@");
  if (at <= 0 || session.slice(at + 1) !== origin) return null;
  const local = session.slice(0, at);
  if (local.includes("@")) return null;
  const colon = local.indexOf(":");
  if (colon <= 0) return null;
  const harness = local.slice(0, colon);
  const native = local.slice(colon + 1);
  if (!HARNESS_SET.has(harness) || native === "") return null;
  return session;
}

function decodeSources(value: unknown, roles: ReadonlySet<string> | null): LearningSource[] | null {
  if (!Array.isArray(value) || value.length > MAX_LEARNING_SOURCES) return null;
  const sources: LearningSource[] = [];
  for (const entry of value) {
    if (!isRecord(entry) || !keysMatch(entry, ["session", "position", "digest", "role"])) return null;
    const session = decodeText(entry.session);
    const position = decodeCount(entry.position);
    const digest = decodeText(entry.digest);
    const role = decodeText(entry.role);
    if (session === null || position === null || digest === null || role === null) return null;
    if (roles !== null && !roles.has(role)) return null;
    sources.push({ session, position, digest, role });
  }
  return sources;
}

function decodeLinks(value: unknown): LearningLink[] | null {
  if (!Array.isArray(value)) return null;
  const links: LearningLink[] = [];
  for (const entry of value) {
    if (!isRecord(entry) || !keysMatch(entry, ["older", "relation"])) return null;
    const older = decodeCount(entry.older);
    const relation = typeof entry.relation === "string" && LINK_RELATIONS.has(entry.relation)
      ? entry.relation
      : null;
    if (older === null || relation === null) return null;
    links.push({ older, relation: relation as LearningLink["relation"] });
  }
  return links;
}

function decodeExperience(value: Record<string, unknown>): LearningExperience | null {
  if (!keysMatch(value, [
    "record", "id", "digest", "project_key", "kind", "claim", "applies_when", "identifiers", "fingerprint",
    "basis", "detector", "revision", "worktree", "observed_at", "captured_at", "validity", "tags",
    "state", "state_reason", "state_at", "sources", "links",
  ], ["project_label"])) return null;
  const id = decodeCount(value.id);
  const digest = decodeDigestField(value.digest);
  const projectKey = decodeProjectKey(value.project_key);
  const labelValue = optionalText(value, "project_label");
  if (id === null || digest === null || projectKey === null || !labelValue.ok) return null;
  if (labelValue.value !== null && decodeProjectLabel(labelValue.value) === null) return null;
  const kind = typeof value.kind === "string" && EXPERIENCE_KINDS.has(value.kind) ? value.kind : null;
  const basis = typeof value.basis === "string" && EXPERIENCE_BASES.has(value.basis) ? value.basis : null;
  const state = typeof value.state === "string" && EXPERIENCE_STATES.has(value.state) ? value.state : null;
  if (kind === null || basis === null || state === null) return null;
  const claim = decodeText(value.claim);
  const appliesWhen = decodeText(value.applies_when);
  const identifiers = decodeText(value.identifiers);
  const fingerprint = decodeText(value.fingerprint);
  const detector = decodeText(value.detector);
  const validity = decodeText(value.validity);
  const tags = decodeText(value.tags);
  if (claim === null || appliesWhen === null || identifiers === null || fingerprint === null) return null;
  if (detector === null || validity === null || tags === null) return null;
  const revision = optionalText(value, "revision");
  const worktree = optionalText(value, "worktree");
  const stateReason = optionalText(value, "state_reason");
  const observedAt = decodeNumber(value.observed_at);
  const capturedAt = decodeNumber(value.captured_at);
  const stateAt = optionalNumber(value, "state_at");
  if (!revision.ok || !worktree.ok || !stateReason.ok || !stateAt.ok) return null;
  if (observedAt === null || capturedAt === null) return null;
  const sources = decodeSources(value.sources, EXPERIENCE_ROLES);
  const links = decodeLinks(value.links);
  if (sources === null || links === null) return null;
  return {
    record: "experience", id, digest, project_key: projectKey, project_label: labelValue.value,
    kind: kind as LearningExperience["kind"], claim, applies_when: appliesWhen, identifiers,
    fingerprint, basis: basis as LearningExperience["basis"], detector,
    revision: revision.value, worktree: worktree.value, observed_at: observedAt, captured_at: capturedAt,
    validity, tags, state: state as LearningExperience["state"], state_reason: stateReason.value,
    state_at: stateAt.value, sources, links,
  };
}

function decodeMemory(value: Record<string, unknown>): LearningMemory | null {
  if (!keysMatch(value, [
    "record", "id", "digest", "project_key", "action", "applicability", "revision", "validity",
    "state", "created_at", "sources", "links",
  ], ["project_label"])) return null;
  const id = decodeCount(value.id);
  const digest = decodeDigestField(value.digest);
  const projectKey = decodeProjectKey(value.project_key);
  const labelValue = optionalText(value, "project_label");
  if (id === null || digest === null || projectKey === null || !labelValue.ok) return null;
  if (labelValue.value !== null && decodeProjectLabel(labelValue.value) === null) return null;
  const action = decodeText(value.action);
  const applicability = decodeText(value.applicability);
  const validity = decodeText(value.validity);
  if (action === null || applicability === null || validity === null) return null;
  const revision = optionalText(value, "revision");
  if (!revision.ok) return null;
  const state = typeof value.state === "string" && MEMORY_STATES.has(value.state) ? value.state : null;
  const createdAt = decodeNumber(value.created_at);
  if (state === null || createdAt === null) return null;
  const sources = decodeSources(value.sources, null);
  const links = decodeLinks(value.links);
  if (sources === null || links === null || links.length !== 0) return null;
  return {
    record: "memory", id, digest, project_key: projectKey, project_label: labelValue.value,
    action, applicability, revision: revision.value, validity,
    state: state as LearningMemory["state"], created_at: createdAt, sources, links: [],
  };
}

export function decodeLearningRecord(value: unknown): LearningRecord | null {
  if (!isRecord(value)) return null;
  if (value.record === "experience") return decodeExperience(value);
  if (value.record === "memory") return decodeMemory(value);
  return null;
}

export function decodeLearningChange(value: unknown): LearningChange | null {
  if (isRecord(value) && Object.hasOwn(value, "deleted")) {
    if (!keysMatch(value, ["record", "id", "deleted"]) || value.deleted !== true) return null;
    const id = decodeCount(value.id);
    if (id === null) return null;
    if (value.record !== "experience" && value.record !== "memory") return null;
    return { record: value.record, id, deleted: true };
  }
  return decodeLearningRecord(value);
}

export function decodeLearningChanges(
  value: unknown,
  origin: string,
  allowDeletions: boolean,
): LearningChange[] | null {
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_LEARNING_CHANGES) return null;
  const changes: LearningChange[] = [];
  const keys = new Set<string>();
  for (const entry of value) {
    const change = decodeLearningChange(entry);
    if (change === null) return null;
    if (!allowDeletions && "deleted" in change) return null;
    const key = `${change.record}:${change.id}`;
    if (keys.has(key)) return null;
    keys.add(key);
    if (!("deleted" in change)) {
      for (const source of change.sources) {
        if (decodeMirrorSession(source.session, origin) === null) return null;
      }
    }
    changes.push(change);
  }
  return changes;
}

export function decodeLearningDelta(value: unknown): LearningDelta | null {
  if (!isRecord(value) || !keysMatch(value, ["protocol", "origin", "base_revision", "digest", "changes"])) {
    return null;
  }
  if (value.protocol !== 1) return null;
  const origin = typeof value.origin === "string" && /^[A-Za-z0-9._-]{1,128}$/.test(value.origin)
    ? value.origin
    : null;
  const baseRevision = decodeCount(value.base_revision);
  const digest = typeof value.digest === "string" && DIGEST.test(value.digest) ? value.digest : null;
  if (origin === null || baseRevision === null || digest === null) return null;
  const changes = decodeLearningChanges(value.changes, origin, true);
  return changes === null ? null : { protocol: 1, origin, base_revision: baseRevision, digest, changes };
}

export type ReconcileRequest =
  | {
    readonly action: "begin";
    readonly protocol: 1;
    readonly origin: string;
    readonly base_revision: number;
    readonly digest: string;
    readonly records: number;
  }
  | {
    readonly action: "commit";
    readonly protocol: 1;
    readonly origin: string;
    readonly epoch: number;
    readonly base_revision: number;
    readonly digest: string;
    readonly records: number;
  }
  | {
    readonly action: "apply";
    readonly protocol: 1;
    readonly origin: string;
    readonly epoch: number;
    readonly changes: readonly LearningRecord[];
  }
  | {
    readonly action: "abort";
    readonly protocol: 1;
    readonly origin: string;
    readonly epoch: number;
  };

export function decodeReconcileRequest(value: unknown): ReconcileRequest | null {
  if (!isRecord(value) || value.protocol !== 1) return null;
  const origin = typeof value.origin === "string" && /^[A-Za-z0-9._-]{1,128}$/.test(value.origin)
    ? value.origin
    : null;
  if (origin === null) return null;
  if (value.action === "begin") {
    if (!keysMatch(value, ["protocol", "origin", "action", "base_revision", "digest", "records"])) return null;
    const baseRevision = decodeCount(value.base_revision);
    const records = decodeCount(value.records);
    const digest = typeof value.digest === "string" && DIGEST.test(value.digest) ? value.digest : null;
    if (baseRevision === null || records === null || digest === null) return null;
    return { action: "begin", protocol: 1, origin, base_revision: baseRevision, digest, records };
  }
  if (value.action === "commit") {
    if (!keysMatch(value, ["protocol", "origin", "action", "epoch", "base_revision", "digest", "records"])) {
      return null;
    }
    const baseRevision = decodeCount(value.base_revision);
    const records = decodeCount(value.records);
    const epoch = decodeCount(value.epoch);
    const digest = typeof value.digest === "string" && DIGEST.test(value.digest) ? value.digest : null;
    if (baseRevision === null || records === null || epoch === null || epoch < 1 || digest === null) return null;
    return {
      action: "commit", protocol: 1, origin, epoch,
      base_revision: baseRevision, digest, records,
    };
  }
  if (value.action === "apply") {
    if (!keysMatch(value, ["protocol", "origin", "action", "epoch", "changes"])) return null;
    const epoch = decodeCount(value.epoch);
    if (epoch === null || epoch < 1) return null;
    const changes = decodeLearningChanges(value.changes, origin, false);
    return changes === null ? null : { action: "apply", protocol: 1, origin, epoch, changes: changes as LearningRecord[] };
  }
  if (value.action === "abort") {
    if (!keysMatch(value, ["protocol", "origin", "action", "epoch"])) return null;
    const epoch = decodeCount(value.epoch);
    return epoch === null || epoch < 1 ? null : { action: "abort", protocol: 1, origin, epoch };
  }
  return null;
}

export async function readLearningState(db: D1Database, origin: string): Promise<LearningState> {
  const row = await db.prepare(
    "SELECT revision, epoch, digest, records FROM learning_state WHERE origin_host = ?",
  ).bind(origin).first<LearningState>();
  if (row === null) return { revision: 0, epoch: 1, digest: null, records: 0 };
  return {
    revision: row.revision,
    epoch: row.epoch,
    digest: row.digest,
    records: row.records,
  };
}

const QUERY_CHUNK = 90;
const EXPERIENCE_ROWS = 4;
const MEMORY_ROWS = 7;
const SOURCE_ROWS = 12;
const LINK_ROWS = 16;
const SEARCH_ROWS = 19;

function chunks<T>(items: readonly T[], size: number): T[][] {
  const result: T[][] = [];
  for (let start = 0; start < items.length; start += size) result.push(items.slice(start, start + size));
  return result;
}

function placeholders(count: number): string {
  return Array.from({ length: count }, () => "?").join(",");
}

async function readExistingIds(
  db: D1Database,
  origin: string,
  epoch: number,
  record: "experience" | "memory",
  ids: readonly number[],
): Promise<Set<number>> {
  const table = record === "experience" ? "learning_experience" : "learning_memory";
  const found = new Set<number>();
  for (const chunk of chunks(ids, QUERY_CHUNK)) {
    const result = await db.prepare(
      `SELECT remote_id FROM ${table} WHERE origin_host = ? AND epoch = ? AND remote_id IN (${placeholders(chunk.length)})`,
    ).bind(origin, epoch, ...chunk).all<{ readonly remote_id: number }>();
    for (const row of result.results) found.add(row.remote_id);
  }
  return found;
}

function searchText(record: LearningRecord): string {
  if (record.record === "experience") {
    return [record.claim, record.applies_when, record.identifiers, record.tags].join("\n");
  }
  return [record.action, record.applicability].join("\n");
}

function childDeletes(
  db: D1Database,
  origin: string,
  epoch: number,
  record: "experience" | "memory",
  ids: readonly number[],
): D1PreparedStatement[] {
  const statements: D1PreparedStatement[] = [];
  for (const chunk of chunks(ids, QUERY_CHUNK)) {
    const list = placeholders(chunk.length);
    statements.push(
      db.prepare(
        `DELETE FROM learning_source WHERE origin_host = ? AND epoch = ? AND record = ? AND remote_id IN (${list})`,
      ).bind(origin, epoch, record, ...chunk),
    );
    statements.push(
      db.prepare(
        `DELETE FROM learning_search WHERE origin_host = ? AND epoch = ? AND record = ? AND remote_id IN (${list})`,
      ).bind(origin, epoch, record, ...chunk),
    );
    if (record === "experience") {
      statements.push(
        db.prepare(
          `DELETE FROM learning_link WHERE origin_host = ? AND epoch = ? AND newer IN (${list})`,
        ).bind(origin, epoch, ...chunk),
        db.prepare(
          `DELETE FROM learning_link WHERE origin_host = ? AND epoch = ? AND older IN (${list})`,
        ).bind(origin, epoch, ...chunk),
      );
    }
  }
  return statements;
}

function recordDeletes(
  db: D1Database,
  origin: string,
  epoch: number,
  record: "experience" | "memory",
  ids: readonly number[],
): D1PreparedStatement[] {
  const table = record === "experience" ? "learning_experience" : "learning_memory";
  const statements: D1PreparedStatement[] = [];
  for (const chunk of chunks(ids, QUERY_CHUNK)) {
    statements.push(
      db.prepare(
        `DELETE FROM ${table} WHERE origin_host = ? AND epoch = ? AND remote_id IN (${placeholders(chunk.length)})`,
      ).bind(origin, epoch, ...chunk),
    );
  }
  return statements;
}

function experienceUpserts(
  db: D1Database,
  origin: string,
  epoch: number,
  revision: number,
  pending: number,
  records: readonly LearningExperience[],
): D1PreparedStatement[] {
  const statements: D1PreparedStatement[] = [];
  for (const chunk of chunks(records, EXPERIENCE_ROWS)) {
    const values = chunk.map(() => `(${placeholders(24)})`).join(",");
    const params: (string | number | null)[] = [];
    for (const record of chunk) {
      params.push(
        origin, epoch, record.id, record.digest, record.project_key, record.project_label, record.kind, record.claim,
        record.applies_when, record.identifiers, record.fingerprint, record.basis, record.detector,
        record.revision, record.worktree, record.observed_at, record.captured_at, record.validity,
        record.tags, record.state, record.state_reason, record.state_at, revision, pending,
      );
    }
    statements.push(
      db.prepare(
        `INSERT INTO learning_experience(
           origin_host, epoch, remote_id, record_digest, project_key, project_label, kind, claim, applies_when,
           identifiers, fingerprint, basis, detector, revision, worktree, observed_at, captured_at,
           validity, tags, state, state_reason, state_at, sync_revision, pending
         ) VALUES ${values}
         ON CONFLICT(origin_host, epoch, remote_id) DO UPDATE SET
           record_digest = excluded.record_digest,
           project_key = excluded.project_key, project_label = excluded.project_label,
           kind = excluded.kind, claim = excluded.claim, applies_when = excluded.applies_when,
           identifiers = excluded.identifiers, fingerprint = excluded.fingerprint,
           basis = excluded.basis, detector = excluded.detector, revision = excluded.revision,
           worktree = excluded.worktree, observed_at = excluded.observed_at,
           captured_at = excluded.captured_at, validity = excluded.validity, tags = excluded.tags,
           state = excluded.state, state_reason = excluded.state_reason, state_at = excluded.state_at,
           sync_revision = excluded.sync_revision, pending = excluded.pending`,
      ).bind(...params),
    );
  }
  return statements;
}

function memoryUpserts(
  db: D1Database,
  origin: string,
  epoch: number,
  revision: number,
  pending: number,
  records: readonly LearningMemory[],
): D1PreparedStatement[] {
  const statements: D1PreparedStatement[] = [];
  for (const chunk of chunks(records, MEMORY_ROWS)) {
    const values = chunk.map(() => `(${placeholders(14)})`).join(",");
    const params: (string | number | null)[] = [];
    for (const record of chunk) {
      params.push(
        origin, epoch, record.id, record.digest, record.project_key, record.project_label, record.action,
        record.applicability, record.revision, record.validity, record.state, record.created_at, revision, pending,
      );
    }
    statements.push(
      db.prepare(
        `INSERT INTO learning_memory(
           origin_host, epoch, remote_id, record_digest, project_key, project_label, action, applicability,
           revision, validity, state, created_at, sync_revision, pending
         ) VALUES ${values}
         ON CONFLICT(origin_host, epoch, remote_id) DO UPDATE SET
           record_digest = excluded.record_digest,
           project_key = excluded.project_key, project_label = excluded.project_label,
           action = excluded.action, applicability = excluded.applicability,
           revision = excluded.revision, validity = excluded.validity, state = excluded.state,
           created_at = excluded.created_at, sync_revision = excluded.sync_revision,
           pending = excluded.pending`,
      ).bind(...params),
    );
  }
  return statements;
}

function sourceInserts(
  db: D1Database,
  origin: string,
  epoch: number,
  records: readonly LearningRecord[],
): D1PreparedStatement[] {
  const rows: { record: "experience" | "memory"; id: number; source: LearningSource }[] = [];
  for (const record of records) {
    for (const source of record.sources) rows.push({ record: record.record, id: record.id, source });
  }
  const statements: D1PreparedStatement[] = [];
  for (const chunk of chunks(rows, SOURCE_ROWS)) {
    const values = chunk.map(() => `(${placeholders(8)})`).join(",");
    const params: (string | number)[] = [];
    for (const row of chunk) {
      params.push(origin, epoch, row.record, row.id, row.source.session, row.source.position,
        row.source.digest, row.source.role);
    }
    statements.push(
      db.prepare(
        `INSERT INTO learning_source(origin_host, epoch, record, remote_id, session, position, digest, role)
         VALUES ${values}`,
      ).bind(...params),
    );
  }
  return statements;
}

function linkInserts(
  db: D1Database,
  origin: string,
  epoch: number,
  records: readonly LearningExperience[],
): D1PreparedStatement[] {
  const rows: { newer: number; link: LearningLink }[] = [];
  for (const record of records) {
    for (const link of record.links) rows.push({ newer: record.id, link });
  }
  const statements: D1PreparedStatement[] = [];
  for (const chunk of chunks(rows, LINK_ROWS)) {
    const values = chunk.map(() => `(${placeholders(5)})`).join(",");
    const params: (string | number)[] = [];
    for (const row of chunk) {
      params.push(origin, epoch, row.newer, row.link.older, row.link.relation);
    }
    statements.push(
      db.prepare(
        `INSERT INTO learning_link(origin_host, epoch, newer, older, relation) VALUES ${values}`,
      ).bind(...params),
    );
  }
  return statements;
}

function searchUpserts(
  db: D1Database,
  origin: string,
  epoch: number,
  records: readonly LearningRecord[],
): D1PreparedStatement[] {
  const statements: D1PreparedStatement[] = [];
  for (const chunk of chunks(records, SEARCH_ROWS)) {
    const values = chunk.map(() => `(${placeholders(5)})`).join(",");
    const params: (string | number)[] = [];
    for (const record of chunk) params.push(origin, epoch, record.record, record.id, searchText(record));
    statements.push(
      db.prepare(
        `INSERT INTO learning_search(origin_host, epoch, record, remote_id, text) VALUES ${values}
         ON CONFLICT(origin_host, epoch, record, remote_id) DO UPDATE SET text = excluded.text`,
      ).bind(...params),
    );
  }
  return statements;
}

async function tombstoneKeys(db: D1Database, origin: string): Promise<Set<string>> {
  const result = await db.prepare(
    "SELECT harness, native_id FROM tombstone WHERE origin_host = ?",
  ).bind(origin).all<{ readonly harness: string; readonly native_id: string }>();
  return new Set(result.results.map((row) => `${row.harness}:${row.native_id}`));
}

function citesTombstone(record: LearningRecord, keys: ReadonlySet<string>): boolean {
  return record.sources.some((source) => {
    const at = source.session.lastIndexOf("@");
    return at > 0 && keys.has(source.session.slice(0, at));
  });
}

export async function applyLearningDelta(
  db: D1Database,
  delta: LearningDelta,
  state: LearningState,
): Promise<LearningApply> {
  if (state.digest !== null && state.digest === delta.digest) {
    return { state, applied: 0, deleted: 0, replayed: true };
  }
  if (delta.base_revision !== state.revision) throw new StaleLearningRevision("base revision is stale");
  const epoch = state.epoch;
  const revision = delta.base_revision + 1;
  const live: LearningRecord[] = [];
  const removed: LearningDeletion[] = [];
  for (const change of delta.changes) {
    if ("deleted" in change) removed.push(change);
    else live.push(change);
  }
  const blocked = await tombstoneKeys(db, delta.origin);
  const rejected = live.filter((record) => citesTombstone(record, blocked));
  const accepted = live.filter((record) => !citesTombstone(record, blocked));
  const liveExperiences = accepted.filter((record): record is LearningExperience => record.record === "experience");
  const liveMemories = accepted.filter((record): record is LearningMemory => record.record === "memory");
  const rejectedExperiences = rejected.filter((record) => record.record === "experience").map((record) => record.id);
  const rejectedMemories = rejected.filter((record) => record.record === "memory").map((record) => record.id);
  const deletedExperiences = removed.filter((change) => change.record === "experience").map((change) => change.id);
  const deletedMemories = removed.filter((change) => change.record === "memory").map((change) => change.id);
  const existingExperiences = await readExistingIds(db, delta.origin, epoch, "experience", [
    ...liveExperiences.map((record) => record.id), ...rejectedExperiences, ...deletedExperiences,
  ]);
  const existingMemories = await readExistingIds(db, delta.origin, epoch, "memory", [
    ...liveMemories.map((record) => record.id), ...rejectedMemories, ...deletedMemories,
  ]);
  let records = state.records;
  for (const id of deletedExperiences) if (existingExperiences.has(id)) records -= 1;
  for (const id of deletedMemories) if (existingMemories.has(id)) records -= 1;
  for (const id of rejectedExperiences) if (existingExperiences.has(id)) records -= 1;
  for (const id of rejectedMemories) if (existingMemories.has(id)) records -= 1;
  for (const record of liveExperiences) if (!existingExperiences.has(record.id)) records += 1;
  for (const record of liveMemories) if (!existingMemories.has(record.id)) records += 1;
  if (records < 0) records = 0;
  const statements: D1PreparedStatement[] = [
    db.prepare(
      `INSERT INTO learning_state(origin_host, revision, epoch, digest, records, updated_at)
       VALUES(?, ?, ?, ?, ?, ?)
       ON CONFLICT(origin_host) DO UPDATE SET
         revision = excluded.revision, digest = excluded.digest,
         records = excluded.records, updated_at = excluded.updated_at
       WHERE learning_state.revision = ?`,
    ).bind(delta.origin, revision, epoch, delta.digest, records, Date.now() / 1000, delta.base_revision),
    db.prepare(
      `INSERT INTO learning_fence(singleton, changed) VALUES(1, changes())
       ON CONFLICT(singleton) DO UPDATE SET changed = excluded.changed`,
    ),
  ];
  statements.push(...recordDeletes(db, delta.origin, epoch, "experience", [...deletedExperiences, ...rejectedExperiences]));
  statements.push(...recordDeletes(db, delta.origin, epoch, "memory", [...deletedMemories, ...rejectedMemories]));
  statements.push(...childDeletes(db, delta.origin, epoch, "experience", [...deletedExperiences, ...rejectedExperiences]));
  statements.push(...childDeletes(db, delta.origin, epoch, "memory", [...deletedMemories, ...rejectedMemories]));
  statements.push(...projectionWrites(db, delta.origin, epoch, revision, 0, accepted));
  await db.batch(statements);
  return {
    state: { revision, epoch, digest: delta.digest, records },
    applied: accepted.length,
    deleted: removed.length + rejected.length,
    replayed: false,
  };
}

function projectionWrites(
  db: D1Database,
  origin: string,
  epoch: number,
  revision: number,
  pending: number,
  records: readonly LearningRecord[],
): D1PreparedStatement[] {
  const experiences = records.filter((record): record is LearningExperience => record.record === "experience");
  const memories = records.filter((record): record is LearningMemory => record.record === "memory");
  return [
    ...childDeletes(db, origin, epoch, "experience", experiences.map((record) => record.id)),
    ...childDeletes(db, origin, epoch, "memory", memories.map((record) => record.id)),
    ...experienceUpserts(db, origin, epoch, revision, pending, experiences),
    ...memoryUpserts(db, origin, epoch, revision, pending, memories),
    ...sourceInserts(db, origin, epoch, records),
    ...linkInserts(db, origin, epoch, experiences),
    ...searchUpserts(db, origin, epoch, records),
  ];
}

function epochDeletes(db: D1Database, origin: string, epoch: number): D1PreparedStatement[] {
  return [
    db.prepare("DELETE FROM learning_source WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
    db.prepare("DELETE FROM learning_search WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
    db.prepare("DELETE FROM learning_link WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
    db.prepare("DELETE FROM learning_experience WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
    db.prepare("DELETE FROM learning_memory WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
  ];
}

async function countEpoch(db: D1Database, origin: string, epoch: number): Promise<number> {
  const row = await db.prepare(
    `SELECT (SELECT count(*) FROM learning_experience WHERE origin_host = ? AND epoch = ?)
          + (SELECT count(*) FROM learning_memory WHERE origin_host = ? AND epoch = ?) AS records`,
  ).bind(origin, epoch, origin, epoch).first<{ readonly records: number }>();
  return row?.records ?? 0;
}

export interface LearningKey {
  readonly record: "experience" | "memory";
  readonly id: number;
  readonly digest: string;
}

export interface LearningKeyPage {
  readonly keys: readonly LearningKey[];
  readonly next_cursor: string | null;
  readonly epoch: number;
  readonly revision: number;
}

export interface ReconcileStage {
  readonly epoch: number;
  readonly digest: string;
  readonly records: number;
  readonly started_at: number;
}

export interface ReconcileBegin {
  readonly epoch: number;
  readonly current: boolean;
  readonly resumed: boolean;
  readonly state: LearningState;
}

export class ReconcileMismatch extends Error {}

export async function readReconcileStage(db: D1Database, origin: string): Promise<ReconcileStage | null> {
  return await db.prepare(
    "SELECT epoch, digest, records, started_at FROM learning_reconcile WHERE origin_host = ?",
  ).bind(origin).first<ReconcileStage>();
}

export async function readLearningKeys(
  db: D1Database,
  origin: string,
  after: { readonly record: "experience" | "memory"; readonly id: number } | null,
  limit: number,
): Promise<LearningKeyPage> {
  const state = await readLearningState(db, origin);
  const keys: LearningKey[] = [];
  if (after === null || after.record === "experience") {
    const lower = after === null ? 0 : after.id;
    const rows = await db.prepare(
      `SELECT 'experience' AS record, remote_id AS id, record_digest AS digest
       FROM learning_experience WHERE origin_host = ? AND epoch = ? AND remote_id > ?
       ORDER BY remote_id LIMIT ?`,
    ).bind(origin, state.epoch, lower, limit + 1).all<LearningKey>();
    keys.push(...rows.results);
  }
  if (after === null || after.record === "memory") {
    const lower = after === null ? 0 : after.id;
    const rows = await db.prepare(
      `SELECT 'memory' AS record, remote_id AS id, record_digest AS digest
       FROM learning_memory WHERE origin_host = ? AND epoch = ? AND remote_id > ?
       ORDER BY remote_id LIMIT ?`,
    ).bind(origin, state.epoch, lower, limit + 1).all<LearningKey>();
    keys.push(...rows.results);
  } else if (after !== null && after.record === "experience") {
    const rows = await db.prepare(
      `SELECT 'memory' AS record, remote_id AS id, record_digest AS digest
       FROM learning_memory WHERE origin_host = ? AND epoch = ?
       ORDER BY remote_id LIMIT ?`,
    ).bind(origin, state.epoch, limit + 1).all<LearningKey>();
    keys.push(...rows.results);
  }
  keys.sort((left, right) => left.record === right.record
    ? left.id - right.id
    : left.record === "experience" ? -1 : 1);
  const page = keys.slice(0, limit);
  const last = page.at(-1);
  return {
    keys: page,
    next_cursor: keys.length > limit && last !== undefined
      ? encodeCursor([last.record, String(last.id)])
      : null,
    epoch: state.epoch,
    revision: state.revision,
  };
}

export async function beginReconcile(
  db: D1Database,
  origin: string,
  baseRevision: number,
  digest: string,
  records: number,
): Promise<ReconcileBegin> {
  const state = await readLearningState(db, origin);
  const stage = await readReconcileStage(db, origin);
  if (state.digest !== null && state.digest === digest) {
    if (stage !== null) await abortReconcile(db, origin, stage.epoch);
    return { epoch: state.epoch, current: true, resumed: false, state };
  }
  if (baseRevision !== state.revision) throw new StaleLearningRevision("base revision is stale");
  if (stage !== null && stage.digest === digest && stage.epoch === state.epoch + 1) {
    return { epoch: stage.epoch, current: false, resumed: true, state };
  }
  const epoch = state.epoch + 1;
  await db.batch([
    ...epochDeletes(db, origin, epoch),
    db.prepare(
      `INSERT INTO learning_reconcile(origin_host, epoch, digest, records, started_at)
       VALUES(?, ?, ?, ?, ?)
       ON CONFLICT(origin_host) DO UPDATE SET
         epoch = excluded.epoch, digest = excluded.digest,
         records = excluded.records, started_at = excluded.started_at`,
    ).bind(origin, epoch, digest, records, Date.now() / 1000),
  ]);
  return { epoch, current: false, resumed: false, state };
}

export async function applyReconcile(
  db: D1Database,
  origin: string,
  epoch: number,
  changes: readonly LearningRecord[],
): Promise<number> {
  const stage = await readReconcileStage(db, origin);
  if (stage === null || stage.epoch !== epoch) {
    throw new ReconcileMismatch("reconciliation is not staged at this epoch");
  }
  const blocked = await tombstoneKeys(db, origin);
  if (changes.some((record) => citesTombstone(record, blocked))) {
    throw new ReconcileMismatch("reconciliation includes a tombstoned source");
  }
  const state = await readLearningState(db, origin);
  await db.batch(projectionWrites(db, origin, epoch, state.revision + 1, 1, changes));
  return await countEpoch(db, origin, epoch);
}

export async function commitReconcile(
  db: D1Database,
  origin: string,
  epoch: number,
  baseRevision: number,
  digest: string,
  records: number,
): Promise<LearningApply> {
  const state = await readLearningState(db, origin);
  const stage = await readReconcileStage(db, origin);
  if (state.digest !== null && state.digest === digest && stage === null) {
    return { state, applied: 0, deleted: 0, replayed: true };
  }
  if (stage === null || stage.epoch !== epoch) {
    throw new ReconcileMismatch("reconciliation is not staged at this epoch");
  }
  if (baseRevision !== state.revision) throw new StaleLearningRevision("base revision is stale");
  if (await countEpoch(db, origin, epoch) !== records) {
    throw new ReconcileMismatch("staged record count does not match the reconciliation");
  }
  const revision = baseRevision + 1;
  const old = state.epoch;
  await db.batch([
    db.prepare(
      `INSERT INTO learning_state(origin_host, revision, epoch, digest, records, updated_at)
       VALUES(?, ?, ?, ?, ?, ?)
       ON CONFLICT(origin_host) DO UPDATE SET
         revision = excluded.revision, epoch = excluded.epoch, digest = excluded.digest,
         records = excluded.records, updated_at = excluded.updated_at
       WHERE learning_state.revision = ?`,
    ).bind(origin, revision, epoch, digest, records, Date.now() / 1000, baseRevision),
    db.prepare(
      `INSERT INTO learning_fence(singleton, changed) VALUES(1, changes())
       ON CONFLICT(singleton) DO UPDATE SET changed = excluded.changed`,
    ),
    ...epochDeletes(db, origin, old),
    db.prepare("UPDATE learning_experience SET pending = 0 WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
    db.prepare("UPDATE learning_memory SET pending = 0 WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
    db.prepare("DELETE FROM learning_reconcile WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
  ]);
  return {
    state: { revision, epoch, digest, records },
    applied: records,
    deleted: 0,
    replayed: false,
  };
}

export async function abortReconcile(db: D1Database, origin: string, epoch: number): Promise<boolean> {
  const stage = await readReconcileStage(db, origin);
  if (stage === null || stage.epoch !== epoch) return false;
  await db.batch([
    ...epochDeletes(db, origin, epoch),
    db.prepare("DELETE FROM learning_reconcile WHERE origin_host = ? AND epoch = ?").bind(origin, epoch),
  ]);
  return true;
}

export function projectionDeletionStatements(
  db: D1Database,
  origin: string,
  epoch: number,
  sessions: readonly string[],
): D1PreparedStatement[] {
  const statements: D1PreparedStatement[] = [];
  for (const chunk of chunks(sessions, 90)) {
    statements.push(
      db.prepare(
        `DELETE FROM learning_source WHERE origin_host = ? AND session IN (${placeholders(chunk.length)})`,
      ).bind(origin, ...chunk),
    );
  }
  const sourceless = (record: "experience" | "memory", target: string, column: string) =>
    `NOT EXISTS (
       SELECT 1 FROM learning_source s
       WHERE s.origin_host = ? AND s.epoch = ? AND s.record = '${record}' AND s.remote_id = ${target}.${column}
     )`;
  statements.push(
    db.prepare(
      `DELETE FROM learning_search
       WHERE origin_host = ? AND epoch = ? AND record = 'experience'
         AND ${sourceless("experience", "learning_search", "remote_id")}`,
    ).bind(origin, epoch, origin, epoch),
  );
  statements.push(
    db.prepare(
      `DELETE FROM learning_search
       WHERE origin_host = ? AND epoch = ? AND record = 'memory'
         AND ${sourceless("memory", "learning_search", "remote_id")}`,
    ).bind(origin, epoch, origin, epoch),
  );
  statements.push(
    db.prepare(
      `DELETE FROM learning_memory
       WHERE origin_host = ? AND epoch = ? AND ${sourceless("memory", "learning_memory", "remote_id")}`,
    ).bind(origin, epoch, origin, epoch),
  );
  statements.push(
    db.prepare(
      `DELETE FROM learning_link
       WHERE origin_host = ? AND epoch = ? AND ${sourceless("experience", "learning_link", "newer")}`,
    ).bind(origin, epoch, origin, epoch),
    db.prepare(
      `DELETE FROM learning_link
       WHERE origin_host = ? AND epoch = ? AND ${sourceless("experience", "learning_link", "older")}`,
    ).bind(origin, epoch, origin, epoch),
  );
  statements.push(
    db.prepare(
      `DELETE FROM learning_experience
       WHERE origin_host = ? AND epoch = ? AND ${sourceless("experience", "learning_experience", "remote_id")}`,
    ).bind(origin, epoch, origin, epoch),
  );
  statements.push(
    db.prepare(
      `UPDATE learning_state
       SET records = (SELECT count(*) FROM learning_experience e
                      WHERE e.origin_host = learning_state.origin_host AND e.epoch = learning_state.epoch)
                   + (SELECT count(*) FROM learning_memory m
                      WHERE m.origin_host = learning_state.origin_host AND m.epoch = learning_state.epoch),
           updated_at = ?
       WHERE origin_host = ?`,
    ).bind(Date.now() / 1000, origin),
  );
  return statements;
}
