import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ERROR_CODES,
  GC_GRACE_SECONDS,
  HARNESSES,
  MAX_CURSOR_CHARS,
  MAX_GENERATION_ARTIFACTS,
  MAX_GENERATION_OBJECTS,
  MAX_JSON_BYTES,
  MAX_MISSING_OBJECTS,
  MAX_OBJECT_BYTES,
  MAX_PAGE,
  MAX_SNAPSHOT_BYTES,
  MAX_SNAPSHOT_CHUNKS,
  MAX_SNAPSHOT_OBJECTS,
  MAX_SNAPSHOT_PAGE_OBJECTS,
  MAX_SOURCE_BYTES,
  MAX_TOMBSTONES,
  PROTOCOL,
  decodeCursor,
  decodeDigest,
  decodeGcRequest,
  decodeGenerationManifest,
  decodeHello,
  decodeMissingRequest,
  decodeObjectReplaceRequest,
  decodeOrigin,
  decodeSnapshotCommit,
  decodeSnapshotPage,
  decodeSnapshotPageBody,
  encodeSnapshotPage,
  decodeTombstoneRequest,
  encodeCursor,
} from "../src/protocol.ts";

const FIXTURE = JSON.parse(
  readFileSync(new URL("../../tests/fixtures/mirror_protocol.json", import.meta.url), "utf8"),
);

test("the Worker constants match the shared protocol fixture", () => {
  assert.equal(PROTOCOL, FIXTURE.protocol);
  assert.equal(MAX_JSON_BYTES, FIXTURE.limits.json_bytes);
  assert.equal(MAX_OBJECT_BYTES, FIXTURE.limits.object_bytes);
  assert.equal(MAX_MISSING_OBJECTS, FIXTURE.limits.missing_objects);
  assert.equal(MAX_CURSOR_CHARS, FIXTURE.limits.cursor_chars);
  assert.deepEqual([...ERROR_CODES].sort(), [...FIXTURE.error_codes].sort());
});

test("digest and origin validators agree with every fixture", () => {
  for (const item of FIXTURE.digests) {
    assert.equal(decodeDigest(item.value) === null, !item.ok, item.name);
  }
  for (const item of FIXTURE.origins) {
    assert.equal(decodeOrigin(item.value) === null, !item.ok, item.name);
  }
});

test("hello and missing-object validators agree with every fixture", () => {
  for (const item of FIXTURE.hello) {
    const decoded = decodeHello(item.value);
    assert.equal(decoded === null, !item.ok, item.name);
    if (decoded !== null) {
      assert.equal(decoded.protocol, PROTOCOL);
      assert.equal(decoded.origin, item.value.origin);
    }
  }
  for (const item of FIXTURE.missing) {
    const decoded = decodeMissingRequest(item.value);
    assert.equal(decoded === null, !item.ok, item.name);
    if (decoded !== null) assert.equal(decoded.objects.length, item.value.objects.length);
  }
});

test("cursor round trips and rejects every invalid fixture", () => {
  for (const item of FIXTURE.cursors) {
    if (item.parts !== undefined) {
      if (item.ok) {
        const encoded = encodeCursor(item.parts);
        assert.ok(encoded.length <= MAX_CURSOR_CHARS, item.name);
        if (item.encoded !== undefined) assert.equal(encoded, item.encoded, item.name);
        assert.deepEqual(decodeCursor(encoded), item.parts, item.name);
      } else {
        assert.throws(() => encodeCursor(item.parts), item.name);
      }
    } else {
      assert.equal(decodeCursor(item.value) === null, !item.ok, item.name);
    }
  }
});

test("a missing-object request over the count limit is refused", () => {
  const objects = Array.from({ length: MAX_MISSING_OBJECTS + 1 }, (_value, index) => ({
    plaintext_digest: index.toString(16).padStart(64, "0"),
    size: 0,
  }));
  assert.equal(decodeMissingRequest({ protocol: 1, origin: "origin-a", objects }), null);
});

test("the manifest validator agrees with every fixture", async () => {
  for (const item of FIXTURE.manifests) {
    const decoded = await decodeGenerationManifest(item.value);
    assert.equal(decoded === null, !item.ok, item.name);
    if (decoded !== null) {
      assert.equal(decoded.source_digest, item.value.source_digest, item.name);
      assert.equal(decoded.chunks.length, item.value.chunks.length, item.name);
      assert.equal(decoded.manifest_object, item.value.manifest_object, item.name);
    }
  }
});

test("the tombstone validator agrees with every fixture", () => {
  for (const item of FIXTURE.tombstone_pushes) {
    const decoded = decodeTombstoneRequest(item.value);
    assert.equal(decoded === null, !item.ok, item.name);
    if (decoded !== null) assert.equal(decoded.tombstones.length, item.value.tombstones.length, item.name);
  }
});

test("the snapshot validators agree with every fixture", () => {
  for (const item of FIXTURE.snapshots) {
    const decoded = decodeSnapshotCommit(item.value);
    assert.equal(decoded === null, !item.ok, item.name);
    if (decoded !== null) {
      assert.equal(decoded.snapshot.id, item.value.snapshot.db_digest, item.name);
      assert.equal(decoded.snapshot.chunks.length, item.value.snapshot.chunks.length, item.name);
      assert.equal(decoded.snapshot.objects, item.value.snapshot.objects, item.name);
    }
  }
  for (const item of FIXTURE.snapshot_pages) {
    const decoded = decodeSnapshotPage(item.value);
    assert.equal(decoded === null, !item.ok, item.name);
    if (decoded !== null) assert.equal(decoded.objects.length, item.value.objects.length, item.name);
  }
  const body = FIXTURE.snapshot_page_body;
  assert.equal(encodeSnapshotPage(body.objects), body.body);
  assert.deepEqual(decodeSnapshotPageBody(body.body), body.objects);
  for (const item of FIXTURE.gc_requests) {
    const decoded = decodeGcRequest(item.value);
    assert.equal(decoded === null, !item.ok, item.name);
    if (decoded !== null) assert.equal(decoded.apply, item.value.apply, item.name);
  }
  for (const item of FIXTURE.replace_requests) {
    const decoded = decodeObjectReplaceRequest(item.value);
    assert.equal(decoded === null, !item.ok, item.name);
    if (decoded !== null) assert.equal(decoded.ciphertext_digest, item.value.ciphertext_digest, item.name);
  }
});

test("harnesses and generation limits match the shared fixture", () => {
  assert.deepEqual([...HARNESSES], FIXTURE.harnesses);
  assert.equal(MAX_GENERATION_OBJECTS, FIXTURE.limits.generation_objects);
  assert.equal(MAX_GENERATION_ARTIFACTS, FIXTURE.limits.generation_artifacts);
  assert.equal(MAX_TOMBSTONES, FIXTURE.limits.tombstones);
  assert.equal(MAX_PAGE, FIXTURE.limits.page);
  assert.equal(MAX_SNAPSHOT_BYTES, FIXTURE.limits.snapshot_bytes);
  assert.equal(MAX_SOURCE_BYTES, FIXTURE.limits.source_bytes);
  assert.equal(MAX_SNAPSHOT_CHUNKS, FIXTURE.limits.snapshot_chunks);
  assert.equal(MAX_SNAPSHOT_OBJECTS, FIXTURE.limits.snapshot_objects);
  assert.equal(MAX_SNAPSHOT_PAGE_OBJECTS, FIXTURE.limits.snapshot_page_objects);
  assert.equal(GC_GRACE_SECONDS, FIXTURE.limits.gc_grace_seconds);
});
