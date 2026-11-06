export const PROTOCOL = 1;
export const MAX_JSON_BYTES = 12 * 1024 * 1024;
export const MAX_OBJECT_BYTES = 8 * 1024 * 1024;
export const MAX_CIPHERTEXT_BYTES = MAX_OBJECT_BYTES + 64 * 1024;
export const MAX_MISSING_OBJECTS = 20_000;
export const MAX_GENERATION_OBJECTS = 20_000;
export const MAX_GENERATION_ARTIFACTS = 1_000;
export const MAX_TOMBSTONES = 900;
export const MAX_SNAPSHOT_BYTES = 2 ** 40;
export const MAX_SNAPSHOT_CHUNKS = 4_096;
export const MAX_SNAPSHOT_OBJECTS = 200_000;
export const MAX_SNAPSHOT_PAGE_OBJECTS = 10_000;
export const MAX_SNAPSHOT_PAGES = MAX_SNAPSHOT_OBJECTS / MAX_SNAPSHOT_PAGE_OBJECTS;
export const GC_GRACE_SECONDS = 3_600;
export const MAX_SOURCE_BYTES = 2 ** 50;
export const MAX_PAGE = 200;
export const MAX_CURSOR_CHARS = 512;
export const MAX_CURSOR_PARTS = 4;
export const MAX_CURSOR_PART_CHARS = 512;

export const HARNESSES = ["codex", "claude", "muse", "grok", "opencode", "pi", "cmd", "agy"] as const;

export const ERROR_CODES = [
  "forbidden",
  "not_found",
  "method_not_allowed",
  "unavailable",
  "invalid_request",
  "unsupported_version",
  "payload_too_large",
  "conflict",
  "stale_revision",
  "missing_object",
  "tombstoned",
] as const;

const DIGEST = /^[0-9a-f]{64}$/;
const ORIGIN = /^[A-Za-z0-9._-]{1,128}$/;
const BASE64URL = /^[A-Za-z0-9_-]+$/;
const ARTIFACT = /^[A-Za-z0-9._-]{1,256}$/;
const SCOPES = new Set(["local", "global"]);
const HARNESS_SET = new Set<string>(HARNESSES);

export interface HelloRequest {
  readonly protocol: 1;
  readonly origin: string;
}

export interface MissingObject {
  readonly plaintext_digest: string;
  readonly size: number;
}

