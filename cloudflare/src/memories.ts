import { decodeCursor, encodeCursor } from "./protocol.js";

export const MAX_SEARCH_LIMIT = 100;
export const DEFAULT_SEARCH_LIMIT = 20;
export const MAX_QUERY_CHARS = 512;
export const MAX_QUERY_TERMS = 16;

const EXPERIENCE_KINDS = new Set(["success", "failure", "decision", "gotcha", "method"]);
const EXPERIENCE_STATES = new Set(["retained", "retired", "superseded", "withdrawn"]);
const MEMORY_STATES = new Set(["approved", "superseded"]);
const STATES = new Set([...EXPERIENCE_STATES, ...MEMORY_STATES, "all"]);
const RECORD_TYPES = new Set(["experience", "memory", "all"]);
const PROJECT_KEY = /^[0-9a-f]{16}$/;
const ORIGIN = /^[A-Za-z0-9._-]{1,128}$/;
const CURSOR_NUMBER = /^(0|[1-9][0-9]{0,15})(\.[0-9]{1,9})?$/;
const CURSOR_ID = /^(0|[1-9][0-9]{0,15})$/;

export type RecordType = "experience" | "memory" | "all";
export type StateFilter =
  | "all"
  | "retained"
  | "retired"
  | "superseded"
  | "withdrawn"
  | "approved"
  | null;

export interface SearchCursor {
  readonly sortAt: number;
  readonly record: "experience" | "memory";
  readonly id: number;
}

export interface MemoryFilters {
  readonly q: string | null;
  readonly project: string | null;
  readonly origin: string | null;
  readonly type: RecordType;
  readonly kind: string | null;
  readonly state: StateFilter;
  readonly since: number | null;
  readonly until: number | null;
  readonly after: SearchCursor | null;
  readonly limit: number;
}

export interface SearchRow {
  readonly record: "experience" | "memory";
  readonly id: number;
  readonly origin: string;
  readonly project_key: string;
  readonly project_label: string | null;
  readonly kind: string | null;
  readonly claim: string | null;
  readonly applies_when: string | null;
  readonly action: string | null;
  readonly applicability: string | null;
  readonly basis: string | null;
  readonly state: string;
  readonly sort_at: number;
}

export interface MemoryResult {
  readonly record: "experience" | "memory";
  readonly id: number;
  readonly origin: string;
  readonly project_key: string;
  readonly project_label: string | null;
  readonly kind: string | null;
  readonly claim: string | null;
  readonly applies_when: string | null;
  readonly action: string | null;
  readonly applicability: string | null;
  readonly basis: string | null;
  readonly state: string;
  readonly sort_at: number;
  readonly marker: "Reviewed" | "Observed";
  readonly source_count: number;
}

export interface SearchPage {
  readonly memories: readonly MemoryResult[];
  readonly next_cursor: string | null;
}

export interface LearningSourceRow {
  readonly record: "experience" | "memory";
  readonly remote_id: number;
  readonly session: string;
  readonly position: number;
  readonly digest: string;
  readonly role: string;
}

export interface LinkRow {
  readonly direction: "incoming" | "outgoing";
  readonly other: number;
  readonly relation: string;
  readonly claim: string | null;
  readonly state: string | null;
}

export interface RecordDetail {
  readonly memory: MemoryResult;
  readonly sources: readonly LearningSourceRow[];
  readonly links: readonly LinkRow[];
}

function numberParameter(value: string | null): number | null | undefined {
  if (value === null || value === "") return null;
  if (!CURSOR_NUMBER.test(value)) return undefined;
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}

export function ftsExpression(value: string): string | null {
  const terms = value.split(/\s+/).filter((term) => term !== "");
  if (terms.length === 0 || terms.length > MAX_QUERY_TERMS) return null;
  let total = 0;
  const quoted: string[] = [];
  for (const term of terms) {
    if (term.length > 128 || term.includes('"')) return null;
    total += term.length;
    quoted.push(`"${term}"*`);
  }
  return total > MAX_QUERY_CHARS ? null : quoted.join(" AND ");
}

