#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { startEdge } from "./edge.mjs";

const workerRoot = dirname(dirname(fileURLToPath(import.meta.url)));

function version(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8")).version;
  } catch {
    return null;
  }
}

function usage() {
  process.stderr.write(
    "usage: cli.mjs serve (--ephemeral | --persistent [--state DIR] [--port N]) "
    + "[--origins JSON] [--reader EMAIL] "
    + "[--fault-every N] [--fault-statuses 429,500] [--drop-request-every N] "
    + "[--drop-response-every N] [--test-state] "
    + "[--d1-batch-fault-every N] [--r2-put-fault-every N] [--r2-get-fault-every N]\n",
  );
  process.exit(2);
}

const args = process.argv.slice(2);
if (args.shift() !== "serve") usage();
const options = { origins: [], faultStatuses: [429, 500] };
const faults = {};
for (let index = 0; index < args.length; index += 1) {
  const argument = args[index];
  if (argument === "--ephemeral") continue;
  if (argument === "--persistent") options.persistent = true;
  else if (argument === "--state") options.state = args[++index];
  else if (argument === "--port") options.port = Number(args[++index]);
  else if (argument === "--origins") options.origins = JSON.parse(args[++index]);
  else if (argument === "--reader") options.reader = args[++index];
  else if (argument === "--fault-every") options.faultEvery = Number(args[++index]);
  else if (argument === "--fault-statuses") options.faultStatuses = args[++index].split(",").map(Number);
  else if (argument === "--drop-request-every") options.dropRequestEvery = Number(args[++index]);
  else if (argument === "--drop-response-every") options.dropResponseEvery = Number(args[++index]);
  else if (argument === "--test-state") options.testState = true;
  else if (argument === "--d1-batch-fault-every") faults.d1_batch_every = Number(args[++index]);
  else if (argument === "--r2-put-fault-every") faults.r2_put_every = Number(args[++index]);
  else if (argument === "--r2-get-fault-every") faults.r2_get_every = Number(args[++index]);
  else usage();
}
if (Object.keys(faults).length > 0) options.faults = faults;

let edge;
try {
  if (options.persistent) {
    const { startPersistent } = await import("../scripts/dev.mjs");
    edge = await startPersistent({
      devDirectory: options.state ?? join(dirname(workerRoot), ".work/local/dev"),
      port: options.port ?? 8790,
      origins: options.origins,
      reader: options.reader,
    });
  } else {
    edge = await startEdge(options);
  }
} catch (error) {
  process.stderr.write(String(error) + "\n");
  process.exit(1);
}
const output = options.persistent ? edge.endpoint : {
  url: edge.url,
  port: edge.port,
  ca_file: edge.caFile,
  token_files: edge.tokenFiles,
  runtime: {
    miniflare: version(join(workerRoot, "node_modules/miniflare/package.json")),
    workerd: version(join(workerRoot, "node_modules/miniflare/node_modules/workerd/package.json")),
  },
};
process.stdout.write(JSON.stringify(output) + "\n");

async function shutdown() {
  try {
    await edge.close();
    process.exit(0);
  } catch (error) {
    process.stderr.write(String(error) + "\n");
    process.exit(1);
  }
}

for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, shutdown);
