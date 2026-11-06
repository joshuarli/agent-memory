import type { Principal } from "../auth.js";
import { SECURITY_HEADERS, failure } from "../errors.js";
import { serverTiming } from "../observe.js";
import { MAX_JSON_BYTES, PROTOCOL, isRecord } from "../protocol.js";

export interface SyncTiming {
  d1: number;
  r2: number;
}

export type BodyResult =
  | { readonly ok: true; readonly value: unknown }
  | { readonly ok: false; readonly response: Response };

export type BoundedBody =
  | { readonly ok: true; readonly bytes: Uint8Array }
  | { readonly ok: false; readonly tooLarge: boolean };

export function headers(timing: SyncTiming, extra: Readonly<Record<string, string>> = {}): Headers {
  return new Headers({ ...SECURITY_HEADERS, "Server-Timing": serverTiming(timing), ...extra });
}

export function refuse(
  status: number,
  code: Parameters<typeof failure>[1],
  message: string,
  timing: SyncTiming,
): Response {
  return failure(status, code, message, { "Server-Timing": serverTiming(timing) });
}

export function contentLength(request: Request): number | null {
  const value = request.headers.get("Content-Length");
  if (value === null) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

export async function readBounded(request: Request, limit: number): Promise<BoundedBody> {
  const declared = contentLength(request);
  if (request.headers.get("Content-Length") !== null && declared === null) {
    return { ok: false, tooLarge: false };
  }
  if (declared !== null && declared > limit) return { ok: false, tooLarge: true };
  const reader = request.body?.getReader();
  if (reader === undefined) return { ok: true, bytes: new Uint8Array() };
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel();
      return { ok: false, tooLarge: true };
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { ok: true, bytes };
}

export async function readJson(request: Request, timing: SyncTiming): Promise<BodyResult> {
  const body = await readBounded(request, MAX_JSON_BYTES);
  if (!body.ok) {
    return {
      ok: false,
      response: body.tooLarge
        ? refuse(413, "payload_too_large", "request body exceeds the limit", timing)
        : refuse(400, "invalid_request", "content length is invalid", timing),
    };
  }
  if (body.bytes.byteLength === 0) {
    return { ok: false, response: refuse(400, "invalid_request", "request body is required", timing) };
  }
  try {
    return { ok: true, value: JSON.parse(new TextDecoder().decode(body.bytes)) };
  } catch {
    return { ok: false, response: refuse(400, "invalid_request", "request body is not valid JSON", timing) };
  }
}

export function versionOrError(value: unknown, timing: SyncTiming): Response | null {
  if (!isRecord(value)) return refuse(400, "invalid_request", "request body must be an object", timing);
  if (!Object.hasOwn(value, "protocol")) return refuse(400, "invalid_request", "protocol is required", timing);
  if (value.protocol !== PROTOCOL) return refuse(400, "unsupported_version", "protocol version is not supported", timing);
  return null;
}

export function boundOrigin(principal: Principal, origin: string, timing: SyncTiming): Response | null {
  if (principal.kind !== "collector" || principal.origin_host !== origin) {
    return refuse(403, "forbidden", "origin does not match the collector binding", timing);
  }
  return null;
}
