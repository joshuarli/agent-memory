#!/usr/bin/env python3
"""Consistent snapshots and restore into a new destination.

A backup is a SQLite-API copy of the catalog plus a verified copy of every
immutable object the catalog references, pinned by digest in a manifest. Restore
verifies the manifest, restores the catalog, rebuilds derived indexes from
the captured bytes, and never activates a service or a review grant.
"""
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

from agent_memory import archive as archive_module
from agent_memory.learning import apply_remote_forget

MANIFEST = "manifest.json"
SNAPSHOT_CHUNK_BYTES = 1 << 20
SNAPSHOT_DERIVED_TABLES = ("events", "index_issues", "unindexed_sources")


class BackupError(RuntimeError):
    pass


def _fsync(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create(store, destination, protocol=1):
    """Write a complete backup to a staging directory, then publish it with one rename.

    The shared object lock is held from before the catalog snapshot until every
    referenced object is copied, so a concurrent forget can delete catalog rows
    but cannot unlink an object this backup still needs. A destination either
    does not exist or holds a complete backup whose manifest was written last.
    """
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise BackupError("backup destination must be new or empty: " + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".partial-" + secrets.token_hex(4))
    staging.mkdir(mode=0o700)
    try:
        manifest = _populate(store, staging, protocol)
        if destination.exists():
            destination.rmdir()
        os.rename(staging, destination)
        _fsync(destination.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _populate(store, staging, protocol):
    database = staging / "sessions.sqlite3"
    pinned = []
    with archive_module.object_lock(store.archive_dir):
        with contextlib.closing(sqlite3.connect(str(database))) as target:
            store.db.backup(target)
        os.chmod(database, 0o600)
        with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % database.as_posix(), uri=True)) as snapshot:
            counts = {name: snapshot.execute("SELECT count(*) FROM " + name).fetchone()[0]
                      for name in ("archive_generation", "tombstone")}
            schema_version = snapshot.execute("PRAGMA user_version").fetchone()[0]
            for digest, size in snapshot.execute("SELECT digest, size FROM archive_object ORDER BY digest"):
                try:
                    data = archive_module.read_object(store.archive_dir, digest)
                except (OSError, archive_module.ArchiveError) as exc:
                    raise BackupError("snapshot references an unreadable object: %s (%s)" % (digest, exc)) from exc
                if len(data) != int(size):
                    raise BackupError("snapshot object has the wrong size: " + digest)
                archive_module._write_object(staging, data)
                pinned.append({"digest": digest, "size": int(size)})
    manifest = {
        "protocol": protocol,
        "created_at": time.time(),
        "schema_version": schema_version,
        "objects": pinned,
        "generations": counts["archive_generation"],
        "tombstones": counts["tombstone"],
    }
    pending = staging / (MANIFEST + ".tmp")
    pending.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    _fsync(pending)
    os.replace(pending, staging / MANIFEST)
    _fsync(staging)
    return manifest


def snapshot(store, destination, publish=None):
    """Write a sanitized consistent database copy and optionally publish it under the object lock.

    The copy keeps the archive's shared object lock until ``publish`` returns, so a
    concurrent forget cannot unlink an object the snapshot pins. Archive objects are
    not copied; they are already content-addressed in the mirror.
    """
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise BackupError("snapshot destination must be new or empty: " + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".partial-" + secrets.token_hex(4))
    staging.mkdir(mode=0o700)
    try:
        database = staging / "sessions.sqlite3"
        with archive_module.object_lock(store.archive_dir):
            with contextlib.closing(sqlite3.connect(str(database))) as target:
                store.db.backup(target)
            os.chmod(database, 0o600)
            _fsync(database)
            metadata = _snapshot_metadata(database)
            result = metadata if publish is None else publish(staging, metadata)
        if destination.exists():
            destination.rmdir()
        os.rename(staging, destination)
        _fsync(destination.parent)
        return result
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _strip_derived_index(connection, tables):
    """Remove the transcript search projection; restore rebuilds it from captured bytes."""
    triggers = [row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'events' AND sql IS NOT NULL")]
    for name in ("events_ai", "events_ad", "events_au"):
        connection.execute("DROP TRIGGER IF EXISTS " + name)
    for name in SNAPSHOT_DERIVED_TABLES:
        if name in tables:
            connection.execute("DELETE FROM " + name)
    if "events_fts" in tables:
        connection.execute("INSERT INTO events_fts(events_fts) VALUES('delete-all')")
    for sql in triggers:
        connection.execute(sql)
    connection.commit()
    connection.execute("VACUUM")


def _snapshot_metadata(database):
    with contextlib.closing(sqlite3.connect(str(database))) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"archive_generation", "archive_generation_object", "archive_artifact"} <= tables:
            raise BackupError("snapshot database has no archive schema")
        with connection:
            if "learning_projects" in tables:
                connection.execute("UPDATE learning_projects SET enabled = 0, grant_source = 'project'")
            if "learning_global_grant" in tables:
                connection.execute("DELETE FROM learning_global_grant")
        _strip_derived_index(connection, tables)
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("snapshot database failed integrity_check")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        objects = {}
        for digest, size in connection.execute(
                "SELECT digest, size FROM archive_generation_object "
                "UNION SELECT digest, size FROM archive_artifact ORDER BY digest"):
            known = objects.setdefault(str(digest), int(size))
            if known != int(size):
                raise BackupError("snapshot object has conflicting sizes: " + str(digest))
    digest = hashlib.sha256()
    size = 0
    chunks = []
    with open(database, "rb") as stream:
        while True:
            data = stream.read(SNAPSHOT_CHUNK_BYTES)
            if not data:
                break
            chunks.append({"offset": size, "size": len(data), "digest": archive_module.sha256(data)})
            digest.update(data)
            size += len(data)
    if not chunks:
        raise BackupError("snapshot database is empty")
    return {
        "created_at": time.time(),
        "schema_version": schema_version,
        "db_digest": digest.hexdigest(),
        "db_size": size,
        "chunks": chunks,
        "objects": [{"digest": key, "size": value} for key, value in sorted(objects.items())],
        "objects_count": len(objects),
        "object_bytes": sum(objects.values()),
    }


