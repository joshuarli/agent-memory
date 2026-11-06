import { createHash, randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Log, LogLevel, Miniflare } from "miniflare";

import { BUNDLE_DIR, loadReleaseConfig, prepareWorkerConfig, WORKER_CONFIG_PATH } from "./config.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const workerRoot = dirname(here);
const repoRoot = dirname(workerRoot);
const outputRoot = join(workerRoot, ".cloudflare/output");
const scratchRoot = join(repoRoot, ".work/tmp");
const tailModule = new URL("./tail-worker.mjs", import.meta.url);
const faultModule = new URL("./fault-entry.mjs", import.meta.url);

export { BUNDLE_DIR };

function fail(message) {
  throw new Error(`local stack: ${message}`);
}

function filesUnder(directory, prefix = "") {
  const files = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) files.push(...filesUnder(join(directory, entry.name), path));
    else if (entry.isFile()) files.push(path);
  }
  return files;
}

export function computeBundleDigest(root = outputRoot) {
  const files = filesUnder(root).sort();
  const lines = files.map((file) =>
    `${createHash("sha256").update(readFileSync(join(root, file))).digest("hex")}  ./${file}\n`).join("");
  return createHash("sha256").update(lines).digest("hex");
}

function stateDirectory() {
  return join(scratchRoot, `stack-${process.pid}-${randomBytes(6).toString("hex")}`);
}

function writeFailure(stateDir, logs, reason) {
  const path = join(stateDir, "workerd.log");
  try {
    writeFileSync(path, logs.map((entry) => {
      try {
        return JSON.stringify(entry);
      } catch {
        return String(entry);
      }
    }).join("\n") + "\n");
  } catch {
    return stateDir;
  }
  process.stderr.write(`local stack: kept ${stateDir} after ${reason}; workerd log: ${path}\n`);
  return path;
}

export async function createStack(options = {}) {
  const release = loadReleaseConfig(options.configPath ?? WORKER_CONFIG_PATH);
  const releaseMainModule = release.manifest.mainModule;
  const config = prepareWorkerConfig(release, {
    teamDomain: options.teamDomain,
    audience: options.audience,
    readers: options.readers,
    identity: options.identity,
    accessService: options.accessService === true,
    bundle: options.bundle,
  });
  for (const [name, value] of Object.entries(options.bindings ?? {})) {
    if (config.env?.[name] !== undefined) fail(`binding ${name} already exists`);
    config.env = { ...config.env, [name]: { type: "text", value: String(value) } };
  }
  if (options.harness === true) {
    const contents = readFileSync(fileURLToPath(faultModule), "utf8");
    config.manifest.modules["fault-entry.mjs"] = { type: "esm", contents };
    config.manifest.mainModule = "fault-entry.mjs";
  }
  const stateDir = options.stateDir ?? stateDirectory();
  mkdirSync(stateDir, { recursive: true });

  const logs = [];
  const outbound = [];
  const outboundHandler = async (request) => {
    outbound.push(request.url);
    const response = options.outbound === undefined ? undefined : await options.outbound(request);
    return response ?? new Response("outbound request blocked", {
      status: 599,
      headers: { "content-type": "text/plain" },
    });
  };
  const devOptions = () => ({
    unsafeRegisterWorker: false,
    outboundService: { type: "fetcher", handler: outboundHandler },
  });
  const workers = [{ config, dev: devOptions() }];
  let collectorName;
  if (options.collectorWorker === true) {
    const collector = JSON.parse(JSON.stringify(config));
    collector.name = `${config.name}-collectors`;
    collector.manifest.mainModule = releaseMainModule;
    delete collector.access;
    collectorName = collector.name;
    workers.push({ config: collector, dev: devOptions() });
  }
  const tailSource = readFileSync(fileURLToPath(tailModule), "utf8");
  if (config.tailConsumers !== undefined) {
    workers.push({
      config: {
        name: "stats-observe",
        compatibilityDate: config.compatibilityDate,
        manifest: {
          mainModule: "tail-worker.mjs",
          modules: { "tail-worker.mjs": { type: "esm", contents: tailSource } },
        },
      },
    });
  }

  let miniflare;
  const runtimeOptions = {
    resourcePersistencePath: join(stateDir, "state"),
    resourceTmpPath: join(stateDir, "tmp"),
    enableDevRegistry: false,
    telemetry: { enabled: false },
  };
  try {
    miniflare = new Miniflare({
      workers,
      ...runtimeOptions,
      log: new Log(LogLevel.NONE),
      handleStructuredLogs: (entry) => logs.push(entry),
    });
    await miniflare.ready;
  } catch (error) {
    writeFailure(stateDir, logs, "startup failure");
    throw error;
  }

  return new LocalStack({
    miniflare,
    stateDir,
    config,
    logs,
    outbound,
    allowOutbound: options.allowOutbound ?? (() => false),
    runtimeOptions,
    devOptions: workers[0].dev,
    collectorName,
  });
}

