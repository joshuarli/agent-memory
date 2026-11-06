import { execFileSync } from "node:child_process";
import { generateKeyPairSync, randomBytes, sign } from "node:crypto";
import { chmodSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:https";
import { join } from "node:path";

import { loadReleaseConfig, WORKER_CONFIG_PATH } from "./config.mjs";
import { seedDatabase } from "../bench/seed.mjs";
import { createStack } from "./stack.mjs";

const TEAM_DOMAIN = "team.example.test";
const AUDIENCE = "local-audience";
const READER = "reader@example.test";
const CLIENT_HEADERS = ["cf-access-client-id", "cf-access-client-secret"];
const DROP_HEADERS = new Set(["host", "connection", "content-length", "transfer-encoding", "expect", ...CLIENT_HEADERS]);

function certificate(directory) {
  const key = join(directory, "edge-key.pem");
  const cert = join(directory, "edge-cert.pem");
  execFileSync("openssl", [
    "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
    "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
  ], { stdio: "ignore" });
  chmodSync(key, 0o600);
  return { key, cert, caFile: cert };
}

function tokenTable(directory, origins) {
  const files = {};
  const byClient = new Map();
  for (const item of origins) {
    if (typeof item.client_id !== "string" || typeof item.origin !== "string") {
      throw new Error("local edge: every origin needs client_id and origin");
    }
    const secret = randomBytes(24).toString("base64url");
    const path = join(directory, `token-${item.origin}.json`);
    writeFileSync(path, JSON.stringify({ client_id: item.client_id, client_secret: secret }), { mode: 0o600 });
    chmodSync(path, 0o600);
    files[item.origin] = path;
    byClient.set(item.client_id, { secret, origin: item.origin });
  }
  return { files, byClient };
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks);
}

function send(response, status, value, headers = {}) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers });
  response.end(JSON.stringify(value));
}

async function relay(response, result) {
  response.writeHead(result.status, Object.fromEntries(result.headers));
  response.end(Buffer.from(await result.arrayBuffer()));
}

async function finish(request, response, result, drop) {
  if (drop) {
    request.socket.destroy();
    return;
  }
  await relay(response, result);
}

function workerRequest(request, url, body, domain, extra) {
  const headers = new Headers();
  for (const [name, value] of Object.entries(request.headers)) {
    if (DROP_HEADERS.has(name)) continue;
    if (Array.isArray(value)) for (const item of value) headers.append(name, item);
    else if (value !== undefined) headers.set(name, String(value));
  }
  for (const [name, value] of Object.entries(extra)) headers.set(name, value);
  const method = request.method ?? "GET";
  const init = { method, headers };
  if (method !== "GET" && method !== "HEAD" && body.length > 0) init.body = body;
  return { url: `https://${domain}${url.pathname}${url.search}`, init };
}

