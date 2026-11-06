export const ALLOWED_SPAN_ATTRIBUTES: ReadonlySet<string> = new Set([
  "status",
  "count",
  "bytes",
  "duration_ms",
  "revision",
]);

export type SpanAttributes = Readonly<Record<string, number | string | boolean>>;

export class SpanAttributeError extends Error {}

export interface Timing {
  readonly d1: number;
  readonly r2: number;
}

export function allowlisted(attributes: SpanAttributes): SpanAttributes {
  for (const key of Object.keys(attributes)) {
    if (!ALLOWED_SPAN_ATTRIBUTES.has(key)) {
      throw new SpanAttributeError("span attribute is not on the allowlist");
    }
  }
  return attributes;
}

function duration(value: number): string {
  return (Number.isFinite(value) && value > 0 ? value : 0).toFixed(1);
}

export function serverTiming(timing: Timing): string {
  return `d1;dur=${duration(timing.d1)}, r2;dur=${duration(timing.r2)}`;
}

export async function trace<T>(
  context: ExecutionContext,
  name: string,
  attributes: SpanAttributes,
  action: () => Promise<T>,
): Promise<T> {
  if (context === null || context === undefined) return await action();
  const tracing = context.tracing;
  if (tracing === undefined) return await action();
  let span: Span | undefined;
  try {
    span = tracing.startSpan(name);
    span.setAttributes(allowlisted(attributes));
  } catch (error) {
    span?.end();
    if (error instanceof SpanAttributeError) throw error;
    span = undefined;
  }
  try {
    return await action();
  } catch (error) {
    span?.recordException({ name: "WorkerError", message: "operation failed" });
    throw error;
  } finally {
    span?.end();
  }
}
