import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const workerRoot = dirname(here);

export const BUNDLE_DIR = join(workerRoot, ".cloudflare/output/v0/workers/default/bundle");
export const WORKER_CONFIG_PATH = join(workerRoot, ".cloudflare/output/v0/workers/default/worker.config.json");
const ACCESS_OVERRIDES = new Set(["ACCESS_TEAM_DOMAIN", "ACCESS_AUD"]);

const TOP_LEVEL = new Set([
  "name", "compatibilityDate", "compatibilityFlags", "domains", "tailConsumers",
  "observability", "exports", "workersDev", "previewUrls", "env", "manifest",
]);
const BINDING_KEYS = {
  text: new Set(["type", "value"]),
  d1: new Set(["type", "name", "id"]),
  r2: new Set(["type", "name"]),
};
const MODULE_TYPES = new Set(["esm", "sourcemap"]);

function fail(message) {
  throw new Error(`local stack: ${message}`);
}

function record(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertString(value, key) {
  if (typeof value !== "string" || value === "") fail(`${key} must be a non-empty string`);
}

function validateBinding(name, binding) {
  const key = `env.${name}`;
  if (!record(binding)) fail(`${key} must be an object`);
  const type = binding.type;
  if (typeof type !== "string" || !(type in BINDING_KEYS)) {
    fail(`${key}.type is unsupported (${JSON.stringify(type)})`);
  }
  for (const property of Object.keys(binding)) {
    if (!BINDING_KEYS[type].has(property)) fail(`${key}.${property} is unsupported`);
  }
  if (type === "text") {
    if (typeof binding.value !== "string") fail(`${key}.value must be a string`);
    return;
  }
  assertString(binding.name, `${key}.name`);
  if (type === "d1") assertString(binding.id, `${key}.id`);
}

export function validateReleaseConfig(config) {
  if (!record(config)) fail("worker.config.json must contain an object");
  for (const key of Object.keys(config)) {
    if (!TOP_LEVEL.has(key)) fail(`${key} is unsupported`);
  }
  assertString(config.name, "name");
  assertString(config.compatibilityDate, "compatibilityDate");
  if (config.compatibilityFlags !== undefined) {
    if (!Array.isArray(config.compatibilityFlags)) fail("compatibilityFlags must be an array");
    for (const [index, flag] of config.compatibilityFlags.entries()) {
      assertString(flag, `compatibilityFlags[${index}]`);
    }
  }
  if (config.domains !== undefined) {
    if (!Array.isArray(config.domains)) fail("domains must be an array");
    for (const [index, domain] of config.domains.entries()) {
      assertString(domain, `domains[${index}]`);
    }
  }
  if (config.tailConsumers !== undefined) {
    if (!Array.isArray(config.tailConsumers) || config.tailConsumers.length !== 1) {
      fail("tailConsumers must be exactly [{ worker: \"stats-observe\" }]");
    }
    for (const [index, consumer] of config.tailConsumers.entries()) {
      const key = `tailConsumers[${index}]`;
      if (!record(consumer)) fail(`${key} must be an object`);
      for (const property of Object.keys(consumer)) {
        if (property !== "worker") fail(`${key}.${property} is unsupported`);
      }
      if (consumer.worker !== "stats-observe") fail(`${key}.worker must be "stats-observe"`);
    }
  }
  if (config.observability !== undefined && !record(config.observability)) {
    fail("observability must be an object");
  }
  if (config.exports !== undefined) {
    if (!record(config.exports)) fail("exports must be an object");
    for (const property of Object.keys(config.exports)) {
      if (property !== "default") fail(`exports.${property} is unsupported`);
    }
    const exported = config.exports.default;
    if (!record(exported)) fail("exports.default must be an object");
    for (const property of Object.keys(exported)) {
      if (property !== "type" && property !== "cache") fail(`exports.default.${property} is unsupported`);
    }
    if (exported.type !== "worker") fail("exports.default.type must be \"worker\"");
    if (!record(exported.cache) || exported.cache.enabled !== false) {
      fail("exports.default.cache.enabled must be false");
    }
  }
  for (const key of ["workersDev", "previewUrls"]) {
    if (config[key] !== undefined && typeof config[key] !== "boolean") fail(`${key} must be a boolean`);
  }
  if (config.env !== undefined) {
    if (!record(config.env)) fail("env must be an object");
    for (const [name, binding] of Object.entries(config.env)) validateBinding(name, binding);
  }
  if (!record(config.manifest)) fail("manifest must be an object");
  assertString(config.manifest.mainModule, "manifest.mainModule");
  if (!record(config.manifest.modules)) fail("manifest.modules must be an object");
  for (const [name, module] of Object.entries(config.manifest.modules)) {
    const key = `manifest.modules.${name}`;
    if (!record(module)) fail(`${key} must be an object`);
    for (const property of Object.keys(module)) {
      if (property !== "type") fail(`${key}.${property} is unsupported`);
    }
    if (!MODULE_TYPES.has(module.type)) fail(`${key}.type is unsupported (${JSON.stringify(module.type)})`);
  }
  return config;
}

export function loadReleaseConfig(path = WORKER_CONFIG_PATH) {
  if (!existsSync(path)) fail(`missing ${path}; run \`npm run build\` in cloudflare/`);
  let config;
  try {
    config = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    fail(`cannot parse ${path}: ${error.message}`);
  }
  validateReleaseConfig(config);
  return config;
}

export function prepareWorkerConfig(config, options = {}) {
  validateReleaseConfig(config);
  const bundle = options.bundle ?? BUNDLE_DIR;
  const prepared = JSON.parse(JSON.stringify(config));
  const modules = {};
  for (const [name, module] of Object.entries(config.manifest.modules)) {
    const path = join(bundle, name);
    if (!existsSync(path)) fail(`missing module ${path}`);
    modules[name] = { type: module.type, contents: readFileSync(path, "utf8") };
  }
  prepared.manifest = {
    mainModule: config.manifest.mainModule,
    modulesRoot: bundle,
    modules,
  };
  const overrides = {
    ACCESS_TEAM_DOMAIN: options.teamDomain,
    ACCESS_AUD: options.audience,
  };
  for (const [name, value] of Object.entries(overrides)) {
    if (value === undefined) continue;
    if (!ACCESS_OVERRIDES.has(name)) fail(`${name} is not an allowed override`);
    const binding = prepared.env?.[name];
    if (binding !== undefined && binding.type !== "text") fail(`env.${name} is not a text binding`);
    prepared.env = { ...prepared.env, [name]: { type: "text", value } };
  }
  if (options.readers !== undefined) {
    if (prepared.env?.READERS !== undefined) fail("env.READERS is not an allowed override");
    prepared.env = { ...prepared.env, READERS: { type: "text", value: options.readers } };
  }
  if (options.identity !== undefined) {
    const audience = prepared.env?.ACCESS_AUD?.value;
    if (typeof audience !== "string" || audience === "") fail("identity needs an ACCESS_AUD text binding");
    prepared.access = { dev: { aud: audience, identity: options.identity } };
  }
  return prepared;
}
