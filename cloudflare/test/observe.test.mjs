import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { SpanAttributeError, allowlisted, serverTiming, trace } from "../src/observe.ts";

function recorder() {
  const spans = [];
  return {
    spans,
    tracing: {
      startSpan(name) {
        const span = {
          name,
          attributes: {},
          exceptions: [],
          ended: false,
          setAttributes(attributes) {
            this.attributes = { ...attributes };
            return this;
          },
          recordException(exception) {
            this.exceptions.push(exception);
            return this;
          },
          end() {
            this.ended = true;
          },
        };
        spans.push(span);
        return span;
      },
    },
  };
}

test("span attributes are limited to counts, bytes, durations, status, and revisions", () => {
  assert.deepEqual(allowlisted({ count: 1, bytes: 2, status: 200, revision: 3, duration_ms: 4 }), {
    count: 1, bytes: 2, status: 200, revision: 3, duration_ms: 4,
  });
  for (const key of ["origin", "digest", "claim", "path", "key", "session", "project", "token"]) {
    assert.throws(() => allowlisted({ [key]: "private" }), SpanAttributeError);
  }
});

test("server timing carries durations only", () => {
  assert.equal(serverTiming({ d1: 2.74, r2: 18.42 }), "d1;dur=2.7, r2;dur=18.4");
  assert.equal(serverTiming({ d1: -1, r2: Number.NaN }), "d1;dur=0.0, r2;dur=0.0");
  assert.doesNotMatch(serverTiming({ d1: 2.74, r2: 18.42 }), /origin|digest|claim|path|key|session|project/);
});

test("a traced action records its duration and allowed attributes", async () => {
  const context = recorder();
  const result = await trace(context, "auth", { count: 1 }, async () => "ok");
  assert.equal(result, "ok");
  assert.equal(context.spans[0].name, "auth");
  assert.deepEqual(context.spans[0].attributes, { count: 1 });
  assert.equal(context.spans[0].ended, true);
});

test("a traced failure records no exception text", async () => {
  const context = recorder();
  await assert.rejects(trace(context, "status.d1", {}, async () => {
    throw new Error("claim text at /private/path");
  }));
  assert.deepEqual(context.spans[0].exceptions, [{ name: "WorkerError", message: "operation failed" }]);
});

test("a forbidden attribute aborts before the action and ends the span", async () => {
  const context = recorder();
  let ran = false;
  await assert.rejects(trace(context, "status.d1", { origin: "private" }, async () => {
    ran = true;
    return 1;
  }), SpanAttributeError);
  assert.equal(ran, false);
  assert.equal(context.spans[0].ended, true);
});

test("the config preserves the observability block", () => {
  const config = readFileSync(new URL("../cloudflare.config.ts", import.meta.url), "utf8");
  assert.match(config, /enabled:\s*true/);
  assert.match(config, /invocationLogs:\s*true/);
  assert.match(config, /headSamplingRate:\s*0\.25/);
  assert.match(config, /headSamplingRate:\s*0\.1(?:0)?/);
});