class LocalStack {
  #miniflare;
  #stateDir;
  #config;
  #logs;
  #outbound;
  #allowOutbound;
  #runtimeOptions;
  #devOptions;
  #collectorName;
  #disposed = false;
  #d1;
  #r2;

  constructor({ miniflare, stateDir, config, logs, outbound, allowOutbound, runtimeOptions, devOptions, collectorName }) {
    this.#miniflare = miniflare;
    this.#stateDir = stateDir;
    this.#config = config;
    this.#logs = logs;
    this.#outbound = outbound;
    this.#allowOutbound = allowOutbound;
    this.#runtimeOptions = runtimeOptions;
    this.#devOptions = devOptions;
    this.#collectorName = collectorName;
  }

  get stateDir() {
    return this.#stateDir;
  }

  get config() {
    return this.#config;
  }

  get runtime() {
    return { ...this.#runtimeOptions, dev: this.#devOptions };
  }

  get outbound() {
    return [...this.#outbound];
  }

  async d1() {
    this.#d1 ??= await this.#miniflare.getD1Database("DB", this.#config.name);
    return await this.#d1;
  }

  async r2() {
    this.#r2 ??= await this.#miniflare.getR2Bucket("BUCKET", this.#config.name);
    return await this.#r2;
  }

  async env() {
    const [DB, BUCKET] = await Promise.all([this.d1(), this.r2()]);
    return {
      READERS: this.#config.env?.READERS?.value,
      ACCESS_TEAM_DOMAIN: this.#config.env?.ACCESS_TEAM_DOMAIN?.value,
      ACCESS_AUD: this.#config.env?.ACCESS_AUD?.value,
      DB,
      BUCKET,
    };
  }

  dispatchFetch(input, init) {
    return this.#miniflare.dispatchFetch(input, init);
  }

  async collectorWorker() {
    if (this.#collectorName === undefined) throw new Error("local stack: no collector worker");
    return await this.#miniflare.getWorker(this.#collectorName);
  }

  async tailEvents() {
    if (this.#config.tailConsumers === undefined) {
      throw new Error("local stack: the release config has no tail worker");
    }
    const worker = await this.#miniflare.getWorker("stats-observe");
    const response = await worker.fetch("http://stats-observe/__tail/events");
    if (!response.ok) throw new Error(`local stack: tail worker returned ${response.status}`);
    return await response.json();
  }

  async dispose() {
    if (this.#disposed) return;
    this.#disposed = true;
    try {
      await this.#miniflare.dispose();
    } catch (error) {
      writeFailure(this.#stateDir, this.#logs, "dispose failure");
      throw error;
    }
    const unexpected = this.#outbound.filter((url) => !this.#allowOutbound(url));
    if (unexpected.length > 0) {
      writeFailure(this.#stateDir, this.#logs, "unexpected outbound request");
      throw new Error(`local stack: unexpected outbound request: ${unexpected.join(", ")}`);
    }
    rmSync(this.#stateDir, { recursive: true, force: true });
  }
}