function decodeSearchCursor(value: string): SearchCursor | null {
  const parts = decodeCursor(value);
  if (parts === null || parts.length !== 3) return null;
  const [time, record, id] = parts;
  if (time === undefined || record === undefined || id === undefined) return null;
  if (!CURSOR_NUMBER.test(time) || !CURSOR_ID.test(id)) return null;
  if (record !== "experience" && record !== "memory") return null;
  const sortAt = Number(time);
  const identifier = Number(id);
  if (!Number.isFinite(sortAt) || !Number.isSafeInteger(identifier)) return null;
  return { sortAt, record, id: identifier };
}

export function decodeSearchFilters(url: URL): MemoryFilters | null {
  const query = url.searchParams.get("q");
  const project = url.searchParams.get("project");
  const origin = url.searchParams.get("origin");
  const type = url.searchParams.get("type") ?? "all";
  const kind = url.searchParams.get("kind");
  const state = url.searchParams.get("state");
  const since = numberParameter(url.searchParams.get("since"));
  const until = numberParameter(url.searchParams.get("until"));
  const limitValue = url.searchParams.get("limit");
  const afterValue = url.searchParams.get("after");
  if (since === undefined || until === undefined) return null;
  if (since !== null && until !== null && since > until) return null;
  if (project !== null && !PROJECT_KEY.test(project)) return null;
  if (origin !== null && !ORIGIN.test(origin)) return null;
  if (!RECORD_TYPES.has(type)) return null;
  if (kind !== null && !EXPERIENCE_KINDS.has(kind)) return null;
  if (kind !== null && type === "memory") return null;
  if (state !== null && !STATES.has(state)) return null;
  const included: ("experience" | "memory")[] = type === "all"
    ? ["experience", "memory"]
    : [type as "experience" | "memory"];
  const filterState = state as StateFilter;
  if (!included.some((record) => {
    const allowed = statesFor(filterState, record);
    return allowed === null || allowed.length > 0;
  })) return null;
  const limit = limitValue === null ? DEFAULT_SEARCH_LIMIT : Number(limitValue);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_SEARCH_LIMIT) return null;
  const q = query === null || query === "" ? null : query;
  if (q !== null && (q.length > MAX_QUERY_CHARS || ftsExpression(q) === null)) return null;
  let after: SearchCursor | null = null;
  if (afterValue !== null) {
    after = decodeSearchCursor(afterValue);
    if (after === null) return null;
  }
  return {
    q, project, origin, type: type as RecordType,
    kind, state: state as StateFilter,
    since, until, after, limit,
  };
}

function statesFor(state: StateFilter, record: "experience" | "memory"): readonly string[] | null {
  if (state === "all") return null;
  if (state === null) return record === "experience" ? ["retained"] : ["approved"];
  const allowed = record === "experience" ? EXPERIENCE_STATES : MEMORY_STATES;
  return allowed.has(state) ? [state] : [];
}

