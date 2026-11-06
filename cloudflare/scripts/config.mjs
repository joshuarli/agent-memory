#!/usr/bin/env node
import { readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadAndParseConfig } from "@cloudflare/config";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const configPath = join(root, "cloudflare.config.ts");
const buildContext = { isPreview: false, mode: "production" };

function fail(message) {
  console.error(`config: ${message}`);
  process.exit(1);
}

async function load() {
  const { result } = await loadAndParseConfig(configPath, buildContext);
  if (!result.success) {
    for (const issue of result.error.issues) console.error(`${issue.path.join(".")}: ${issue.message}`);
    process.exit(1);
  }
  return result.data;
}

function onlyD1(config) {
  const bindings = Object.entries(config.worker?.env ?? {}).filter(([, binding]) => binding.type === "d1");
  if (bindings.length !== 1) fail(`expected exactly one D1 binding, found ${bindings.length}`);
  const [name, binding] = bindings[0];
  if (typeof binding.id !== "string" || binding.id === "") fail(`D1 binding ${name} has no id`);
  return binding;
}

function check(config) {
  const worker = config.worker;
  const env = worker?.env ?? {};
  const require = (condition, message) => {
    if (!condition) fail(message);
  };
  require(worker?.name === "usr-memories", "the worker must be usr-memories");
  require(worker?.domains?.includes("memories.17166969.xyz") === true, "the memories domain is missing");
  require(env.ACCESS_TEAM_DOMAIN?.type === "text", "ACCESS_TEAM_DOMAIN must be a text binding");
  require(env.ACCESS_AUD?.type === "text", "ACCESS_AUD must be a text binding");
  require(env.DB?.type === "d1", "DB must be a D1 binding");
  require(env.DB?.name === "usr-memories", "DB must bind the usr-memories database");
  require(env.DB?.id === "9870c4da-3758-4f0c-ab6d-2372eb34009b", "DB has an unexpected database id");
  require(env.BUCKET?.type === "r2", "BUCKET must be an R2 binding");
  require(env.BUCKET?.name === "usr-memories", "BUCKET must bind the usr-memories bucket");
  require(worker?.observability?.logs?.headSamplingRate === 0.25, "logs must sample at 0.25");
  require(worker?.observability?.traces?.headSamplingRate === 0.1, "traces must sample at 0.10");
  require(worker?.workersDev === false, "workersDev must be false");
  require(worker?.previewUrls === false, "previewUrls must be false");
  require(worker?.exports?.default?.type === "worker", "exports.default must be the worker");
  require(worker?.exports?.default?.cache?.enabled === false, "the default export cache must be disabled");
  require(worker?.tailConsumers?.some((item) => item.worker === "stats-observe") === true, "the stats-observe tail is missing");
  process.stdout.write("config-check: ok\n");
}

const [command, argument] = process.argv.slice(2);

if (command === "database-id") {
  process.stdout.write(`${onlyD1(await load()).id}\n`);
} else if (command === "set-database-id") {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(argument ?? "")) {
    fail("set-database-id needs a UUID");
  }
  const current = onlyD1(await load());
  if (current.id === argument) process.exit(0);

  const source = readFileSync(configPath, "utf8");
  const pattern = /(bindings\.d1\(\{[\s\S]*?\bid:\s*")([^"]*)(")/;
  if (!pattern.test(source)) fail("cloudflare.config.ts has no d1 id line to replace");
  const updated = source.replace(pattern, `$1${argument}$3`);

  const temporary = `${configPath}.${process.pid}.tmp`;
  try {
    writeFileSync(temporary, updated);
    renameSync(temporary, configPath);
  } catch (error) {
    rmSync(temporary, { force: true });
    throw error;
  }

  if (onlyD1(await load()).id !== argument) fail("cloudflare.config.ts did not take the new D1 id");
} else if (command === "check") {
  check(await load());
} else {
  console.error("usage: config.mjs database-id | set-database-id <uuid> | check");
  process.exit(2);
}
