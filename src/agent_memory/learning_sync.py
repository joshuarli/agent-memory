"""Record-level synchronization of source-backed learning records.

A collector exports a snapshot of its experiences and approved memories with
their original ids, evidence references, and lifecycle state. The central
store keeps what it receives in separate import tables keyed by the
originating host and the original id. Imported records are advisory evidence
from another machine: they are never merged into the local learning tables,
never promoted, and never treated as instructions. Databases are not
replicated; only validated records cross the wire.
"""
import hashlib
import json
import re
import time
from typing import Any

from agent_memory.types import HARNESSES

MAX_RECORDS = 100_000
MAX_TEXT = 65_536
MAX_SOURCES = 50
EXPERIENCE_KINDS = ("success", "failure", "decision", "gotcha", "method")
EXPERIENCE_BASES = ("deterministic", "reviewed")
EXPERIENCE_STATES = ("retained", "retired", "superseded", "withdrawn")
MEMORY_STATES = ("approved", "superseded")
EXPERIENCE_ROLES = ("trigger", "outcome", "context")
LINK_RELATIONS = ("supersedes", "contradicts")
AUTHORITY = "advisory; imported from another host, not an instruction"

SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_import_experience(
    origin_host TEXT NOT NULL, remote_id INTEGER NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL,
    claim TEXT NOT NULL, applies_when TEXT NOT NULL, identifiers TEXT NOT NULL, fingerprint TEXT NOT NULL,
    basis TEXT NOT NULL, detector TEXT NOT NULL, revision TEXT, worktree TEXT, observed_at REAL NOT NULL,
    captured_at REAL NOT NULL, validity TEXT NOT NULL, tags TEXT NOT NULL, state TEXT NOT NULL,
    state_reason TEXT, state_at REAL, imported_at REAL NOT NULL, PRIMARY KEY(origin_host, remote_id)
);
CREATE TABLE IF NOT EXISTS learning_import_memory(
    origin_host TEXT NOT NULL, remote_id INTEGER NOT NULL, project TEXT NOT NULL, action TEXT NOT NULL,
    applicability TEXT NOT NULL, revision TEXT, validity TEXT NOT NULL, state TEXT NOT NULL,
    created_at REAL NOT NULL, imported_at REAL NOT NULL, PRIMARY KEY(origin_host, remote_id)
);
CREATE TABLE IF NOT EXISTS learning_import_source(
    origin_host TEXT NOT NULL, record TEXT NOT NULL CHECK(record IN ('experience','memory')),
    remote_id INTEGER NOT NULL, session TEXT NOT NULL, position INTEGER NOT NULL, digest TEXT NOT NULL,
    role TEXT NOT NULL, PRIMARY KEY(origin_host, record, remote_id, session, position, role)
);
CREATE INDEX IF NOT EXISTS learning_import_source_session ON learning_import_source(session);
CREATE TABLE IF NOT EXISTS learning_import_link(
    origin_host TEXT NOT NULL, newer INTEGER NOT NULL, older INTEGER NOT NULL, relation TEXT NOT NULL,
    PRIMARY KEY(origin_host, newer, older, relation)
);
CREATE TABLE IF NOT EXISTS learning_import_state(
    origin_host TEXT PRIMARY KEY, digest TEXT NOT NULL, records INTEGER NOT NULL, received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transfer_learning(
    remote TEXT PRIMARY KEY, digest TEXT NOT NULL, durable_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_project_label(
    project_key TEXT PRIMARY KEY, label TEXT NOT NULL, updated_at REAL NOT NULL
);
"""

JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_change(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    record TEXT NOT NULL CHECK(record IN ('experience','memory')),
    remote_id INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
    changed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_change_record ON learning_change(record, remote_id);
CREATE TRIGGER IF NOT EXISTS learning_change_experience_ai AFTER INSERT ON learning_experiences
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', new.id, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_experience_au AFTER UPDATE ON learning_experiences
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', new.id, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_experience_ad AFTER DELETE ON learning_experiences
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', old.id, 1, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_source_ai AFTER INSERT ON learning_experience_sources
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', new.experience, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_source_au AFTER UPDATE ON learning_experience_sources
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', new.experience, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_source_ad AFTER DELETE ON learning_experience_sources
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', old.experience, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_link_ai AFTER INSERT ON learning_experience_links
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', new.newer, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_link_au AFTER UPDATE ON learning_experience_links
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', new.newer, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_link_ad AFTER DELETE ON learning_experience_links
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('experience', old.newer, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_memory_ai AFTER INSERT ON learning_memories
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('memory', new.finding, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_memory_au AFTER UPDATE ON learning_memories
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('memory', new.finding, 0, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_memory_ad AFTER DELETE ON learning_memories
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      VALUES('memory', old.finding, 1, CAST(strftime('%s','now') AS REAL)); END;
CREATE TRIGGER IF NOT EXISTS learning_change_incident_ai AFTER INSERT ON learning_incidents
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      SELECT 'memory', new.finding, 0, CAST(strftime('%s','now') AS REAL)
      WHERE EXISTS(SELECT 1 FROM learning_memories WHERE finding = new.finding); END;
CREATE TRIGGER IF NOT EXISTS learning_change_incident_au AFTER UPDATE ON learning_incidents
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      SELECT 'memory', new.finding, 0, CAST(strftime('%s','now') AS REAL)
      WHERE EXISTS(SELECT 1 FROM learning_memories WHERE finding = new.finding); END;
CREATE TRIGGER IF NOT EXISTS learning_change_incident_ad AFTER DELETE ON learning_incidents
BEGIN INSERT INTO learning_change(record, remote_id, deleted, changed_at)
      SELECT 'memory', old.finding, 0, CAST(strftime('%s','now') AS REAL)
      WHERE EXISTS(SELECT 1 FROM learning_memories WHERE finding = old.finding); END;
"""


class SnapshotError(ValueError):
    pass


def ensure_schema(db):
    db.executescript(SCHEMA)


def has_learning(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'learning_schema'").fetchone() is not None


def ensure_journal(db):
    created = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'learning_change'").fetchone() is None
    db.executescript(JOURNAL_SCHEMA)
    if created:
        with db:
            db.execute("INSERT INTO learning_change(record, remote_id, deleted, changed_at) "
                       "SELECT 'experience', id, 0, CAST(strftime('%s','now') AS REAL) FROM learning_experiences")
            db.execute("INSERT INTO learning_change(record, remote_id, deleted, changed_at) "
                       "SELECT 'memory', finding, 0, CAST(strftime('%s','now') AS REAL) FROM learning_memories")


def requeue(db):
    """Queue every current record for projection without duplicating pending changes."""
    if not has_learning(db):
        return 0
    ensure_journal(db)
    now = time.time()
    with db:
        inserted = 0
        for record, table, column in (
                ("experience", "learning_experiences", "id"),
                ("memory", "learning_memories", "finding")):
            cursor = db.execute(
                "INSERT INTO learning_change(record, remote_id, deleted, changed_at) "
                "SELECT ?, %s, 0, ? FROM %s AS source WHERE NOT EXISTS ("
                " SELECT 1 FROM learning_change c WHERE c.record = ? AND c.remote_id = source.%s)"
                % (column, table, column),
                (record, now, record))
            inserted += cursor.rowcount
        return inserted


def project_key(identity):
    return hashlib.sha256(("agent-memory-project:" + str(identity)).encode("utf-8")).hexdigest()[:16]


def project_labels(db) -> dict[str, str]:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mirror_project_label'").fetchone() is None:
        return {}
    return {row[0]: row[1] for row in db.execute("SELECT project_key, label FROM mirror_project_label ORDER BY project_key")}


def set_project_label(db, key, label, now=None):
    if not isinstance(key, str) or len(key) != 16 or any(character not in "0123456789abcdef" for character in key):
        raise SnapshotError("project key must be 16 lowercase hex characters")
    if not isinstance(label, str) or not 1 <= len(label) <= 128 or any(ord(character) < 32 for character in label):
        raise SnapshotError("project label must be 1..128 printable characters")
    with db:
        db.execute("INSERT INTO mirror_project_label(project_key, label, updated_at) VALUES(?, ?, ?) "
                   "ON CONFLICT(project_key) DO UPDATE SET label=excluded.label, updated_at=excluded.updated_at",
                   (key, label, time.time() if now is None else float(now)))
    return key


def journal_head(db) -> int:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'learning_change'").fetchone() is None:
        return 0
    row = db.execute("SELECT coalesce(max(seq), 0) FROM learning_change").fetchone()
    return int(row[0])


def journal_changes(db, after=0) -> list[dict[str, Any]]:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'learning_change'").fetchone() is None:
        return []
    return [dict(row) for row in db.execute(
        "SELECT seq, record, remote_id, deleted FROM learning_change WHERE seq > ? ORDER BY seq", (int(after),))]


def export_record(db, kind, remote_id) -> dict[str, Any] | None:
    """Describe one local experience or memory, or None when its evidence is not local."""
    if kind == "experience":
        row = db.execute("SELECT * FROM learning_experiences WHERE id = ?", (int(remote_id),)).fetchone()
        if row is None:
            return None
        sources = [dict(item) for item in db.execute(
            "SELECT session, position, digest, role FROM learning_experience_sources WHERE experience = ? "
            "ORDER BY session, position, role", (row["id"],))]
        if any("@" in source["session"] for source in sources):
            return None
        links = [dict(item) for item in db.execute(
            "SELECT older, relation FROM learning_experience_links WHERE newer = ? ORDER BY older, relation", (row["id"],))]
        return {
            "record": "experience", "id": row["id"], "project": row["project"], "kind": row["kind"], "claim": row["claim"],
            "applies_when": row["applies_when"], "identifiers": row["identifiers"], "fingerprint": row["fingerprint"],
            "basis": row["basis"], "detector": row["detector"], "revision": row["revision"], "worktree": row["worktree"],
            "observed_at": row["observed_at"], "captured_at": row["captured_at"], "validity": row["validity"],
            "tags": row["tags"], "state": row["state"], "state_reason": row["state_reason"], "state_at": row["state_at"],
            "sources": sources, "links": links,
        }
    if kind == "memory":
        row = db.execute("SELECT * FROM learning_memories WHERE finding = ?", (int(remote_id),)).fetchone()
        if row is None:
            return None
        sources = [
            {"session": item["session"], "position": item["position"], "digest": item["digest"], "role": item["signal"]}
            for item in db.execute(
                "SELECT session, position, digest, signal FROM learning_incidents WHERE finding = ? "
                "ORDER BY observed_at DESC, id LIMIT ?", (row["finding"], MAX_SOURCES))]
        if any("@" in source["session"] for source in sources):
            return None
        return {
            "record": "memory", "id": row["finding"], "project": row["project"], "action": row["action"],
            "applicability": row["applicability"], "revision": row["revision"], "validity": row["validity"],
            "state": row["state"], "created_at": row["created_at"], "sources": sources, "links": [],
        }
    return None


def export_snapshot(db) -> list[dict[str, Any]]:
    """Describe every experience and memory whose evidence is entirely local to this host."""
    records: list[dict[str, Any]] = []
    for (remote_id,) in db.execute("SELECT id FROM learning_experiences ORDER BY id"):
        record = export_record(db, "experience", remote_id)
        if record is not None:
            records.append(record)
    for (remote_id,) in db.execute("SELECT finding FROM learning_memories ORDER BY finding"):
        record = export_record(db, "memory", remote_id)
        if record is not None:
            records.append(record)
    return records


def export_changes(db, after=0) -> dict[str, Any]:
    """Return the current payload for every record changed after ``after``, with deletions explicit."""
    head = journal_head(db)
    last = {}
    for row in journal_changes(db, after):
        last[(row["record"], int(row["remote_id"]))] = row
    changes = []
    for (kind, remote_id), change in sorted(last.items(), key=lambda item: item[1]["seq"]):
        record = None if change["deleted"] else export_record(db, kind, remote_id)
        if record is None:
            changes.append({"record": kind, "id": remote_id, "deleted": True})
        else:
            changes.append(record)
    return {"head": head, "changes": changes}


def prune_journal(db, through):
    through = int(through)
    with db:
        return db.execute("DELETE FROM learning_change WHERE seq <= ?", (through,)).rowcount


def changes_digest(changes):
    body = json.dumps(changes, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _mirror_source(source, origin_host):
    session = source["session"]
    qualified = session if ":" in session.partition("@")[0] else "codex:" + session
    if "@" not in qualified:
        qualified = qualified + "@" + origin_host
    return dict(source, session=qualified)


def mirror_record(record, labels, origin_host):
    """Replace a local project identity with its hash and qualify source sessions with the origin."""
    if record.get("deleted"):
        return dict(record)
    clean = {key: value for key, value in record.items() if key != "project"}
    key = project_key(record["project"])
    clean["project_key"] = key
    label = labels.get(key)
    if label is not None:
        clean["project_label"] = label
    clean["sources"] = [_mirror_source(source, origin_host) for source in record["sources"]]
    clean["digest"] = changes_digest([clean])
    return clean


def mirror_snapshot(db, labels, origin_host):
    return [mirror_record(record, labels, origin_host) for record in export_snapshot(db)]


def mirror_changes(db, after, labels, origin_host) -> dict[str, Any]:
    result = export_changes(db, after)
    return {"head": result["head"], "changes": [mirror_record(record, labels, origin_host) for record in result["changes"]]}


def snapshot_digest(records):
    body = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _text(record, field) -> str:
    value = record.get(field)
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise SnapshotError("learning record field is not bounded text: " + field)
    return value


def _optional_text(record, field) -> str | None:
    return None if record.get(field) is None else _text(record, field)


def _number(record, field) -> float:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotError("learning record field is not a number: " + field)
    return float(value)


def _optional_number(record, field) -> float | None:
    return None if record.get(field) is None else _number(record, field)


def _integer(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SnapshotError("learning record %s is not a non-negative integer" % what)
    return value


def validate_record(record) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SnapshotError("learning record is not an object")
    kind = record.get("record")
    if kind not in ("experience", "memory"):
        raise SnapshotError("unsupported learning record kind: %r" % (kind,))
    sources = record.get("sources")
    links = record.get("links")
    if not isinstance(sources, list) or len(sources) > 1000 or not isinstance(links, list):
        raise SnapshotError("learning record needs source and link lists")
    clean: dict[str, Any] = {"record": kind, "id": _integer(record.get("id"), "id"), "project": _text(record, "project")}
    roles = EXPERIENCE_ROLES if kind == "experience" else None
    clean_sources = []
    for source in sources:
        if not isinstance(source, dict):
            raise SnapshotError("learning source is not an object")
        role = _text(source, "role")
        if roles is not None and role not in roles:
            raise SnapshotError("unsupported learning source role: %s" % role)
        clean_sources.append({"session": _text(source, "session"), "position": _integer(source.get("position"), "position"),
                              "digest": _text(source, "digest"), "role": role})
    clean["sources"] = clean_sources
    if kind == "experience":
        for field in ("kind", "basis", "state"):
            clean[field] = _text(record, field)
        if clean["kind"] not in EXPERIENCE_KINDS or clean["basis"] not in EXPERIENCE_BASES or clean["state"] not in EXPERIENCE_STATES:
            raise SnapshotError("learning experience has an unsupported kind, basis, or state")
        for field in ("claim", "applies_when", "identifiers", "fingerprint", "detector", "validity", "tags"):
            clean[field] = _text(record, field)
        for field in ("revision", "worktree", "state_reason"):
            clean[field] = _optional_text(record, field)
        clean["observed_at"] = _number(record, "observed_at")
        clean["captured_at"] = _number(record, "captured_at")
        clean["state_at"] = _optional_number(record, "state_at")
        clean_links = []
        for link in links:
            if not isinstance(link, dict) or link.get("relation") not in LINK_RELATIONS:
                raise SnapshotError("learning link is malformed")
            clean_links.append({"older": _integer(link.get("older"), "link target"), "relation": link["relation"]})
        clean["links"] = clean_links
    else:
        if links:
            raise SnapshotError("learning memories carry no links")
        for field in ("action", "applicability", "validity"):
            clean[field] = _text(record, field)
        clean["revision"] = _optional_text(record, "revision")
        clean["state"] = _text(record, "state")
        if clean["state"] not in MEMORY_STATES:
            raise SnapshotError("learning memory has an unsupported state")
        clean["created_at"] = _number(record, "created_at")
        clean["links"] = []
    return clean


PROJECT_KEY = re.compile(r"^[0-9a-f]{16}$")
MAX_LEARNING_CHANGES = 200
MIRROR_EXPERIENCE_KEYS = frozenset({
    "record", "id", "digest", "project_key", "kind", "claim", "applies_when", "identifiers", "fingerprint",
    "basis", "detector", "revision", "worktree", "observed_at", "captured_at", "validity", "tags",
    "state", "state_reason", "state_at", "sources", "links",
})
MIRROR_MEMORY_KEYS = frozenset({
    "record", "id", "digest", "project_key", "action", "applicability", "revision", "validity",
    "state", "created_at", "sources", "links",
})
MIRROR_LABEL = "project_label"
SOURCE_KEYS = frozenset({"session", "position", "digest", "role"})
LINK_KEYS = frozenset({"older", "relation"})


def _mirror_keys(record, required, label):
    keys = set(record)
    if label is not None and MIRROR_LABEL in keys:
        keys = keys - {MIRROR_LABEL}
    return keys == set(required)


def _mirror_label(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(character) < 32 for character in value):
        raise SnapshotError("learning project label must be 1..128 printable characters")
    return value


def _mirror_session(value, origin):
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise SnapshotError("learning source session is not bounded text")
    local, separator, suffix = value.rpartition("@")
    if not separator or not local or suffix != origin or "@" in local:
        raise SnapshotError("learning source session is not qualified with the origin")
    harness, colon, native = local.partition(":")
    if not colon or harness not in HARNESSES or not native:
        raise SnapshotError("learning source session is not a harness session")
    return value


def validate_mirror_record(record, origin) -> dict[str, Any]:
    """Validate the wire form of a learning record, including its project hash and origin."""
    if not isinstance(record, dict):
        raise SnapshotError("learning record is not an object")
    kind = record.get("record")
    if set(record) == {"record", "id", "deleted"}:
        if record.get("deleted") is not True or kind not in ("experience", "memory"):
            raise SnapshotError("learning deletion is malformed")
        return {"record": kind, "id": _integer(record.get("id"), "id"), "deleted": True}
    required = MIRROR_EXPERIENCE_KEYS if kind == "experience" else MIRROR_MEMORY_KEYS if kind == "memory" else None
    if required is None:
        raise SnapshotError("unsupported learning record kind: %r" % (kind,))
    if not _mirror_keys(record, required, MIRROR_LABEL):
        raise SnapshotError("learning record has unrecognised or missing fields")
    key = record.get("project_key")
    if not isinstance(key, str) or not PROJECT_KEY.match(key):
        raise SnapshotError("learning project key is not 16 lowercase hex characters")
    digest = record.get("digest")
    if not isinstance(digest, str) or not re.match(r"^[0-9a-f]{64}$", digest):
        raise SnapshotError("learning record digest is not 64 lowercase hex characters")
    label = record.get("project_label")
    if label is not None:
        _mirror_label(label)
    sources = record.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
                raise SnapshotError("learning source is malformed")
            _mirror_session(source.get("session"), origin)
    links = record.get("links")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict) or set(link) != LINK_KEYS:
                raise SnapshotError("learning link is malformed")
    local = {field: value for field, value in record.items() if field not in ("project_key", MIRROR_LABEL)}
    local["project"] = key
    clean = validate_record(local)
    clean["project_key"] = clean.pop("project")
    clean["digest"] = digest
    if label is not None:
        clean[MIRROR_LABEL] = label
    return clean


def validate_mirror_delta(delta) -> dict[str, Any]:
    if not isinstance(delta, dict) or set(delta) != {"protocol", "origin", "base_revision", "digest", "changes"}:
        raise SnapshotError("learning delta is malformed")
    if delta.get("protocol") != 1:
        raise SnapshotError("learning delta protocol is not supported")
    origin = delta.get("origin")
    if not isinstance(origin, str) or not re.match(r"^[A-Za-z0-9._-]{1,128}$", origin):
        raise SnapshotError("learning delta origin is invalid")
    base = delta.get("base_revision")
    if isinstance(base, bool) or not isinstance(base, int) or base < 0:
        raise SnapshotError("learning delta base revision is invalid")
    digest = delta.get("digest")
    if not isinstance(digest, str) or not re.match(r"^[0-9a-f]{64}$", digest):
        raise SnapshotError("learning delta digest is invalid")
    changes = delta.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_LEARNING_CHANGES:
        raise SnapshotError("learning delta is not a bounded change list")
    seen = set()
    clean = []
    for change in changes:
        validated = validate_mirror_record(change, origin)
        key = (validated["record"], validated["id"])
        if key in seen:
            raise SnapshotError("learning delta repeats a record")
        seen.add(key)
        clean.append(validated)
    return {"protocol": 1, "origin": origin, "base_revision": base, "digest": digest, "changes": clean}


def _session_identity(session):
    harness, separator, native = session.partition(":")
    if separator and harness in HARNESSES:
        return harness, native
    return "codex", session


def _tombstoned(store, origin_host, record):
    return any(store.is_tombstoned(origin_host, *_session_identity(source["session"])) for source in record["sources"])


def import_snapshot(store, origin_host, records, digest):
    """Replace everything previously imported from one origin with a verified snapshot."""
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise SnapshotError("learning snapshot is not a bounded list")
    if snapshot_digest(records) != digest:
        raise SnapshotError("learning snapshot does not match its digest")
    clean = [validate_record(record) for record in records]
    seen = set()
    for record in clean:
        key = (record["record"], record["id"])
        if key in seen:
            raise SnapshotError("learning snapshot repeats a record")
        seen.add(key)
    ensure_schema(store.db)
    db = store.db
    now = time.time()
    db.execute("BEGIN IMMEDIATE")
    try:
        accepted = [record for record in clean if not _tombstoned(store, origin_host, record)]
        for table in ("learning_import_experience", "learning_import_memory", "learning_import_source", "learning_import_link"):
            db.execute("DELETE FROM %s WHERE origin_host = ?" % table, (origin_host,))
        for record in accepted:
            if record["record"] == "experience":
                db.execute(
                    "INSERT INTO learning_import_experience (origin_host, remote_id, project, kind, claim, applies_when, identifiers, "
                    "fingerprint, basis, detector, revision, worktree, observed_at, captured_at, validity, tags, state, state_reason, "
                    "state_at, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (origin_host, record["id"], record["project"], record["kind"], record["claim"], record["applies_when"],
                     record["identifiers"], record["fingerprint"], record["basis"], record["detector"], record["revision"],
                     record["worktree"], record["observed_at"], record["captured_at"], record["validity"], record["tags"],
                     record["state"], record["state_reason"], record["state_at"], now))
                for link in record["links"]:
                    db.execute("INSERT OR IGNORE INTO learning_import_link (origin_host, newer, older, relation) VALUES (?, ?, ?, ?)",
                               (origin_host, record["id"], link["older"], link["relation"]))
            else:
                db.execute(
                    "INSERT INTO learning_import_memory (origin_host, remote_id, project, action, applicability, revision, validity, "
                    "state, created_at, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (origin_host, record["id"], record["project"], record["action"], record["applicability"], record["revision"],
                     record["validity"], record["state"], record["created_at"], now))
            for source in record["sources"]:
                db.execute(
                    "INSERT OR IGNORE INTO learning_import_source (origin_host, record, remote_id, session, position, digest, role) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (origin_host, record["record"], record["id"], source["session"] + "@" + origin_host,
                     source["position"], source["digest"], source["role"]))
        db.execute("INSERT OR REPLACE INTO learning_import_state (origin_host, digest, records, received_at) VALUES (?, ?, ?, ?)",
                   (origin_host, digest, len(accepted), now))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return len(accepted)


def forget_session_imports(db, session):
    """Delete every imported record that cites a forgotten session."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'learning_import_source'").fetchone() is None:
        return
    rows = db.execute(
        "SELECT DISTINCT origin_host, record, remote_id FROM learning_import_source WHERE session = ?", (session,)).fetchall()
    for origin_host, record, remote_id in rows:
        table = "learning_import_experience" if record == "experience" else "learning_import_memory"
        db.execute("DELETE FROM %s WHERE origin_host = ? AND remote_id = ?" % table, (origin_host, remote_id))
        db.execute("DELETE FROM learning_import_source WHERE origin_host = ? AND record = ? AND remote_id = ?",
                   (origin_host, record, remote_id))
        if record == "experience":
            db.execute("DELETE FROM learning_import_link WHERE origin_host = ? AND (newer = ? OR older = ?)",
                       (origin_host, remote_id, remote_id))


def durable_digest(db, remote):
    row = db.execute("SELECT digest FROM transfer_learning WHERE remote = ?", (str(remote),)).fetchone()
    return row[0] if row is not None else None


def mark_durable(db, remote, digest):
    with db:
        db.execute("INSERT OR REPLACE INTO transfer_learning (remote, digest, durable_at) VALUES (?, ?, ?)",
                   (str(remote), digest, time.time()))


def imported_rows(db, limit):
    """Active imported records, newest observation first, each labeled advisory."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'learning_import_experience'").fetchone() is None:
        return []
    rows = []
    experiences = db.execute(
        "SELECT * FROM learning_import_experience WHERE state = 'retained' ORDER BY observed_at DESC LIMIT ?", (limit,)).fetchall()
    memories = db.execute(
        "SELECT * FROM learning_import_memory WHERE state = 'approved' ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    for row in experiences:
        rows.append(_imported(db, row, "experience", row["claim"], row["applies_when"], row["observed_at"]))
    for row in memories:
        rows.append(_imported(db, row, "memory", row["action"], row["applicability"], row["created_at"]))
    rows.sort(key=lambda item: item["observed_at"], reverse=True)
    return rows[:limit]


def _imported(db, row, record, claim, applies_when, observed_at):
    sources = [dict(item) for item in db.execute(
        "SELECT session, position, digest, role FROM learning_import_source "
        "WHERE origin_host = ? AND record = ? AND remote_id = ? ORDER BY session, position, role",
        (row["origin_host"], record, row["remote_id"]))]
    return {
        "record": "%s:%d@%s" % (record, row["remote_id"], row["origin_host"]), "origin_host": row["origin_host"],
        "project": row["project"], "kind": row["kind"] if record == "experience" else "memory", "claim": claim,
        "applies_when": applies_when, "observed_at": observed_at, "sources": sources, "authority": AUTHORITY,
    }
