#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { loadAndParseConfig } from "@cloudflare/config";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const output = join(root, ".cloudflare/output/v0");
const worker = join(output, "workers/default");
const bundle = join(worker, "bundle");
const buildContext = { isPreview: false, mode: "production" };

rmSync(worker, { recursive: true, force: true });
execFileSync(join(root, "node_modules/.bin/tsrs"), ["-p", "tsconfig.json", "--fast"], {
  cwd: root,
  stdio: "inherit",
});

const mainModule = join(bundle, "index.js");
if (!existsSync(mainModule)) {
  console.error("build: tsrs did not emit bundle/index.js");
  process.exit(1);
}

const modules = {};
const add = (path) => {
  const name = relative(bundle, path).split("\\").join("/");
  if (path.endsWith(".js")) modules[name] = { type: "esm" };
  else if (path.endsWith(".js.map")) modules[name] = { type: "sourcemap" };
};
const walk = (dir) => {
  for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) walk(path);
    else add(path);
  }
};
walk(bundle);

const { result } = await loadAndParseConfig(join(root, "cloudflare.config.ts"), buildContext);
if (!result.success) {
  for (const issue of result.error.issues) console.error(`${issue.path.join(".")}: ${issue.message}`);
  process.exit(1);
}
const { worker: parsed, accountId, complianceRegion } = result.data;
if (parsed === undefined) {
  console.error("build: cloudflare.config.ts has no worker");
  process.exit(1);
}
const { entrypoint: _entrypoint, ...config } = parsed;
writeFileSync(
  join(worker, "worker.config.json"),
  JSON.stringify({ ...config, manifest: { type: "complete", mainModule: "index.js", modules } }),
);
writeFileSync(
  join(output, "config.json"),
  JSON.stringify({
    ...(accountId === undefined ? {} : { accountId }),
    ...(complianceRegion === undefined ? {} : { complianceRegion }),
    buildContext,
  }),
);

console.log(`built bundle/index.js (${statSync(mainModule).size} B, ${Object.keys(modules).length} modules)`);
