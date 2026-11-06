import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { loadReleaseConfig } from "../local/config.mjs";
import { startEdge } from "../local/edge.mjs";

const workerRoot = dirname(dirname(fileURLToPath(import.meta.url)));

function cfBinary() {
  const local = join(workerRoot, "node_modules/.bin/cf");
  return process.env.CF_BIN ?? (existsSync(local) ? local : "cf");
}

function keygenBinary() {
  const primary = process.env.AGE_BIN ?? "rage";
  for (const name of ["rage-keygen", "age-keygen"]) {
    const sibling = join(dirname(primary), name);
    if (existsSync(sibling)) return sibling;
  }
  return "rage-keygen";
}

function ageKeys(directory) {
  const identity = join(directory, "age.key");
  const recipients = join(directory, "age.recipients");
  const keygen = keygenBinary();
  if (!existsSync(identity)) {
    execFileSync(keygen, ["-o", identity], { stdio: ["ignore", "pipe", "pipe"] });
    chmodSync(identity, 0o600);
  }
  const recipient = execFileSync(keygen, ["-y", identity], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  writeFileSync(recipients, recipient + "\n", { mode: 0o600 });
  chmodSync(recipients, 0o600);
  return { identity, recipients };
}

export async function startPersistent(options = {}) {
  if (options.origins === undefined || options.origins.length === 0) {
    throw new Error("local dev: at least one origin is required");
  }
  const devDirectory = options.devDirectory;
  mkdirSync(devDirectory, { recursive: true, mode: 0o700 });
  const release = loadReleaseConfig();
  execFileSync(cfBinary(), [
    "d1", "migrations", "apply", release.env.DB.id, "--local", "--persist-to", devDirectory,
  ], { cwd: workerRoot, stdio: ["ignore", "pipe", "pipe"] });
  const keys = join(devDirectory, "keys");
  mkdirSync(keys, { recursive: true, mode: 0o700 });
  const { identity, recipients } = ageKeys(keys);
  const edge = await startEdge({
    origins: options.origins,
    reader: options.reader,
    stateDir: devDirectory,
    persistencePath: join(devDirectory, "v3"),
    keepState: true,
    seed: false,
    port: options.port ?? 8790,
  });
  const home = join(devDirectory, "home");
  const configDirectory = join(home, ".agent-sessions");
  mkdirSync(configDirectory, { recursive: true, mode: 0o700 });
  const configFile = join(configDirectory, "config.json");
  const tokenFile = edge.tokenFiles[options.origins[0].origin];
  writeFileSync(configFile, JSON.stringify({
    central: {
      transport: "cloudflare",
      url: edge.url,
      service_token_file: tokenFile,
      age_recipients_file: recipients,
      age_identity_file: identity,
    },
  }) + "\n", { mode: 0o600 });
  chmodSync(configFile, 0o600);
  const endpoint = { url: edge.url, ca_file: edge.caFile, token_files: edge.tokenFiles, home };
  const endpointFile = join(devDirectory, "endpoint.json");
  writeFileSync(endpointFile, JSON.stringify(endpoint) + "\n", { mode: 0o600 });
  chmodSync(endpointFile, 0o600);
  return { ...edge, endpoint, endpointFile };
}
