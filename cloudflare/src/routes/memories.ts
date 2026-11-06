import type { Principal } from "../auth.js";
import { json, methodNotAllowed } from "../errors.js";
import {
  decodeSearchFilters,
  idFromPath,
  readRecord,
  recordFromPath,
  searchMemories,
  type MemoryFilters,
} from "../memories.js";
import { serverTiming } from "../observe.js";
import { PROTOCOL } from "../protocol.js";
import type { Env } from "../index.js";
import { refuse, type SyncTiming } from "./shared.js";

const SEARCH_PATH = "/api/v1/memories";
const DETAIL_PREFIX = "/api/v1/memories/";

function bound(filters: MemoryFilters, principal: Principal, timing: SyncTiming): MemoryFilters | Response {
  if (principal.kind !== "collector") return filters;
  if (filters.origin !== null && filters.origin !== principal.origin_host) {
    return refuse(403, "forbidden", "origin does not match the collector binding", timing);
  }
  return { ...filters, origin: principal.origin_host };
}

async function search(
  request: Request,
  env: Env,
  principal: Principal,
  url: URL,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return methodNotAllowed(["GET", "HEAD"], { "Server-Timing": serverTiming(timing) });
  }
  const decoded = decodeSearchFilters(url);
  if (decoded === null) return refuse(400, "invalid_request", "search filters are invalid", timing);
  const filters = bound(decoded, principal, timing);
  if (filters instanceof Response) return filters;
  const started = performance.now();
  let page;
  try {
    page = await searchMemories(env.DB, filters);
  } finally {
    timing.d1 += performance.now() - started;
  }
  return json(200, {
    protocol: PROTOCOL,
    memories: page.memories,
    next_cursor: page.next_cursor,
  }, request.method === "HEAD", { "Server-Timing": serverTiming(timing) });
}

async function detail(
  request: Request,
  env: Env,
  principal: Principal,
  origin: string,
  record: "experience" | "memory",
  id: number,
  timing: SyncTiming,
): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return methodNotAllowed(["GET", "HEAD"], { "Server-Timing": serverTiming(timing) });
  }
  if (principal.kind === "collector" && principal.origin_host !== origin) {
    return refuse(403, "forbidden", "origin does not match the collector binding", timing);
  }
  const started = performance.now();
  let detail: Awaited<ReturnType<typeof readRecord>>;
  try {
    detail = await readRecord(env.DB, origin, record, id);
  } finally {
    timing.d1 += performance.now() - started;
  }
  if (detail === null) return refuse(404, "not_found", "memory not found", timing);
  return json(200, {
    protocol: PROTOCOL,
    memory: detail.memory,
    sources: detail.sources,
    links: detail.links,
  }, request.method === "HEAD", { "Server-Timing": serverTiming(timing) });
}

export async function handleMemories(
  request: Request,
  env: Env,
  principal: Principal,
  pathname: string,
  url: URL,
  timing: SyncTiming,
): Promise<Response | null> {
  if (pathname === SEARCH_PATH) return await search(request, env, principal, url, timing);
  if (!pathname.startsWith(DETAIL_PREFIX)) return null;
  const parts = pathname.slice(DETAIL_PREFIX.length).split("/");
  if (parts.length !== 3) return refuse(404, "not_found", "route not found", timing);
  const [originPart, recordPart, idPart] = parts;
  if (originPart === undefined || recordPart === undefined || idPart === undefined) {
    return refuse(404, "not_found", "route not found", timing);
  }
  const origin = /^[A-Za-z0-9._-]{1,128}$/.test(originPart) ? originPart : null;
  const record = recordFromPath(recordPart);
  const id = idFromPath(idPart);
  if (origin === null || record === null || id === null) {
    return refuse(404, "not_found", "route not found", timing);
  }
  return await detail(request, env, principal, origin, record, id, timing);
}
