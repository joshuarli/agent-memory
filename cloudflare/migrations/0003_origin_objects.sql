PRAGMA defer_foreign_keys = ON;

CREATE TABLE remote_object_next(
    origin_host TEXT NOT NULL,
    plaintext_digest TEXT NOT NULL,
    plaintext_size INTEGER NOT NULL CHECK(plaintext_size >= 0),
    encoding TEXT NOT NULL,
    ciphertext_digest TEXT NOT NULL,
    ciphertext_size INTEGER NOT NULL CHECK(ciphertext_size >= 0),
    created_at REAL NOT NULL,
    last_referenced_at REAL,
    gc_pending_at REAL,
    PRIMARY KEY(origin_host, plaintext_digest),
    UNIQUE(origin_host, ciphertext_digest)
);

INSERT INTO remote_object_next(
    origin_host, plaintext_digest, plaintext_size, encoding, ciphertext_digest,
    ciphertext_size, created_at, last_referenced_at, gc_pending_at
)
SELECT origin_host, plaintext_digest, plaintext_size, encoding, ciphertext_digest,
       ciphertext_size, created_at, last_referenced_at, gc_pending_at
FROM remote_object;

CREATE TABLE generation_next(
    id INTEGER PRIMARY KEY,
    origin_host TEXT NOT NULL,
    harness TEXT NOT NULL,
    native_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    source_size INTEGER NOT NULL CHECK(source_size >= 0),
    captured_at REAL NOT NULL,
    committed_at REAL NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    manifest_object TEXT NOT NULL,
    UNIQUE(origin_host, manifest_digest),
    FOREIGN KEY(origin_host, manifest_object)
        REFERENCES remote_object_next(origin_host, ciphertext_digest)
);

INSERT INTO generation_next SELECT
    id, origin_host, harness, native_id, manifest_digest, source_digest, source_size,
    captured_at, committed_at, revision, manifest_object
FROM generation;

CREATE TABLE generation_object_next(
    generation INTEGER NOT NULL REFERENCES generation_next(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    plaintext_digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    PRIMARY KEY(generation, ordinal)
);

INSERT INTO generation_object_next SELECT generation, ordinal, plaintext_digest, size
FROM generation_object;

CREATE TABLE snapshot_chunk_next(
    snapshot TEXT NOT NULL REFERENCES snapshot(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    plaintext_digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    ciphertext_digest TEXT NOT NULL,
    PRIMARY KEY(snapshot, ordinal)
);

INSERT INTO snapshot_chunk_next SELECT snapshot, ordinal, plaintext_digest, size, ciphertext_digest
FROM snapshot_chunk;

CREATE TABLE snapshot_object_next(
    snapshot TEXT NOT NULL REFERENCES snapshot(id),
    plaintext_digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    PRIMARY KEY(snapshot, plaintext_digest)
);

INSERT INTO snapshot_object_next SELECT snapshot, plaintext_digest, size
FROM snapshot_object;

DROP TABLE snapshot_object;
DROP TABLE snapshot_chunk;
DROP TABLE generation_object;
DROP TABLE generation;
DROP TABLE remote_object;

ALTER TABLE remote_object_next RENAME TO remote_object;
ALTER TABLE generation_next RENAME TO generation;
ALTER TABLE generation_object_next RENAME TO generation_object;
ALTER TABLE snapshot_chunk_next RENAME TO snapshot_chunk;
ALTER TABLE snapshot_object_next RENAME TO snapshot_object;

CREATE INDEX IF NOT EXISTS generation_origin_committed ON generation(origin_host, committed_at DESC);
CREATE INDEX IF NOT EXISTS generation_origin_revision ON generation(origin_host, revision);
CREATE INDEX IF NOT EXISTS remote_object_ciphertext ON remote_object(origin_host, ciphertext_digest);

CREATE TRIGGER IF NOT EXISTS generation_revision_fence
BEFORE INSERT ON generation
WHEN NEW.revision != COALESCE((SELECT revision FROM origin_sync WHERE origin_host = NEW.origin_host), 0) + 1
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;

CREATE TRIGGER IF NOT EXISTS generation_tombstone_guard
BEFORE INSERT ON generation
WHEN EXISTS (
    SELECT 1 FROM tombstone
    WHERE origin_host = NEW.origin_host AND harness = NEW.harness AND native_id = NEW.native_id
)
BEGIN
    SELECT RAISE(ABORT, 'tombstoned');
END;

CREATE TRIGGER IF NOT EXISTS tombstone_revision_fence_insert
BEFORE INSERT ON tombstone
WHEN NEW.revision != COALESCE((SELECT revision FROM origin_sync WHERE origin_host = NEW.origin_host), 0) + 1
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;

CREATE TRIGGER IF NOT EXISTS tombstone_revision_fence_update
BEFORE UPDATE ON tombstone
WHEN NEW.revision != COALESCE((SELECT revision FROM origin_sync WHERE origin_host = NEW.origin_host), 0) + 1
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;
