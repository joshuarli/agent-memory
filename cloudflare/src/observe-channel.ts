import diagnostics from "node:diagnostics_channel";

const channel = diagnostics.channel("stats.observe");

export type Operation = "request.auth" | "request.route" | "sync.replace" | "snapshots.commit" | "generations.commit";

export function markInvocation(context: ExecutionContext | null | undefined): void {
  const tracing: Partial<Tracing> | undefined = context?.tracing;
  if (typeof tracing?.getActiveSpan !== "function") return;
  const span = tracing.getActiveSpan();
  if (span === undefined) return;
  const { traceId, spanId } = span.spanContext();
  channel.publish({ kind: "invocation", traceId, spanId });
}

export function reportError(operation: Operation, error: unknown): void {
  if (!(error instanceof Error)) {
    channel.publish({ kind: "error", operation, name: typeof error, message: "", stack: null });
    return;
  }
  channel.publish({ kind: "error", operation, name: readText(error, "name") ?? "Error", message: readText(error, "message") ?? "", stack: readText(error, "stack") });
}

function readText(error: Error, field: "name" | "message" | "stack"): string | null {
  try {
    const value: unknown = error[field];
    return typeof value === "string" ? value : null;
  } catch {
    return null;
  }
}
