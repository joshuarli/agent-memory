ALTER TABLE learning_experience ADD COLUMN record_digest TEXT NOT NULL DEFAULT '';
ALTER TABLE learning_memory ADD COLUMN record_digest TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS learning_reconcile(
    origin_host TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL CHECK(epoch >= 1),
    digest TEXT NOT NULL,
    records INTEGER NOT NULL CHECK(records >= 0),
    started_at REAL NOT NULL
);
