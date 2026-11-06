DROP TABLE snapshot_object;
DROP TABLE snapshot_chunk;
DROP TABLE snapshot;

CREATE TABLE snapshot(
    id TEXT PRIMARY KEY,
    origin_host TEXT NOT NULL,
    created_at REAL NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version >= 0),
    db_digest TEXT NOT NULL,
    db_size INTEGER NOT NULL CHECK(db_size >= 0),
    objects INTEGER NOT NULL CHECK(objects >= 0),
    object_bytes INTEGER NOT NULL CHECK(object_bytes >= 0),
    objects_digest TEXT NOT NULL,
    verified_at REAL,
    revision INTEGER NOT NULL CHECK(revision >= 0)
);

CREATE TABLE snapshot_chunk(
    snapshot TEXT NOT NULL REFERENCES snapshot(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    plaintext_digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    ciphertext_digest TEXT NOT NULL,
    PRIMARY KEY(snapshot, ordinal)
);

CREATE INDEX snapshot_origin_created ON snapshot(origin_host, created_at DESC);
CREATE INDEX snapshot_origin_revision ON snapshot(origin_host, revision);
CREATE INDEX snapshot_chunk_plaintext ON snapshot_chunk(plaintext_digest);
CREATE INDEX snapshot_chunk_ciphertext ON snapshot_chunk(ciphertext_digest);

CREATE TRIGGER snapshot_revision_fence
BEFORE INSERT ON snapshot
WHEN NEW.revision != COALESCE((SELECT revision FROM origin_sync WHERE origin_host = NEW.origin_host), 0) + 1
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;
