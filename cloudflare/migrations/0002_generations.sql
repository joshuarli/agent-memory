CREATE INDEX IF NOT EXISTS generation_origin_revision ON generation(origin_host, revision);
CREATE INDEX IF NOT EXISTS tombstone_origin_revision ON tombstone(origin_host, revision);

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
