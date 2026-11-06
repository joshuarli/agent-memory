import diagnostics from "node:diagnostics_channel";

const channel = diagnostics.channel("stats.observe");

export const MEMORIES_OPERATIONS = [
  "request.auth", "request.route", "sync.replace", "snapshots.commit", "generations.commit",
  "sync.hello", "sync.missing", "sync.upload", "snapshots.page", "snapshots.verify", "snapshots.gc",
  "tombstones.commit", "learning.state", "learning.delta", "learning.reconcile", "memories.search",
  "status.read", "mcp.request"
] as const;

export type MemoriesOperation = typeof MEMORIES_OPERATIONS[number];
export type Operation = "request.auth" | "request.route" | "sync.replace" | "snapshots.commit" | "generations.commit";

export interface RowMeter {
  read: number;
  written: number;
  statements: number;
}

function safePublish(message: unknown): void {
  try {
    channel.publish(message);
  } catch {
    return;
  }
}

function spanOf(context: ExecutionContext | null | undefined): { traceId: string; spanId: string } | null {
  const tracing: Partial<Tracing> | undefined = context?.tracing;
  if (typeof tracing?.getActiveSpan !== "function") return null;
  const span = tracing.getActiveSpan();
  if (span === undefined) return null;
  const { traceId, spanId } = span.spanContext();
  return { traceId, spanId };
}

export function markInvocation(context: ExecutionContext | null | undefined): void {
  const span = spanOf(context);
  if (span === null) return;
  safePublish({ kind: "invocation", ...span });
}

export function markOperation(
  context: ExecutionContext | null | undefined,
  operation: MemoriesOperation,
  rows: RowMeter,
): void {
  const span = spanOf(context);
  if (span === null) return;
  safePublish({
    kind: "operation",
    ...span,
    operation,
    synthetic: false,
    rows: { read: rows.read, written: rows.written, statements: rows.statements },
  });
}

export function operationFor(pathname: string, method: string): MemoriesOperation | null {
  if (pathname === "/mcp") return "mcp.request";
  if (pathname === "/api/v1/status" || pathname === "/status") return "status.read";
  if (pathname === "/" || pathname === "/search" || pathname.startsWith("/m/") ||
      pathname === "/api/v1/memories" || pathname.startsWith("/api/v1/memories/")) return "memories.search";
  if (pathname === "/api/v1/sync/hello") return "sync.hello";
  if (pathname === "/api/v1/sync/objects/missing") return "sync.missing";
  if (/^\/api\/v1\/sync\/objects\/[^/]+\/replace$/u.test(pathname)) return "sync.replace";
  if (pathname.startsWith("/api/v1/sync/objects/") || pathname.startsWith("/api/v1/sync/manifests/")) return "sync.upload";
  if (pathname === "/api/v1/sync/generations") return "generations.commit";
  if (pathname === "/api/v1/sync/gc") return "snapshots.gc";
  if (pathname === "/api/v1/sync/snapshots") return method === "POST" ? "snapshots.commit" : "snapshots.page";
  if (pathname.startsWith("/api/v1/sync/snapshots/") && pathname.endsWith("/verify")) return "snapshots.verify";
  if (pathname.startsWith("/api/v1/sync/snapshots/")) return "snapshots.page";
  if (pathname === "/api/v1/sync/tombstones") return "tombstones.commit";
  if (pathname === "/api/v1/sync/learning/state" || pathname === "/api/v1/sync/learning/keys") return "learning.state";
  if (pathname === "/api/v1/sync/learning/deltas") return "learning.delta";
  if (pathname === "/api/v1/sync/learning/reconcile") return "learning.reconcile";
  return null;
}

export function reportError(operation: Operation, error: unknown): void {
  if (!(error instanceof Error)) {
    safePublish({ kind: "error", operation, name: typeof error, message: "", stack: null });
    return;
  }
  safePublish({
    kind: "error",
    operation,
    name: readText(error, "name") ?? "Error",
    message: readText(error, "message") ?? "",
    stack: readText(error, "stack"),
  });
}

function readText(error: Error, field: "name" | "message" | "stack"): string | null {
  try {
    const value: unknown = error[field];
    return typeof value === "string" ? value : null;
  } catch {
    return null;
  }
}
