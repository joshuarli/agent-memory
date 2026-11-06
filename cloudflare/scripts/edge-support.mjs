import { execFileSync } from "node:child_process";
import { randomBytes, sign } from "node:crypto";
import { chmodSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const TEAM_DOMAIN = "team.example.test";
export const AUDIENCE = "local-audience";
export const READER = "reader@example.test";

export function certificate(directory) {
  const key = join(directory, "edge-key.pem");
  const cert = join(directory, "edge-cert.pem");
  execFileSync("openssl", [
    "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
    "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
  ], { stdio: "ignore" });
  chmodSync(key, 0o600);
  return { key, cert, caFile: cert };
}

export function tokenTable(directory, origins) {
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

export function assertionFor(privateKey, clientId) {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const now = Math.floor(Date.now() / 1000);
  const input = `${encode({ alg: "RS256", kid: "local-key", typ: "JWT" })}.${encode({
    type: "app", aud: [AUDIENCE], exp: now + 3600, iat: now - 10,
    iss: `https://${TEAM_DOMAIN}`, sub: "", common_name: clientId,
  })}`;
  return `${input}.${sign("RSA-SHA256", Buffer.from(input), privateKey).toString("base64url")}`;
}
