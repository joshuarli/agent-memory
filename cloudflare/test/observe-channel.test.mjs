import assert from "node:assert/strict";
import diagnostics from "node:diagnostics_channel";
import test from "node:test";

import { environment, loadWorker, request } from "./harness.mjs";

const worker = await loadWorker();
const SPAN = { traceId: "7bcf42a3d155dee9563e537e144790e4", spanId: "1b1e95b0d4dcde81" };

function published() {
  const messages = [];
  const listener = message => messages.push(message);
  diagnostics.subscribe("stats.observe", listener);
  return { messages, stop: () => diagnostics.unsubscribe("stats.observe", listener) };
}

function tracedContext(identity) {
  return {
    access: { getIdentity: () => identity },
    tracing: { getActiveSpan: () => ({ spanContext: () => SPAN }), startSpan: () => ({ setAttributes() { return this; }, recordException() {}, end() {} }) },
  };
}

test("every request publishes its span context before anything else", async () => {
  const channel = published();
  try {
    const { env } = environment();
    await worker.fetch(request("/api/v1/status"), env, tracedContext({ email: "stranger@example.test" }));
    assert.deepEqual(channel.messages[0], { kind: "invocation", ...SPAN });
  } finally {
    channel.stop();
  }
});

test("an authentication failure answered with 503 is reported with its operation and stack", async () => {
  const channel = published();
  try {
    const { env } = environment({}, { dbFailure: "query", collectors: { "token-id": { origin_host: "origin-a" } } });
    const response = await worker.fetch(request("/api/v1/status"), env, tracedContext({ service_token_id: "token-id" }));
    assert.equal(response.status, 503);
    const report = channel.messages.find(message => message.kind === "error");
    assert.equal(report.operation, "request.auth");
    assert.equal(report.name, "Error");
    assert.equal(report.message, "d1 query failed");
    assert.match(report.stack, /d1 query failed/u);
    assert.deepEqual(Object.keys(report).sort(), ["kind", "message", "name", "operation", "stack"]);
  } finally {
    channel.stop();
  }
});

test("a request without tracing still answers and publishes no mark", async () => {
  const channel = published();
  try {
    const { env } = environment();
    const response = await worker.fetch(request("/api/v1/status"), env, { access: { getIdentity: () => ({ email: "stranger@example.test" }) } });
    assert.equal(response.status, 403);
    assert.deepEqual(channel.messages, []);
  } finally {
    channel.stop();
  }
});