export async function startEdge(options = {}) {
  const origins = options.origins ?? [];
  const reader = options.reader ?? READER;
  const faultEvery = options.faultEvery ?? 0;
  const faultStatuses = options.faultStatuses ?? [429, 500];
  const dropRequestEvery = options.dropRequestEvery ?? 0;
  const dropResponseEvery = options.dropResponseEvery ?? 0;
  const release = loadReleaseConfig(options.configPath ?? WORKER_CONFIG_PATH);
  const domain = release.domains?.[0] ?? "memories.17166969.xyz";
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "local-key", alg: "RS256", use: "sig" };
  const certs = `https://${TEAM_DOMAIN}/cdn-cgi/access/certs`;
  const stack = await createStack({
    harness: true,
    collectorWorker: true,
    identity: { email: reader },
    accessService: true,
    readers: reader,
    teamDomain: TEAM_DOMAIN,
    audience: AUDIENCE,
    outbound: async (request) => request.url === certs ? Response.json({ keys: [jwk] }) : undefined,
    allowOutbound: (url) => url === certs,
    bindings: options.faults === undefined ? undefined : { LOCAL_FAULTS: JSON.stringify(options.faults) },
  });
  const tls = certificate(stack.stateDir);
  const tokens = tokenTable(stack.stateDir, origins);
  const db = await stack.d1();
  await seedDatabase(db);
  const insert = db.prepare(
    "INSERT OR REPLACE INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES(?, ?, ?, ?, 0)",
  );
  for (const item of origins) await insert.bind(item.client_id, item.origin, "local", 0).run();
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const assertion = (clientId) => {
    const now = Math.floor(Date.now() / 1000);
    const input = `${encode({ alg: "RS256", kid: "local-key", typ: "JWT" })}.${encode({
      type: "app", aud: [AUDIENCE], exp: now + 3600, iat: now - 10,
      iss: `https://${TEAM_DOMAIN}`, sub: "", common_name: clientId,
    })}`;
    return `${input}.${sign("RSA-SHA256", Buffer.from(input), privateKey).toString("base64url")}`;
  };
  let requests = 0;
  let faults = 0;
  let drops = 0;
  let droppedRequests = 0;
  let droppedResponses = 0;
  const handler = async (request, response) => {
    try {
      const url = new URL(request.url ?? "/", "https://127.0.0.1");
      if (options.testState === true && url.pathname === "/__test/faults") {
        return send(response, 200, { requests, faults, drops });
      }
      if (url.pathname.startsWith("/__bench/")) {
        const forwarded = workerRequest(request, url, await readBody(request), domain, {});
        return await relay(response, await stack.dispatchFetch(forwarded.url, forwarded.init));
      }
      if (dropRequestEvery > 0) {
        droppedRequests += 1;
        if (droppedRequests % dropRequestEvery === 0) {
          drops += 1;
          request.socket.destroy();
          return;
        }
      }
      const body = await readBody(request);
      if (faultEvery > 0) {
        requests += 1;
        if (requests % faultEvery === 0) {
          const status = faultStatuses[faults % faultStatuses.length];
          faults += 1;
          return send(response, status, { error: { code: "unavailable", message: "synthetic fault" } },
            { "retry-after": "0" });
        }
      }
      let dropResponse = false;
      if (dropResponseEvery > 0) {
        droppedResponses += 1;
        if (droppedResponses % dropResponseEvery === 0) {
          dropResponse = true;
          drops += 1;
        }
      }
      const clientId = request.headers["cf-access-client-id"];
      const secret = request.headers["cf-access-client-secret"];
      if (typeof clientId === "string") {
        const token = tokens.byClient.get(clientId);
        if (token === undefined || token.secret !== secret) {
          return send(response, 403, { error: { code: "forbidden", message: "Access service token is invalid" } });
        }
        const forwarded = workerRequest(request, url, body, domain, {
          "Cf-Access-Jwt-Assertion": assertion(clientId),
        });
        const result = await (await stack.collectorWorker()).fetch(new Request(forwarded.url, forwarded.init));
        return await finish(request, response, result, dropResponse);
      }
      const session = request.headers["x-bench-principal"] ?? request.headers["x-local-reader"];
      if (typeof session === "string" && session !== "") {
        const forwarded = workerRequest(request, url, body, domain, {});
        return await finish(request, response, await stack.dispatchFetch(forwarded.url, forwarded.init), dropResponse);
      }
      const forwarded = workerRequest(request, url, body, domain, {});
      return await finish(request, response, await (await stack.collectorWorker())
        .fetch(new Request(forwarded.url, forwarded.init)), dropResponse);
    } catch (error) {
      send(response, 500, { error: String(error) });
    }
  };
  const server = createServer({ key: readFileSync(tls.key), cert: readFileSync(tls.cert) }, handler);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address !== null ? address.port : 0;
  const close = async () => {
    server.closeAllConnections?.();
    await new Promise((resolve) => server.close(resolve));
    await stack.dispose();
  };
  return { port, caFile: tls.caFile, tokenFiles: tokens.files, close, stack };
}
