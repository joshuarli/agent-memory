CREATE TABLE IF NOT EXISTS learning_state(
    origin_host TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    epoch INTEGER NOT NULL DEFAULT 1 CHECK(epoch >= 1),
    digest TEXT,
    records INTEGER NOT NULL DEFAULT 0 CHECK(records >= 0),
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS learning_fence(
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    changed INTEGER NOT NULL CHECK(changed IN (0, 1))
);

CREATE TRIGGER IF NOT EXISTS learning_fence_insert
BEFORE INSERT ON learning_fence
WHEN NEW.changed = 0
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;

CREATE TRIGGER IF NOT EXISTS learning_fence_update
BEFORE UPDATE ON learning_fence
WHEN NEW.changed = 0
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;

CREATE TRIGGER IF NOT EXISTS learning_state_revision_fence_insert
BEFORE INSERT ON learning_state
WHEN NOT EXISTS (SELECT 1 FROM learning_state WHERE origin_host = NEW.origin_host)
  AND NEW.revision != 1
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;

CREATE TRIGGER IF NOT EXISTS learning_state_revision_fence_update
BEFORE UPDATE ON learning_state
WHEN NEW.revision != OLD.revision AND NEW.revision != OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;

CREATE TABLE IF NOT EXISTS learning_experience(
    origin_host TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    remote_id INTEGER NOT NULL,
    project_key TEXT NOT NULL,
    project_label TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('success','failure','decision','gotcha','method')),
    claim TEXT NOT NULL,
    applies_when TEXT NOT NULL,
    identifiers TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    basis TEXT NOT NULL CHECK(basis IN ('deterministic','reviewed')),
    detector TEXT NOT NULL,
    revision TEXT,
    worktree TEXT,
    observed_at REAL NOT NULL,
    captured_at REAL NOT NULL,
    validity TEXT NOT NULL,
    tags TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('retained','retired','superseded','withdrawn')),
    state_reason TEXT,
    state_at REAL,
    sync_revision INTEGER NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0 CHECK(pending IN (0,1)),
    PRIMARY KEY(origin_host, epoch, remote_id)
);
CREATE INDEX IF NOT EXISTS learning_experience_project ON learning_experience(project_key, state, observed_at DESC, remote_id DESC);
CREATE INDEX IF NOT EXISTS learning_experience_origin ON learning_experience(origin_host, state, observed_at DESC, remote_id DESC);
CREATE INDEX IF NOT EXISTS learning_experience_kind ON learning_experience(kind, observed_at DESC, remote_id DESC);
CREATE INDEX IF NOT EXISTS learning_experience_state_observed ON learning_experience(state, observed_at DESC, remote_id DESC);
CREATE INDEX IF NOT EXISTS learning_experience_observed ON learning_experience(observed_at DESC, remote_id DESC);

CREATE TABLE IF NOT EXISTS learning_memory(
    origin_host TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    remote_id INTEGER NOT NULL,
    project_key TEXT NOT NULL,
    project_label TEXT,
    action TEXT NOT NULL,
    applicability TEXT NOT NULL,
    revision TEXT,
    validity TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('approved','superseded')),
    created_at REAL NOT NULL,
    sync_revision INTEGER NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0 CHECK(pending IN (0,1)),
    PRIMARY KEY(origin_host, epoch, remote_id)
);
CREATE INDEX IF NOT EXISTS learning_memory_project ON learning_memory(project_key, state, created_at DESC, remote_id DESC);
CREATE INDEX IF NOT EXISTS learning_memory_origin ON learning_memory(origin_host, state, created_at DESC, remote_id DESC);
CREATE INDEX IF NOT EXISTS learning_memory_state_created ON learning_memory(state, created_at DESC, remote_id DESC);
CREATE INDEX IF NOT EXISTS learning_memory_created ON learning_memory(created_at DESC, remote_id DESC);

CREATE TABLE IF NOT EXISTS learning_source(
    origin_host TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    record TEXT NOT NULL CHECK(record IN ('experience','memory')),
    remote_id INTEGER NOT NULL,
    session TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    digest TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY(origin_host, epoch, record, remote_id, session, position, role)
);
CREATE INDEX IF NOT EXISTS learning_source_record ON learning_source(origin_host, record, remote_id);
CREATE INDEX IF NOT EXISTS learning_source_session ON learning_source(origin_host, session);

CREATE TABLE IF NOT EXISTS learning_link(
    origin_host TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    newer INTEGER NOT NULL,
    older INTEGER NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN ('supersedes','contradicts')),
    PRIMARY KEY(origin_host, epoch, newer, older, relation)
);

CREATE TABLE IF NOT EXISTS learning_search(
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_host TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    record TEXT NOT NULL CHECK(record IN ('experience','memory')),
    remote_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(origin_host, epoch, record, remote_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text, content='learning_search', content_rowid='rowid',
    tokenize = "unicode61 remove_diacritics 2 tokenchars '-_./'"
);
CREATE TRIGGER IF NOT EXISTS learning_search_ai AFTER INSERT ON learning_search BEGIN
    INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS learning_search_ad AFTER DELETE ON learning_search BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS learning_search_au AFTER UPDATE ON learning_search BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text);
END;
