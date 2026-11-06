export const SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
} as const;

export type ErrorCode =
  | "forbidden"
  | "not_found"
  | "method_not_allowed"
  | "unavailable"
  | "invalid_request"
  | "unsupported_version"
  | "payload_too_large"
  | "conflict"
  | "stale_revision"
  | "missing_object"
  | "tombstoned";

interface ErrorEnvelope {
  readonly error: {
    readonly code: ErrorCode;
    readonly message: string;
  };
}

function responseHeaders(extra: Readonly<Record<string, string>>): Headers {
  return new Headers({ ...SECURITY_HEADERS, ...extra });
}

export function json(status: number, body: unknown, head = false, extra: Readonly<Record<string, string>> = {}): Response {
  return new Response(head ? null : JSON.stringify(body), {
    status,
    headers: responseHeaders({ "Content-Type": "application/json; charset=utf-8", ...extra }),
  });
}

export function failure(status: number, code: ErrorCode, message: string, extra: Readonly<Record<string, string>> = {}): Response {
  const body: ErrorEnvelope = { error: { code, message } };
  return json(status, body, false, extra);
}

export function methodNotAllowed(allow: readonly string[], extra: Readonly<Record<string, string>> = {}): Response {
  return failure(405, "method_not_allowed", "method not allowed", { Allow: allow.join(", "), ...extra });
}