function branch(record: "experience" | "memory", filters: MemoryFilters): {
  readonly sql: string;
  readonly params: (string | number)[];
} | null {
  const states = statesFor(filters.state, record);
  if (states !== null && states.length === 0) return null;
  const table = record === "experience" ? "learning_experience" : "learning_memory";
  const alias = record === "experience" ? "e" : "m";
  const time = record === "experience" ? "observed_at" : "created_at";
  const params: (string | number)[] = [];
  const conditions: string[] = [];
  if (filters.origin !== null) {
    conditions.push(`${alias}.origin_host = ?`);
    params.push(filters.origin);
  }
  if (filters.project !== null) {
    conditions.push(`${alias}.project_key = ?`);
    params.push(filters.project);
  }
  if (record === "experience" && filters.kind !== null) {
    conditions.push(`${alias}.kind = ?`);
    params.push(filters.kind);
  }
  if (states !== null) {
    conditions.push(`${alias}.state IN (${states.map(() => "?").join(",")})`);
    params.push(...states);
  }
  if (filters.since !== null) {
    conditions.push(`${alias}.${time} >= ?`);
    params.push(filters.since);
  }
  if (filters.until !== null) {
    conditions.push(`${alias}.${time} <= ?`);
    params.push(filters.until);
  }
  if (filters.after !== null) {
    if (filters.after.record === record) {
      conditions.push(`(${alias}.${time}, ${alias}.remote_id) < (?, ?)`);
      params.push(filters.after.sortAt, filters.after.id);
    } else if (filters.after.record === "memory" && record === "experience") {
      conditions.push(`${alias}.${time} <= ?`);
      params.push(filters.after.sortAt);
    } else {
      conditions.push(`${alias}.${time} < ?`);
      params.push(filters.after.sortAt);
    }
  }
  const where = conditions.length === 0 ? "" : ` AND ${conditions.join(" AND ")}`;
  const matches = filters.q === null
    ? ""
    : ` JOIN matches hit ON hit.origin_host = ${alias}.origin_host AND hit.epoch = ${alias}.epoch
          AND hit.record = '${record}' AND hit.remote_id = ${alias}.remote_id`;
  const columns = record === "experience"
    ? `'experience' AS record, e.remote_id AS id, e.origin_host AS origin, e.project_key, e.project_label,
       e.kind, e.claim, e.applies_when, NULL AS action, NULL AS applicability, e.basis, e.state,
       e.observed_at AS sort_at`
    : `'memory' AS record, m.remote_id AS id, m.origin_host AS origin, m.project_key, m.project_label,
       NULL AS kind, NULL AS claim, NULL AS applies_when, m.action, m.applicability, NULL AS basis,
       m.state, m.created_at AS sort_at`;
  const sql = `SELECT ${columns}
     FROM ${table} ${alias}
     JOIN learning_state ls ON ls.origin_host = ${alias}.origin_host AND ls.epoch = ${alias}.epoch${matches}
     WHERE 1 = 1${where}
     ORDER BY ${alias}.${time} DESC, ${alias}.remote_id DESC
     LIMIT ?`;
  params.push(filters.limit + 1);
  return { sql, params };
}

export function searchStatement(filters: MemoryFilters): {
  readonly sql: string;
  readonly params: (string | number)[];
} {
  const expression = filters.q === null ? null : ftsExpression(filters.q);
  const branches: { readonly sql: string; readonly params: (string | number)[] }[] = [];
  if (filters.type !== "memory") {
    const experience = branch("experience", filters);
    if (experience !== null) branches.push(experience);
  }
  if (filters.type !== "experience" && filters.kind === null) {
    const memory = branch("memory", filters);
    if (memory !== null) branches.push(memory);
  }
  const params: (string | number)[] = [];
  if (expression !== null) params.push(expression);
  for (const item of branches) params.push(...item.params);
  params.push(filters.limit + 1);
  const body = branches.map((item) => `SELECT * FROM (${item.sql})`).join(" UNION ALL ");
  const matches = expression === null
    ? ""
    : `WITH matches AS (
         SELECT s.origin_host, s.epoch, s.record, s.remote_id
         FROM memory_fts JOIN learning_search s ON s.rowid = memory_fts.rowid
         WHERE memory_fts MATCH ?
       )
`;
  return {
    sql: `${matches}SELECT * FROM (${body})
          ORDER BY sort_at DESC, record DESC, id DESC LIMIT ?`,
    params,
  };
}

function result(row: SearchRow, sourceCount = 0): MemoryResult {
  return {
    record: row.record, id: row.id, origin: row.origin,
    project_key: row.project_key, project_label: row.project_label,
    kind: row.kind, claim: row.claim, applies_when: row.applies_when,
    action: row.action, applicability: row.applicability, basis: row.basis,
    state: row.state, sort_at: row.sort_at,
    marker: row.record === "memory" || row.basis === "reviewed" ? "Reviewed" : "Observed",
    source_count: sourceCount,
  };
}

async function pageSources(
  db: D1Database,
  rows: readonly SearchRow[],
): Promise<Map<string, LearningSourceRow[]>> {
  const provided = new Map<string, LearningSourceRow[]>();
  if (rows.length === 0) return provided;
  const GROUP = 33;
  for (let start = 0; start < rows.length; start += GROUP) {
    const chunk = rows.slice(start, start + GROUP);
    const params: (string | number)[] = [];
    for (const row of chunk) params.push(row.origin, row.record, row.id);
    const result = await db.prepare(
      `SELECT s.record, s.remote_id, s.session, s.position, s.digest, s.role
       FROM learning_source s
       JOIN learning_state ls ON ls.origin_host = s.origin_host AND ls.epoch = s.epoch
       WHERE (s.origin_host, s.record, s.remote_id) IN (${chunk.map(() => "(?, ?, ?)").join(",")})
       ORDER BY s.record, s.remote_id, s.session, s.position, s.role`,
    ).bind(...params).all<LearningSourceRow>();
    for (const source of result.results) {
      const key = `${source.record}:${source.remote_id}`;
      const list = provided.get(key) ?? [];
      list.push(source);
      provided.set(key, list);
    }
  }
  return provided;
}

