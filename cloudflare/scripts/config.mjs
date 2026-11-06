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
} else {
  console.error("usage: config.mjs database-id | set-database-id <uuid>");
  process.exit(2);
}
