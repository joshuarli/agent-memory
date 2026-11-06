export interface AccessConfig {
  readonly teamDomain: string;
  readonly audience: string;
}

export interface ServiceToken {
  readonly clientId: string;
}

export type KeyLoader = (teamDomain: string, kid: string) => Promise<JsonWebKeyWithKid | null>;

const JWKS_PATH = "/cdn-cgi/access/certs";
const KEY_TTL_MS = 60 * 60 * 1000;
const MAX_CLOCK_SKEW_SECONDS = 60;
const TEAM_DOMAIN = /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/;
const BASE64URL = /^[A-Za-z0-9_-]+$/;
const CLIENT_ID = /^[A-Za-z0-9_-]+\.access$/;
const ALGORITHM = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" } as const;

const keyCache = new Map<string, { readonly expiresAt: number; readonly keys: ReadonlyMap<string, JsonWebKeyWithKid> }>();

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function decodeBase64Url(value: string): Uint8Array | null {
  if (!BASE64URL.test(value)) return null;
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  try {
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes;
  } catch {
    return null;
  }
}

function decodeJson(value: string): unknown {
  const bytes = decodeBase64Url(value);
  if (bytes === null) return null;
  try {
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
}

export function keysFromJwks(value: unknown): ReadonlyMap<string, JsonWebKeyWithKid> {
  if (!isRecord(value) || !Array.isArray(value.keys) || value.keys.length === 0) {
    throw new Error("Access keyset has no keys");
  }
  const keys = new Map<string, JsonWebKeyWithKid>();
  for (const entry of value.keys) {
    if (!isRecord(entry) || entry.kty !== "RSA" || typeof entry.kid !== "string" || entry.kid === "") {
      throw new Error("Access keyset contains an invalid key");
    }
    if (typeof entry.n !== "string" || typeof entry.e !== "string") {
      throw new Error("Access keyset contains an invalid RSA key");
    }
    if (keys.has(entry.kid)) throw new Error("Access keyset repeats a key id");
    keys.set(entry.kid, { kty: "RSA", kid: entry.kid, n: entry.n, e: entry.e, alg: "RS256", ext: true });
  }
  return keys;
}

async function fetchKeys(teamDomain: string): Promise<ReadonlyMap<string, JsonWebKeyWithKid>> {
  if (!TEAM_DOMAIN.test(teamDomain)) throw new Error("Access team domain is invalid");
  const response = await fetch(`https://${teamDomain}${JWKS_PATH}`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error("Access keyset request failed");
  return keysFromJwks(await response.json());
}

async function cachedKeys(teamDomain: string): Promise<ReadonlyMap<string, JsonWebKeyWithKid>> {
  const cached = keyCache.get(teamDomain);
  if (cached !== undefined && cached.expiresAt > Date.now()) return cached.keys;
  const keys = await fetchKeys(teamDomain);
  keyCache.set(teamDomain, { expiresAt: Date.now() + KEY_TTL_MS, keys });
  return keys;
}

async function keyFor(teamDomain: string, kid: string): Promise<JsonWebKeyWithKid | null> {
  return (await cachedKeys(teamDomain)).get(kid) ?? null;
}

function audienceMatches(value: unknown, audience: string): boolean {
  if (typeof value === "string") return value === audience;
  if (!Array.isArray(value)) return false;
  return value.includes(audience);
}

async function signatureValid(jwk: JsonWebKeyWithKid, signingInput: string, signature: Uint8Array): Promise<boolean> {
  const key = await crypto.subtle.importKey("jwk", jwk, ALGORITHM, false, ["verify"]);
  return await crypto.subtle.verify(ALGORITHM, key, signature, new TextEncoder().encode(signingInput));
}

export async function verifyAssertion(
  assertion: string,
  config: AccessConfig,
  load: KeyLoader = keyFor,
  now = Math.floor(Date.now() / 1000),
): Promise<ServiceToken | null> {
  if (!TEAM_DOMAIN.test(config.teamDomain) || config.audience === "") return null;
  const parts = assertion.split(".");
  if (parts.length !== 3) return null;
  const [headerPart, payloadPart, signaturePart] = parts;
  if (headerPart === undefined || payloadPart === undefined || signaturePart === undefined) return null;
  const header = decodeJson(headerPart);
  if (!isRecord(header) || header.alg !== "RS256" || typeof header.kid !== "string" || header.kid === "") return null;
  const signature = decodeBase64Url(signaturePart);
  if (signature === null) return null;
  const jwk = await load(config.teamDomain, header.kid);
  if (jwk === null) return null;
  if (!await signatureValid(jwk, `${headerPart}.${payloadPart}`, signature)) return null;
  const payload = decodeJson(payloadPart);
  if (!isRecord(payload)) return null;
  if (payload.type !== "app" || payload.iss !== `https://${config.teamDomain}`) return null;
  if (!audienceMatches(payload.aud, config.audience)) return null;
  if (typeof payload.exp !== "number" || payload.exp <= now) return null;
  if (typeof payload.nbf === "number" && payload.nbf > now + MAX_CLOCK_SKEW_SECONDS) return null;
  if (payload.sub !== "") return null;
  if (typeof payload.common_name !== "string" || !CLIENT_ID.test(payload.common_name)) return null;
  return { clientId: payload.common_name };
}