export interface MissingRequest {
  readonly protocol: 1;
  readonly origin: string;
  readonly objects: readonly MissingObject[];
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

export function decodeDigest(value: unknown): string | null {
  return typeof value === "string" && DIGEST.test(value) ? value : null;
}

export function decodeOrigin(value: unknown): string | null {
  return typeof value === "string" && ORIGIN.test(value) ? value : null;
}

export function decodeProtocol(value: unknown): 1 | null {
  return value === PROTOCOL ? PROTOCOL : null;
}

function decodeSize(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= MAX_OBJECT_BYTES
    ? value
    : null;
}

function decodeCiphertextSize(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= MAX_CIPHERTEXT_BYTES
    ? value
    : null;
}

export function decodeHello(value: unknown): HelloRequest | null {
  if (!isRecord(value) || !exactKeys(value, ["protocol", "origin"])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  return origin === null ? null : { protocol: PROTOCOL, origin };
}

function decodeMissingObject(value: unknown): MissingObject | null {
  if (!isRecord(value) || !exactKeys(value, ["plaintext_digest", "size"])) return null;
  const plaintext = decodeDigest(value.plaintext_digest);
  const size = decodeSize(value.size);
  return plaintext === null || size === null ? null : { plaintext_digest: plaintext, size };
}

export function decodeMissingRequest(value: unknown): MissingRequest | null {
  if (!isRecord(value) || !exactKeys(value, ["protocol", "origin", "objects"])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  if (origin === null || !Array.isArray(value.objects)) return null;
  if (value.objects.length > MAX_MISSING_OBJECTS) return null;
  const objects: MissingObject[] = [];
  const digests = new Set<string>();
  for (const entry of value.objects) {
    const object = decodeMissingObject(entry);
    if (object === null || digests.has(object.plaintext_digest)) return null;
    digests.add(object.plaintext_digest);
    objects.push(object);
  }
  return { protocol: PROTOCOL, origin, objects };
}

export interface ManifestChunk {
  readonly offset: number;
  readonly size: number;
  readonly digest: string;
}

export interface ManifestArtifact {
  readonly name: string;
  readonly digest: string;
  readonly size: number;
}

export interface GenerationManifest {
  readonly origin_host: string;
  readonly harness: string;
  readonly native_id: string;
  readonly source_path: string;
  readonly source_digest: string;
  readonly source_size: number;
  readonly captured_at: number;
  readonly chunks: readonly ManifestChunk[];
  readonly artifacts: readonly ManifestArtifact[];
  readonly session_metadata: unknown;
  readonly manifest_digest: string;
  readonly manifest_object: string;
}

export interface GenerationCommit {
  readonly protocol: 1;
  readonly origin: string;
  readonly base_revision: number;
  readonly manifest: GenerationManifest;
}

export interface TombstonePush {
  readonly harness: string;
  readonly native_id: string;
  readonly scope: "local" | "global";
  readonly forgotten_at: number;
}

export interface TombstoneRequest {
  readonly protocol: 1;
  readonly origin: string;
  readonly base_revision: number;
  readonly tombstones: readonly TombstonePush[];
}

function decodeRevision(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function decodeTimestamp(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER
    ? value
    : null;
}

function decodeText(value: unknown, maximum: number): string | null {
  return typeof value === "string" && value !== "" && value.length <= maximum && !value.includes("\u0000")
    ? value
    : null;
}

function decodeManifestChunk(value: unknown): ManifestChunk | null {
  if (!isRecord(value) || !exactKeys(value, ["offset", "size", "digest"])) return null;
  const offset = decodeRevision(value.offset);
  const size = decodeSize(value.size);
  const digest = decodeDigest(value.digest);
  if (offset === null || size === null || digest === null || size === 0) return null;
  return { offset, size, digest };
}

function decodeManifestArtifact(value: unknown): ManifestArtifact | null {
  if (!isRecord(value) || !exactKeys(value, ["name", "digest", "size"])) return null;
  const name = typeof value.name === "string" && ARTIFACT.test(value.name) ? value.name : null;
  const digest = decodeDigest(value.digest);
  const size = decodeSize(value.size);
  return name === null || digest === null || size === null ? null : { name, digest, size };
}

async function sourceDigestOf(chunks: readonly ManifestChunk[]): Promise<string> {
  const text = chunks.map((chunk) => chunk.digest).join("");
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function decodeGenerationManifest(value: unknown): Promise<GenerationManifest | null> {
  if (!isRecord(value)) return null;
  if (!exactKeys(value, [
    "origin_host", "harness", "native_id", "source_path", "source_digest", "source_size",
    "captured_at", "chunks", "artifacts", "session_metadata", "manifest_digest", "manifest_object",
  ])) return null;
  const origin = decodeOrigin(value.origin_host);
  const harness = typeof value.harness === "string" && HARNESS_SET.has(value.harness) ? value.harness : null;
  const native = decodeText(value.native_id, 512);
  const sourcePath = decodeText(value.source_path, 4096);
  const sourceDigest = decodeDigest(value.source_digest);
  const sourceSize = typeof value.source_size === "number"
    && Number.isSafeInteger(value.source_size) && value.source_size >= 0 && value.source_size <= MAX_SOURCE_BYTES
    ? value.source_size
    : null;
  const capturedAt = decodeTimestamp(value.captured_at);
  const manifestDigest = decodeDigest(value.manifest_digest);
  const manifestObject = decodeDigest(value.manifest_object);
  if (origin === null || harness === null || native === null || sourcePath === null) return null;
  if (sourceDigest === null || sourceSize === null || capturedAt === null) return null;
  if (manifestDigest === null || manifestObject === null) return null;
  if (!Array.isArray(value.chunks) || value.chunks.length > MAX_GENERATION_OBJECTS) return null;
  if (!Array.isArray(value.artifacts) || value.artifacts.length > MAX_GENERATION_ARTIFACTS) return null;
  if (value.session_metadata !== null && !isRecord(value.session_metadata) && !Array.isArray(value.session_metadata)) {
    return null;
  }
  const chunks: ManifestChunk[] = [];
  let cursor = 0;
  for (const entry of value.chunks) {
    const chunk = decodeManifestChunk(entry);
    if (chunk === null || chunk.offset !== cursor) return null;
    cursor += chunk.size;
    if (cursor > MAX_SOURCE_BYTES) return null;
    chunks.push(chunk);
  }
  if (cursor !== sourceSize) return null;
  const artifacts: ManifestArtifact[] = [];
  const names = new Set<string>();
  for (const entry of value.artifacts) {
    const artifact = decodeManifestArtifact(entry);
    if (artifact === null || names.has(artifact.name)) return null;
    names.add(artifact.name);
    artifacts.push(artifact);
  }
  const sizes = new Map<string, number>();
  for (const object of [...chunks, ...artifacts]) {
    const known = sizes.get(object.digest);
    if (known !== undefined && known !== object.size) return null;
    sizes.set(object.digest, object.size);
  }
  if (await sourceDigestOf(chunks) !== sourceDigest) return null;
  return {
    origin_host: origin, harness, native_id: native, source_path: sourcePath,
    source_digest: sourceDigest, source_size: sourceSize, captured_at: capturedAt,
    chunks, artifacts, session_metadata: value.session_metadata,
    manifest_digest: manifestDigest, manifest_object: manifestObject,
  };
}

export async function decodeGenerationCommit(value: unknown): Promise<GenerationCommit | null> {
  if (!isRecord(value) || !exactKeys(value, ["protocol", "origin", "base_revision", "manifest"])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  const baseRevision = decodeRevision(value.base_revision);
  if (origin === null || baseRevision === null) return null;
  const manifest = await decodeGenerationManifest(value.manifest);
  if (manifest === null || manifest.origin_host !== origin) return null;
  return { protocol: PROTOCOL, origin, base_revision: baseRevision, manifest };
}

function decodeTombstone(value: unknown): TombstonePush | null {
  if (!isRecord(value) || !exactKeys(value, ["harness", "native_id", "scope", "forgotten_at"])) return null;
  const harness = typeof value.harness === "string" && HARNESS_SET.has(value.harness) ? value.harness : null;
  const native = decodeText(value.native_id, 512);
  const scope = typeof value.scope === "string" && SCOPES.has(value.scope) ? value.scope : null;
  const forgottenAt = decodeTimestamp(value.forgotten_at);
  if (harness === null || native === null || scope === null || forgottenAt === null) return null;
  return { harness, native_id: native, scope: scope as "local" | "global", forgotten_at: forgottenAt };
}

export function decodeTombstoneRequest(value: unknown): TombstoneRequest | null {
  if (!isRecord(value) || !exactKeys(value, ["protocol", "origin", "base_revision", "tombstones"])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  const baseRevision = decodeRevision(value.base_revision);
  if (origin === null || baseRevision === null || !Array.isArray(value.tombstones)) return null;
  if (value.tombstones.length > MAX_TOMBSTONES) return null;
  const tombstones: TombstonePush[] = [];
  const keys = new Set<string>();
  for (const entry of value.tombstones) {
    const tombstone = decodeTombstone(entry);
    if (tombstone === null) return null;
    const key = `${tombstone.harness}:${tombstone.native_id}`;
    if (keys.has(key)) return null;
    keys.add(key);
    tombstones.push(tombstone);
  }
  return { protocol: PROTOCOL, origin, base_revision: baseRevision, tombstones };
}

export interface SnapshotChunkRef {
  readonly plaintext_digest: string;
  readonly size: number;
  readonly ciphertext_digest: string;
}

export interface SnapshotObjectRef {
  readonly plaintext_digest: string;
  readonly size: number;
}

export interface SnapshotInput {
  readonly id: string;
  readonly created_at: number;
  readonly schema_version: number;
  readonly db_digest: string;
  readonly db_size: number;
  readonly chunks: readonly SnapshotChunkRef[];
  readonly objects: number;
  readonly object_bytes: number;
  readonly objects_digest: string;
}

export interface SnapshotPage {
  readonly protocol: 1;
  readonly origin: string;
  readonly page: number;
  readonly objects: readonly SnapshotObjectRef[];
}

export interface SnapshotCommit {
  readonly protocol: 1;
  readonly origin: string;
  readonly base_revision: number;
  readonly snapshot: SnapshotInput;
}

export interface SnapshotVerifyRequest {
  readonly protocol: 1;
  readonly origin: string;
}

export interface GcRequest {
  readonly protocol: 1;
  readonly origin: string;
  readonly apply: boolean;
}

export interface ObjectReplaceRequest {
  readonly protocol: 1;
  readonly origin: string;
  readonly plaintext_digest: string;
  readonly plaintext_size: number;
  readonly encoding: "identity" | "zlib";
  readonly ciphertext_digest: string;
  readonly ciphertext_size: number;
}

function decodeSnapshotChunk(value: unknown): SnapshotChunkRef | null {
  if (!isRecord(value) || !exactKeys(value, ["plaintext_digest", "size", "ciphertext_digest"])) return null;
  const plaintext = decodeDigest(value.plaintext_digest);
  const ciphertext = decodeDigest(value.ciphertext_digest);
  const size = decodeSize(value.size);
  if (plaintext === null || ciphertext === null || size === null || size === 0) return null;
  return { plaintext_digest: plaintext, size, ciphertext_digest: ciphertext };
}

function decodeSnapshotObject(value: unknown): SnapshotObjectRef | null {
  if (!isRecord(value) || !exactKeys(value, ["plaintext_digest", "size"])) return null;
  const digest = decodeDigest(value.plaintext_digest);
  const size = decodeSize(value.size);
  return digest === null || size === null ? null : { plaintext_digest: digest, size };
}

export function snapshotPageCount(objects: number): number {
  return Math.ceil(objects / MAX_SNAPSHOT_PAGE_OBJECTS);
}

export function encodeSnapshotPage(objects: readonly SnapshotObjectRef[]): string {
  return objects.map((object) => `${object.plaintext_digest} ${object.size}\n`).join("");
}

const PAGE_LINE = /^([0-9a-f]{64}) (0|[1-9][0-9]*)$/;

export function decodeSnapshotPageBody(text: string): readonly SnapshotObjectRef[] | null {
  if (!text.endsWith("\n")) return null;
  const objects: SnapshotObjectRef[] = [];
  for (const line of text.slice(0, -1).split("\n")) {
    const match = PAGE_LINE.exec(line);
    const size = match === null ? null : decodeSize(Number(match[2]));
    if (match === null || size === null) return null;
    objects.push({ plaintext_digest: match[1]!, size });
  }
  return objects.length > MAX_SNAPSHOT_PAGE_OBJECTS ? null : objects;
}

function decodeObjectBytes(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export function decodeSnapshotPage(value: unknown): SnapshotPage | null {
  if (!isRecord(value) || !exactKeys(value, ["protocol", "origin", "page", "objects"])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  const page = value.page;
  if (origin === null || typeof page !== "number" || !Number.isSafeInteger(page) || page < 0 || page >= MAX_SNAPSHOT_PAGES) {
    return null;
  }
  if (!Array.isArray(value.objects) || value.objects.length === 0 || value.objects.length > MAX_SNAPSHOT_PAGE_OBJECTS) {
    return null;
  }
  const objects: SnapshotObjectRef[] = [];
  for (const entry of value.objects) {
    const object = decodeSnapshotObject(entry);
    const previous = objects.at(-1);
    if (object === null || (previous !== undefined && previous.plaintext_digest >= object.plaintext_digest)) return null;
    objects.push(object);
  }
  return { protocol: PROTOCOL, origin, page, objects };
}

export function decodeSnapshotCommit(value: unknown): SnapshotCommit | null {
  if (!isRecord(value) || !exactKeys(value, ["protocol", "origin", "base_revision", "snapshot"])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  const baseRevision = decodeRevision(value.base_revision);
  const snapshot = value.snapshot;
  if (origin === null || baseRevision === null) return null;
  if (!isRecord(snapshot) || !exactKeys(snapshot, [
    "id", "created_at", "schema_version", "db_digest", "db_size", "chunks", "objects", "object_bytes", "objects_digest",
  ])) return null;
  const id = decodeDigest(snapshot.id);
  const dbDigest = decodeDigest(snapshot.db_digest);
  const createdAt = decodeTimestamp(snapshot.created_at);
  const schemaVersion = decodeRevision(snapshot.schema_version);
  const dbSize = typeof snapshot.db_size === "number"
    && Number.isSafeInteger(snapshot.db_size) && snapshot.db_size >= 0 && snapshot.db_size <= MAX_SNAPSHOT_BYTES
    ? snapshot.db_size
    : null;
  if (id === null || dbDigest === null || id !== dbDigest) return null;
  if (createdAt === null || schemaVersion === null || schemaVersion > 1_000 || dbSize === null) return null;
  if (!Array.isArray(snapshot.chunks) || snapshot.chunks.length === 0 || snapshot.chunks.length > MAX_SNAPSHOT_CHUNKS) {
    return null;
  }
  const objects = snapshot.objects;
  if (typeof objects !== "number" || !Number.isSafeInteger(objects) || objects < 0 || objects > MAX_SNAPSHOT_OBJECTS) {
    return null;
  }
  const objectBytes = decodeObjectBytes(snapshot.object_bytes);
  const objectsDigest = decodeDigest(snapshot.objects_digest);
  if (objectBytes === null || objectsDigest === null) return null;
  const chunks: SnapshotChunkRef[] = [];
  let cursor = 0;
  for (const entry of snapshot.chunks) {
    const chunk = decodeSnapshotChunk(entry);
    if (chunk === null) return null;
    cursor += chunk.size;
    if (cursor > MAX_SNAPSHOT_BYTES) return null;
    chunks.push(chunk);
  }
  if (cursor !== dbSize) return null;
  return {
    protocol: PROTOCOL,
    origin,
    base_revision: baseRevision,
    snapshot: {
      id, created_at: createdAt, schema_version: schemaVersion, db_digest: dbDigest, db_size: dbSize, chunks,
      objects, object_bytes: objectBytes, objects_digest: objectsDigest,
    },
  };
}

export function decodeSnapshotVerifyRequest(value: unknown): SnapshotVerifyRequest | null {
  if (!isRecord(value) || !exactKeys(value, ["protocol", "origin"])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  return origin === null ? null : { protocol: PROTOCOL, origin };
}

export function decodeGcRequest(value: unknown): GcRequest | null {
  if (!isRecord(value) || !exactKeys(value, ["protocol", "origin", "apply"])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  if (origin === null || typeof value.apply !== "boolean") return null;
  return { protocol: PROTOCOL, origin, apply: value.apply };
}

export function decodeObjectReplaceRequest(value: unknown): ObjectReplaceRequest | null {
  if (!isRecord(value) || !exactKeys(value, [
    "protocol", "origin", "plaintext_digest", "plaintext_size", "encoding", "ciphertext_digest", "ciphertext_size",
  ])) return null;
  if (decodeProtocol(value.protocol) === null) return null;
  const origin = decodeOrigin(value.origin);
  const plaintext = decodeDigest(value.plaintext_digest);
  const ciphertext = decodeDigest(value.ciphertext_digest);
  const plaintextSize = decodeSize(value.plaintext_size);
  const ciphertextSize = decodeCiphertextSize(value.ciphertext_size);
  const encoding = value.encoding === "identity" || value.encoding === "zlib" ? value.encoding : null;
  if (origin === null || plaintext === null || ciphertext === null) return null;
  if (plaintextSize === null || ciphertextSize === null || encoding === null) return null;
  return {
    protocol: PROTOCOL, origin, plaintext_digest: plaintext, plaintext_size: plaintextSize,
    encoding, ciphertext_digest: ciphertext, ciphertext_size: ciphertextSize,
  };
}

function encodeBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
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

function validCursorPart(value: unknown): value is string {
  return typeof value === "string" && value !== "" && value.length <= MAX_CURSOR_PART_CHARS;
}

export function encodeCursor(parts: readonly string[]): string {
  if (parts.length === 0 || parts.length > MAX_CURSOR_PARTS || !parts.every(validCursorPart)) {
    throw new Error("cursor parts are invalid");
  }
  const encoded = encodeBase64Url(new TextEncoder().encode(JSON.stringify(parts)));
  if (encoded.length > MAX_CURSOR_CHARS) throw new Error("cursor is too long");
  return encoded;
}

export function decodeCursor(value: string): readonly string[] | null {
  if (value === "" || value.length > MAX_CURSOR_CHARS) return null;
  const bytes = decodeBase64Url(value);
  if (bytes === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
  if (!Array.isArray(parsed) || parsed.length === 0 || parsed.length > MAX_CURSOR_PARTS) return null;
  if (!parsed.every(validCursorPart)) return null;
  return parsed;
}