export async function searchMemories(
  db: D1Database,
  filters: MemoryFilters,
): Promise<SearchPage> {
  const statement = searchStatement(filters);
  const rows = (await db.prepare(statement.sql).bind(...statement.params).all<SearchRow>()).results;
  const page = rows.slice(0, filters.limit);
  const sources = await pageSources(db, page);
  const memories = page.map((row) => result(row, sources.get(`${row.record}:${row.id}`)?.length ?? 0));
  const last = page.at(-1);
  const next_cursor = rows.length > filters.limit && last !== undefined
    ? encodeCursor([String(last.sort_at), last.record, String(last.id)])
    : null;
  return { memories, next_cursor };
}

export async function readRecord(
  db: D1Database,
  origin: string,
  record: "experience" | "memory",
  id: number,
): Promise<RecordDetail | null> {
  const state = await db.prepare(
    "SELECT epoch FROM learning_state WHERE origin_host = ?",
  ).bind(origin).first<{ readonly epoch: number }>();
  if (state === null) return null;
  const table = record === "experience" ? "learning_experience" : "learning_memory";
  const alias = record === "experience" ? "e" : "m";
  const columns = record === "experience"
    ? `e.remote_id AS id, e.origin_host AS origin, e.project_key, e.project_label, e.kind, e.claim,
       e.applies_when, NULL AS action, NULL AS applicability, e.basis, e.state, e.observed_at AS sort_at`
    : `m.remote_id AS id, m.origin_host AS origin, m.project_key, m.project_label, NULL AS kind, NULL AS claim,
       NULL AS applies_when, m.action, m.applicability, NULL AS basis, m.state, m.created_at AS sort_at`;
  const row = await db.prepare(
    `SELECT ${columns} FROM ${table} ${alias}
     WHERE ${alias}.origin_host = ? AND ${alias}.epoch = ? AND ${alias}.remote_id = ?`,
  ).bind(origin, state.epoch, id).first<SearchRow>();
  if (row === null) return null;
  const sources = await db.prepare(
    `SELECT record, remote_id, session, position, digest, role FROM learning_source
     WHERE origin_host = ? AND epoch = ? AND record = ? AND remote_id = ?
     ORDER BY session, position, role`,
  ).bind(origin, state.epoch, record, id).all<LearningSourceRow>();
  const links = record === "experience"
    ? await db.prepare(
      `SELECT 'outgoing' AS direction, l.older AS other, l.relation, e.claim, e.state
       FROM learning_link l
       LEFT JOIN learning_experience e
         ON e.origin_host = l.origin_host AND e.epoch = l.epoch AND e.remote_id = l.older
       WHERE l.origin_host = ? AND l.epoch = ? AND l.newer = ?
       UNION ALL
       SELECT 'incoming' AS direction, l.newer AS other, l.relation, e.claim, e.state
       FROM learning_link l
       LEFT JOIN learning_experience e
         ON e.origin_host = l.origin_host AND e.epoch = l.epoch AND e.remote_id = l.newer
       WHERE l.origin_host = ? AND l.epoch = ? AND l.older = ?
       ORDER BY direction, other, relation`,
    ).bind(origin, state.epoch, id, origin, state.epoch, id).all<LinkRow>()
    : { results: [] as LinkRow[] };
  return {
    memory: result({ ...row, record } as SearchRow, sources.results.length),
    sources: sources.results,
    links: links.results,
  };
}

export function recordFromPath(value: string): "experience" | "memory" | null {
  return value === "experience" || value === "memory" ? value : null;
}

export function idFromPath(value: string): number | null {
  return CURSOR_ID.test(value) ? Number(value) : null;
}
