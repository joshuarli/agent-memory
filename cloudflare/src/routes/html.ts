import type { Principal } from "../auth.js";
import {
  decodeSearchFilters,
  idFromPath,
  readRecord,
  recordFromPath,
  searchMemories,
  type MemoryFilters,
} from "../memories.js";
import { serverTiming } from "../observe.js";
import {
  renderDetail,
  renderLanding,
  renderSearch,
  renderStatus,
  type SearchFormValues,
} from "../render.js";
import { readCounts, readStatus } from "./status.js";
import type { Env } from "../index.js";
import { type SyncTiming } from "./shared.js";

const CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

function html(body: string, status: number, timing: SyncTiming, head = false): Response {
  return new Response(head ? null : body, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
      "Content-Security-Policy": CSP,
      "Server-Timing": serverTiming(timing),
    },
  });
}

function recentFilters(limit: number): MemoryFilters {
  return {
    q: null, project: null, origin: null, type: "all", kind: null, state: null,
    since: null, until: null, after: null, limit,
  };
}

function formValues(filters: MemoryFilters): SearchFormValues {
  return {
    q: filters.q,
    project: filters.project,
    origin: filters.origin,
    type: filters.type,
    kind: filters.kind,
    state: filters.state,
  };
}

async function landing(env: Env, timing: SyncTiming, head: boolean): Promise<Response> {
  const started = performance.now();
  let counts: Awaited<ReturnType<typeof readCounts>>;
  let recent: Awaited<ReturnType<typeof searchMemories>>;
  try {
    counts = await readCounts(env.DB);
    recent = await searchMemories(env.DB, recentFilters(10));
  } finally {
    timing.d1 += performance.now() - started;
  }
  return html(renderLanding(counts, recent.memories), 200, timing, head);
}

async function search(url: URL, env: Env, timing: SyncTiming, head: boolean): Promise<Response> {
  const cleaned = new URL(url.href);
  for (const key of ["q", "project", "origin", "type", "kind", "state"]) {
    if (cleaned.searchParams.get(key) === "") cleaned.searchParams.delete(key);
  }
  if (cleaned.searchParams.get("limit") === null) cleaned.searchParams.set("limit", "20");
  const filters = decodeSearchFilters(cleaned);
  if (filters === null) {
    return html(renderSearch({
      q: url.searchParams.get("q"), project: url.searchParams.get("project"),
      origin: url.searchParams.get("origin"), type: url.searchParams.get("type") ?? "all",
      kind: url.searchParams.get("kind"), state: url.searchParams.get("state"),
    }, [], null), 400, timing, head);
  }
  const started = performance.now();
  let page: Awaited<ReturnType<typeof searchMemories>>;
  try {
    page = await searchMemories(env.DB, filters);
  } finally {
    timing.d1 += performance.now() - started;
  }
  return html(renderSearch(formValues(filters), page.memories, page.next_cursor), 200, timing, head);
}

async function detail(
  pathname: string,
  env: Env,
  timing: SyncTiming,
  head: boolean,
): Promise<Response> {
  const parts = pathname.slice("/m/".length).split("/");
  if (parts.length !== 3) return html(renderSearch({
    q: null, project: null, origin: null, type: "all", kind: null, state: null,
  }, [], null), 404, timing, head);
  const [originPart, recordPart, idPart] = parts;
  if (originPart === undefined || recordPart === undefined || idPart === undefined) {
    return html("Not found", 404, timing, head);
  }
  const origin = /^[A-Za-z0-9._-]{1,128}$/.test(originPart) ? originPart : null;
  const record = recordFromPath(recordPart);
  const id = idFromPath(idPart);
  if (origin === null || record === null || id === null) return html("Not found", 404, timing, head);
  const started = performance.now();
  let found;
  try {
    found = await readRecord(env.DB, origin, record, id);
  } finally {
    timing.d1 += performance.now() - started;
  }
  if (found === null) return html("Not found", 404, timing, head);
  return html(renderDetail(found), 200, timing, head);
}

async function status(env: Env, principal: Principal, timing: SyncTiming, head: boolean): Promise<Response> {
  const started = performance.now();
  let data: Awaited<ReturnType<typeof readStatus>>;
  try {
    data = await readStatus(env.DB);
  } finally {
    timing.d1 += performance.now() - started;
  }
  if (data === null) return html("Status unavailable", 503, timing, head);
  const name = principal.kind === "reader" ? principal.email : principal.origin_host;
  return html(renderStatus(name, data), 200, timing, head);
}

export async function handleHtml(
  request: Request,
  env: Env,
  principal: Principal,
  pathname: string,
  url: URL,
  timing: SyncTiming,
): Promise<Response | null> {
  const isPage = pathname === "/" || pathname === "/search" || pathname === "/status"
    || pathname.startsWith("/m/");
  if (!isPage) return null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    return html("Method not allowed", 405, timing);
  }
  if (principal.kind !== "reader") {
    return html("A reader identity is required to browse memories.", 403, timing);
  }
  const head = request.method === "HEAD";
  if (pathname === "/") return await landing(env, timing, head);
  if (pathname === "/search") return await search(url, env, timing, head);
  if (pathname === "/status") return await status(env, principal, timing, head);
  return await detail(pathname, env, timing, head);
}
