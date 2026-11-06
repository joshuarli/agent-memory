import { verifyAssertion, type AccessConfig, type KeyLoader } from "./assertion.js";

export interface AccessIdentity {
  readonly email: string | undefined;
  readonly service_token_id: string | undefined;
}

export type Principal =
  | { readonly kind: "reader"; readonly email: string }
  | { readonly kind: "collector"; readonly origin_host: string };

const ASSERTION_HEADER = "Cf-Access-Jwt-Assertion";

function normalize(email: string): string {
  return email.trim().toLowerCase();
}

export function parseReaders(value: string | undefined): ReadonlySet<string> {
  if (value === undefined) return new Set();
  const readers = new Set<string>();
  for (const email of value.split(",")) {
    const normalized = normalize(email);
    if (normalized !== "") readers.add(normalized);
  }
  return readers;
}

async function readerIdentity(context: ExecutionContext): Promise<AccessIdentity | null> {
  if (context === null || context === undefined) return null;
  const access = context.access;
  if (access === undefined) return null;
  try {
    const identity = await access.getIdentity();
    if (identity === undefined) return null;
    return {
      email: typeof identity.email === "string" ? identity.email : undefined,
      service_token_id: typeof identity.service_token_id === "string" ? identity.service_token_id : undefined,
    };
  } catch {
    return null;
  }
}

export async function identityOf(
  context: ExecutionContext,
  request: Request,
  config: AccessConfig,
  load?: KeyLoader,
): Promise<AccessIdentity | null> {
  const reader = await readerIdentity(context);
  if (reader !== null) return reader;
  const assertion = request.headers.get(ASSERTION_HEADER);
  if (assertion === null) return null;
  const serviceToken = await verifyAssertion(assertion, config, load);
  if (serviceToken === null) return null;
  return { email: undefined, service_token_id: serviceToken.clientId };
}

export async function authorize(
  db: D1Database,
  identity: AccessIdentity | null,
  readers: ReadonlySet<string>,
): Promise<Principal | null> {
  if (identity === null) return null;
  if (identity.service_token_id !== undefined) {
    const row = await db.prepare("SELECT origin_host FROM collector WHERE service_token_id = ? AND disabled = 0")
      .bind(identity.service_token_id)
      .first<{ origin_host: string }>();
    return row === null ? null : { kind: "collector", origin_host: row.origin_host };
  }
  const email = identity.email;
  if (email === undefined) return null;
  const normalized = normalize(email);
  if (!readers.has(normalized)) return null;
  return { kind: "reader", email: normalized };
}
