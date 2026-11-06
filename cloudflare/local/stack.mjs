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

export { BUNDLE_DIR, WORKER_CONFIG_PATH };

export const BUNDLE_PATH = join(BUNDLE_DIR, "index.js");

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

function hostEntry(mainModule) {
  return `
import worker from "./${mainModule}";
export default {
  async fetch(request, env, context) {
    const headers = new Headers(request.headers);
    headers.set("Host", new URL(request.url).host);
    return await worker.fetch(new Request(request, { headers }), env, context);
  },
};
`;
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
  const config = prepareWorkerConfig(release, {
    teamDomain: options.teamDomain,
    audience: options.audience,
    readers: options.readers,
    identity: options.identity,
    bundle: options.bundle,
  });
  if (options.hostFromUrl === true) {
    const mainModule = config.manifest.mainModule;
    config.manifest.modules["entry-host.js"] = { type: "esm", contents: hostEntry(mainModule) };
    config.manifest.mainModule = "entry-host.js";
  }
  const stateDir = options.stateDir ?? stateDirectory();
  mkdirSync(stateDir, { recursive: true });

  const logs = [];
  const outbound = [];
  const tailSource = readFileSync(fileURLToPath(tailModule), "utf8");
  const workers = [
    {
      config,
      dev: {
        unsafeRegisterWorker: false,
        outboundService: {
          type: "fetcher",
          handler: async (request) => {
            outbound.push(request.url);
            return new Response("outbound request blocked", {
              status: 599,
              headers: { "content-type": "text/plain" },
            });
          },
        },
      },
    },
  ];
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
  #disposed = false;
  #d1;
  #r2;
  #digest;

  constructor({ miniflare, stateDir, config, logs, outbound, allowOutbound, runtimeOptions, devOptions }) {
    this.#miniflare = miniflare;
    this.#stateDir = stateDir;
    this.#config = config;
    this.#logs = logs;
    this.#outbound = outbound;
    this.#allowOutbound = allowOutbound;
    this.#runtimeOptions = runtimeOptions;
    this.#devOptions = devOptions;
  }

  get stateDir() {
    return this.#stateDir;
  }

  get config() {
    return this.#config;
  }

  get compatibilityDate() {
    return this.#config.compatibilityDate;
  }

  get compatibilityFlags() {
    return [...(this.#config.compatibilityFlags ?? [])];
  }

  get moduleNames() {
    return Object.keys(this.#config.manifest.modules).sort();
  }

  get registryEnabled() {
    return this.#runtimeOptions.enableDevRegistry;
  }

  get registryPath() {
    return this.#runtimeOptions.unsafeDevRegistryPath;
  }

  get unsafeRegisterWorker() {
    return this.#devOptions.unsafeRegisterWorker;
  }

  get outbound() {
    return [...this.#outbound];
  }

  get disposed() {
    return this.#disposed;
  }

  get bundleDigest() {
    this.#digest ??= computeBundleDigest();
    return this.#digest;
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

  getWorker(name) {
    return this.#miniflare.getWorker(name);
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

  async drainTailEvents() {
    if (this.#config.tailConsumers === undefined) {
      throw new Error("local stack: the release config has no tail worker");
    }
    const worker = await this.#miniflare.getWorker("stats-observe");
    const response = await worker.fetch("http://stats-observe/__tail/drain");
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
