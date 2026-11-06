CREATE TABLE IF NOT EXISTS store_meta(
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS collector(
    service_token_id TEXT PRIMARY KEY,
    origin_host TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS origin_sync(
    origin_host TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    last_change_at REAL
);

CREATE TABLE IF NOT EXISTS remote_object(
    plaintext_digest TEXT PRIMARY KEY,
    plaintext_size INTEGER NOT NULL CHECK(plaintext_size >= 0),
    encoding TEXT NOT NULL,
    ciphertext_digest TEXT NOT NULL UNIQUE,
    ciphertext_size INTEGER NOT NULL CHECK(ciphertext_size >= 0),
    origin_host TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_referenced_at REAL,
    gc_pending_at REAL
);

CREATE TABLE IF NOT EXISTS generation(
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
    manifest_object TEXT NOT NULL REFERENCES remote_object(ciphertext_digest),
    UNIQUE(origin_host, manifest_digest)
);

CREATE TABLE IF NOT EXISTS generation_object(
    generation INTEGER NOT NULL REFERENCES generation(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    plaintext_digest TEXT NOT NULL REFERENCES remote_object(plaintext_digest),
    size INTEGER NOT NULL CHECK(size >= 0),
    PRIMARY KEY(generation, ordinal)
);

CREATE TABLE IF NOT EXISTS tombstone(
    origin_host TEXT NOT NULL,
    harness TEXT NOT NULL,
    native_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    forgotten_at REAL NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    PRIMARY KEY(origin_host, harness, native_id)
);

CREATE TABLE IF NOT EXISTS snapshot(
    id TEXT PRIMARY KEY,
    origin_host TEXT NOT NULL,
    created_at REAL NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version >= 0),
    db_digest TEXT NOT NULL,
    db_size INTEGER NOT NULL CHECK(db_size >= 0),
    objects INTEGER NOT NULL CHECK(objects >= 0),
    object_bytes INTEGER NOT NULL CHECK(object_bytes >= 0),
    verified_at REAL,
    revision INTEGER NOT NULL CHECK(revision >= 0)
);

CREATE TABLE IF NOT EXISTS snapshot_chunk(
    snapshot TEXT NOT NULL REFERENCES snapshot(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    plaintext_digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    ciphertext_digest TEXT NOT NULL REFERENCES remote_object(ciphertext_digest),
    PRIMARY KEY(snapshot, ordinal)
);

CREATE TABLE IF NOT EXISTS snapshot_object(
    snapshot TEXT NOT NULL REFERENCES snapshot(id),
    plaintext_digest TEXT NOT NULL REFERENCES remote_object(plaintext_digest),
    size INTEGER NOT NULL CHECK(size >= 0),
    PRIMARY KEY(snapshot, plaintext_digest)
);

CREATE INDEX IF NOT EXISTS generation_origin_committed ON generation(origin_host, committed_at DESC);
CREATE INDEX IF NOT EXISTS remote_object_ciphertext ON remote_object(ciphertext_digest);
CREATE INDEX IF NOT EXISTS tombstone_origin_session ON tombstone(origin_host, harness, native_id);

INSERT OR IGNORE INTO store_meta(id, created_at) VALUES ('usr-memories', CAST(strftime('%s', 'now') AS REAL));