def verify(destination):
    destination = Path(destination)
    manifest_path = destination / MANIFEST
    if not manifest_path.is_file():
        raise BackupError("backup manifest is missing")
    manifest = json.loads(manifest_path.read_text())
    for item in manifest.get("objects") or []:
        path = archive_module.object_path(destination, item["digest"])
        if not path.is_file():
            raise BackupError("backup object is missing: " + item["digest"])
        if path.stat().st_size != int(item["size"]):
            raise BackupError("backup object has the wrong size: " + item["digest"])
        if archive_module.sha256(path.read_bytes()) != item["digest"]:
            raise BackupError("backup object failed verification: " + item["digest"])
    database = destination / "sessions.sqlite3"
    if not database.is_file():
        raise BackupError("backup database is missing")
    with contextlib.closing(
            sqlite3.connect("file:%s?mode=ro" % database.as_posix(), uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("backup database failed integrity_check")
    return manifest


def restore(backup_dir, new_state_dir, tombstones_from=None):
    backup_dir = Path(backup_dir)
    manifest = verify(backup_dir)
    from agent_memory.store import SCHEMA_VERSION, Store, StoreError, _prepare_state_dir

    if int(manifest.get("schema_version") or 0) > SCHEMA_VERSION:
        raise BackupError("backup schema is newer than this tool")
    state = _prepare_state_dir(new_state_dir)
    if (state / "sessions.sqlite3").exists():
        raise BackupError("restore destination already holds a session store; choose a new directory")
    archive_dir = state / "archive"
    archive_dir.mkdir(mode=0o700, exist_ok=True)
    for item in manifest.get("objects") or []:
        target = archive_module.object_path(archive_dir, item["digest"])
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(archive_module.object_path(backup_dir, item["digest"]), target)
    database = state / "sessions.sqlite3"
    with contextlib.closing(
            sqlite3.connect("file:%s?mode=ro" % (backup_dir / "sessions.sqlite3").as_posix(), uri=True)) as source:
        with contextlib.closing(sqlite3.connect(str(database))) as target:
            source.backup(target)
    os.chmod(database, 0o600)
    try:
        store = Store(state)
    except StoreError as exc:
        raise BackupError(str(exc)) from exc
    with store:
        tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with store.db:
            if "learning_projects" in tables:
                store.db.execute("UPDATE learning_projects SET enabled = 0, grant_source = 'project'")
            if "learning_global_grant" in tables:
                store.db.execute("DELETE FROM learning_global_grant")
        store.reindex_generations()
        if tombstones_from is not None:
            with contextlib.closing(sqlite3.connect(
                    "file:%s?mode=ro" % (Path(tombstones_from) / "sessions.sqlite3").as_posix(), uri=True)) as current:
                current.row_factory = sqlite3.Row
                for row in current.execute("SELECT origin_host, harness, native_id, scope, forgotten_at FROM tombstone"):
                    apply_remote_forget(store, row["origin_host"], row["harness"], row["native_id"],
                                        row["scope"], row["forgotten_at"])
    return manifest
