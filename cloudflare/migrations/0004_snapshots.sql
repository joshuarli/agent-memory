CREATE INDEX IF NOT EXISTS snapshot_origin_created ON snapshot(origin_host, created_at DESC);
CREATE INDEX IF NOT EXISTS snapshot_origin_revision ON snapshot(origin_host, revision);
CREATE INDEX IF NOT EXISTS remote_object_gc ON remote_object(origin_host, gc_pending_at);

CREATE TRIGGER IF NOT EXISTS snapshot_revision_fence
BEFORE INSERT ON snapshot
WHEN NEW.revision != COALESCE((SELECT revision FROM origin_sync WHERE origin_host = NEW.origin_host), 0) + 1
BEGIN
    SELECT RAISE(ABORT, 'stale_revision');
END;
