#!/usr/bin/env python3
"""Cloudflare mirror protocol: validators, configuration, and a pooled HTTPS client."""
import base64
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

from agent_memory import archive as archive_module
from agent_memory import backup as backup_module
from agent_memory import learning as learning_module
from agent_memory import learning_sync
from agent_memory import mirror_crypto
from agent_memory.types import HARNESSES

PROTOCOL = 1
MAX_JSON_BYTES = 12 * 1024 * 1024
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_CIPHERTEXT_BYTES = MAX_OBJECT_BYTES + 64 * 1024
MAX_MISSING_OBJECTS = 20000
MAX_GENERATION_OBJECTS = 20000
MAX_GENERATION_ARTIFACTS = 1000
MAX_TOMBSTONES = 900
MAX_SNAPSHOT_BYTES = 1 << 40
MAX_SNAPSHOT_CHUNKS = 4096
MAX_SNAPSHOT_OBJECTS = 200000
MAX_SNAPSHOT_PAGE_OBJECTS = 10000
MAX_SNAPSHOT_PAGES = MAX_SNAPSHOT_OBJECTS // MAX_SNAPSHOT_PAGE_OBJECTS
GC_GRACE_SECONDS = 3600
MAX_GC_SWEEP = 200
SNAPSHOT_MIN_INTERVAL_SECONDS = 24 * 60 * 60
SNAPSHOT_RETRY_BASE_SECONDS = 15 * 60
MAX_SOURCE_BYTES = 1 << 50
MAX_PAGE = 200
MAX_CURSOR_CHARS = 512
MAX_CURSOR_PARTS = 4
MAX_CURSOR_PART_CHARS = 512
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT = 30
ATTEMPTS = 5
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ERROR_CODES = frozenset({
    "forbidden",
    "not_found",
    "method_not_allowed",
    "unavailable",
    "invalid_request",
    "unsupported_version",
    "payload_too_large",
    "conflict",
    "stale_revision",
    "missing_object",
    "tombstoned",
})
CONFIG_KEYS = frozenset({
    "enabled",
    "transport",
    "url",
    "service_token_file",
    "age_recipients_file",
    "age_identity_file",
    "learning_projection",
})
ENCODINGS = frozenset({"identity", "zlib"})
OBJECT_CLASSES = frozenset({"chunk", "artifact", "manifest", "snapshot"})
SCOPES = frozenset({"local", "global"})
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ORIGIN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
TOKEN_PART = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
ARTIFACT = re.compile(r"^[A-Za-z0-9._-]{1,256}$")


class MirrorError(RuntimeError):
    def __init__(self, code, message, status=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.state = None


@dataclass(frozen=True)
class MirrorConfig:
    url: str
    service_token_file: Path
    age_recipients_file: Path
    age_identity_file: Path
    learning_projection: bool
    enabled: bool = True


def _record(value):
    return isinstance(value, dict)


def _exact_keys(value, keys):
    return _record(value) and set(value) == set(keys)


def decode_digest(value):
    return value if isinstance(value, str) and DIGEST.match(value) else None


def decode_origin(value):
    return value if isinstance(value, str) and ORIGIN.match(value) else None


def decode_protocol(value):
    return PROTOCOL if value == PROTOCOL and not isinstance(value, bool) else None


def _decode_size(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_OBJECT_BYTES else None


def decode_hello(value):
    if not _exact_keys(value, ("protocol", "origin")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    return None if origin is None else {"protocol": PROTOCOL, "origin": origin}


def decode_missing_request(value):
    if not _exact_keys(value, ("protocol", "origin", "objects")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    objects = value.get("objects")
    if origin is None or not isinstance(objects, list) or len(objects) > MAX_MISSING_OBJECTS:
        return None
    decoded = []
    digests = set()
    for item in objects:
        if not _exact_keys(item, ("plaintext_digest", "size")):
            return None
        digest = decode_digest(item.get("plaintext_digest"))
        size = _decode_size(item.get("size"))
        if digest is None or size is None or digest in digests:
            return None
        digests.add(digest)
        decoded.append({"plaintext_digest": digest, "size": size})
    return {"protocol": PROTOCOL, "origin": origin, "objects": decoded}


def _valid_cursor_part(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_CURSOR_PART_CHARS


def encode_cursor(parts):
    parts = list(parts)
    if not parts or len(parts) > MAX_CURSOR_PARTS or not all(_valid_cursor_part(part) for part in parts):
        raise ValueError("cursor parts are invalid")
    encoded = base64.urlsafe_b64encode(
        json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")
    if len(encoded) > MAX_CURSOR_CHARS:
        raise ValueError("cursor is too long")
    return encoded


def decode_cursor(value):
    if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_CHARS or not BASE64URL.match(value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parts = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parts, list) or not parts or len(parts) > MAX_CURSOR_PARTS:
        return None
    if not all(_valid_cursor_part(part) for part in parts):
        return None
    return parts


def _decode_path(value, key):
    if not isinstance(value, str) or not value:
        raise MirrorError("invalid_config", "cloudflare config needs a " + key + " path")
    return Path(value).expanduser()


def decode_config(value):
    if not _record(value):
        raise MirrorError("invalid_config", "cloudflare config must be an object")
    unknown = set(value) - CONFIG_KEYS
    if unknown:
        raise MirrorError("invalid_config", "unknown cloudflare config key: " + sorted(unknown)[0])
    if value.get("transport") != "cloudflare":
        raise MirrorError("invalid_config", "cloudflare config needs transport=cloudflare")
    url = value.get("url")
    if not isinstance(url, str):
        raise MirrorError("invalid_config", "cloudflare config needs a url")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise MirrorError("invalid_config", "cloudflare url has an invalid port") from exc
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise MirrorError("invalid_config", "cloudflare url must be an https origin")
    normalized = "https://" + parts.hostname + ((":" + str(port)) if port else "")
    projection = value.get("learning_projection", False)
    if not isinstance(projection, bool):
        raise MirrorError("invalid_config", "learning_projection must be a boolean")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MirrorError("invalid_config", "enabled must be a boolean")
    return MirrorConfig(
        url=normalized,
        service_token_file=_decode_path(value.get("service_token_file"), "service_token_file"),
        age_recipients_file=_decode_path(value.get("age_recipients_file"), "age_recipients_file"),
        age_identity_file=_decode_path(value.get("age_identity_file"), "age_identity_file"),
        learning_projection=projection,
        enabled=enabled,
    )


def load_config(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text())
    except OSError as exc:
        raise MirrorError("invalid_config", "agent-sessions config is not readable") from exc
    except ValueError as exc:
        raise MirrorError("invalid_config", "agent-sessions config is not valid JSON") from exc
    if not _record(value) or not _record(value.get("central")):
        raise MirrorError("invalid_config", "agent-sessions config has no central object")
    return decode_config(value["central"])


def load_service_token(path):
    path = Path(path)
    try:
        info = path.stat()
    except OSError as exc:
        raise MirrorError("invalid_config", "service token file is missing") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise MirrorError("invalid_config", "service token file must be regular and mode 0600")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MirrorError("invalid_config", "service token file is not valid JSON") from exc
    if not _exact_keys(value, ("client_id", "client_secret")):
        raise MirrorError("invalid_config", "service token file must hold client_id and client_secret")
    for key in ("client_id", "client_secret"):
        if not isinstance(value[key], str) or not TOKEN_PART.match(value[key]):
            raise MirrorError("invalid_config", "service token " + key + " is invalid")
    return value["client_id"], value["client_secret"]


def _decode_learning(value):
    if not _exact_keys(value, ("revision", "digest", "records")):
        return None
    revision, records = value.get("revision"), value.get("records")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return None
    if isinstance(records, bool) or not isinstance(records, int) or records < 0:
        return None
    digest = value.get("digest")
    if digest is not None and decode_digest(digest) is None:
        return None
    return {"revision": revision, "digest": digest, "records": records}


def decode_hello_response(value):
    if not _record(value):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    store = value.get("store_id")
    origin = decode_origin(value.get("origin"))
    if not isinstance(store, str) or not ORIGIN.match(store) or origin is None:
        return None
    revision = value.get("revision")
    watermark = value.get("tombstone_watermark")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return None
    if isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
        return None
    learning = _decode_learning(value.get("learning"))
    if learning is None:
        return None
    return {
        "protocol": PROTOCOL, "store_id": store, "origin": origin,
        "revision": revision, "tombstone_watermark": watermark, "learning": learning,
    }


def _decode_learning_revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def decode_learning_state_response(value):
    if not _exact_keys(value, ("protocol", "origin", "revision", "epoch", "digest", "records")):
        return None
    origin = decode_origin(value.get("origin"))
    revision = _decode_learning_revision(value.get("revision"))
    epoch = _decode_learning_revision(value.get("epoch"))
    records = _decode_learning_revision(value.get("records"))
    digest = value.get("digest")
    if decode_protocol(value.get("protocol")) is None or origin is None:
        return None
    if revision is None or epoch is None or records is None or epoch < 1:
        return None
    if digest is not None and decode_digest(digest) is None:
        return None
    return {"protocol": PROTOCOL, "origin": origin, "revision": revision, "epoch": epoch,
            "digest": digest, "records": records}


def decode_learning_key_page(value):
    if not _exact_keys(value, ("protocol", "origin", "revision", "epoch", "keys", "next_cursor")):
        return None
    state = decode_learning_state_response({
        "protocol": value.get("protocol"), "origin": value.get("origin"),
        "revision": value.get("revision"), "epoch": value.get("epoch"), "digest": None, "records": 0,
    })
    if state is None:
        return None
    keys = value.get("keys")
    if not isinstance(keys, list) or len(keys) > MAX_PAGE:
        return None
    decoded = []
    for item in keys:
        if not _exact_keys(item, ("record", "id", "digest")):
            return None
        record, identifier, digest = item.get("record"), item.get("id"), item.get("digest")
        if record not in ("experience", "memory"):
            return None
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 0:
            return None
        if decode_digest(digest) is None:
            return None
        decoded.append({"record": record, "id": identifier, "digest": digest})
    cursor = value.get("next_cursor")
    if cursor is not None and decode_cursor(cursor) is None:
        return None
    return {"protocol": PROTOCOL, "origin": state["origin"], "revision": state["revision"],
            "epoch": state["epoch"], "keys": decoded, "next_cursor": cursor}


def decode_reconcile_response(value):
    if not _record(value) or decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    action = value.get("action")
    if origin is None or action not in ("begin", "apply", "commit", "abort"):
        return None
    if action == "begin":
        if not _exact_keys(value, ("protocol", "origin", "action", "revision", "epoch", "digest", "records",
                                   "current", "resumed")):
            return None
        revision = _decode_learning_revision(value.get("revision"))
        epoch = _decode_learning_revision(value.get("epoch"))
        records = _decode_learning_revision(value.get("records"))
        digest = value.get("digest")
        if revision is None or epoch is None or epoch < 1 or records is None:
            return None
        if digest is not None and decode_digest(digest) is None:
            return None
        if not isinstance(value.get("current"), bool) or not isinstance(value.get("resumed"), bool):
            return None
        return {"protocol": PROTOCOL, "origin": origin, "action": action, "revision": revision,
                "epoch": epoch, "digest": digest, "records": records,
                "current": value["current"], "resumed": value["resumed"]}
    if action == "apply":
        if not _exact_keys(value, ("protocol", "origin", "action", "epoch", "staged")):
            return None
        epoch = _decode_learning_revision(value.get("epoch"))
        staged = _decode_learning_revision(value.get("staged"))
        if epoch is None or epoch < 1 or staged is None:
            return None
        return {"protocol": PROTOCOL, "origin": origin, "action": action, "epoch": epoch, "staged": staged}
    if action == "commit":
        if not _exact_keys(value, ("protocol", "origin", "action", "revision", "epoch", "digest", "records",
                                   "applied", "replayed")):
            return None
        revision = _decode_learning_revision(value.get("revision"))
        epoch = _decode_learning_revision(value.get("epoch"))
        records = _decode_learning_revision(value.get("records"))
        applied = _decode_learning_revision(value.get("applied"))
        digest = value.get("digest")
        if revision is None or epoch is None or epoch < 1 or records is None or applied is None:
            return None
        if decode_digest(digest) is None or not isinstance(value.get("replayed"), bool):
            return None
        return {"protocol": PROTOCOL, "origin": origin, "action": action, "revision": revision,
                "epoch": epoch, "digest": digest, "records": records, "applied": applied,
                "replayed": value["replayed"]}
    if not _exact_keys(value, ("protocol", "origin", "action", "epoch", "aborted")):
        return None
    epoch = _decode_learning_revision(value.get("epoch"))
    if epoch is None or epoch < 1 or not isinstance(value.get("aborted"), bool):
        return None
    return {"protocol": PROTOCOL, "origin": origin, "action": action, "epoch": epoch,
            "aborted": value["aborted"]}


def decode_learning_delta_response(value):
    if not _record(value):
        return None
    decoded = decode_learning_state_response(
        {key: value.get(key) for key in ("protocol", "origin", "revision", "epoch", "digest", "records")})
    if decoded is None or not _exact_keys(
            value, ("protocol", "origin", "revision", "epoch", "digest", "records", "applied", "deleted", "replayed")):
        return None
    applied = _decode_learning_revision(value.get("applied"))
    deleted = _decode_learning_revision(value.get("deleted"))
    replayed = value.get("replayed")
    if applied is None or deleted is None or not isinstance(replayed, bool):
        return None
    return dict(decoded, applied=applied, deleted=deleted, replayed=replayed)


def _decode_remote_object(value):
    if not _exact_keys(value, ("plaintext_digest", "plaintext_size", "encoding", "ciphertext_digest", "ciphertext_size")):
        return None
    plaintext = decode_digest(value.get("plaintext_digest"))
    ciphertext = decode_digest(value.get("ciphertext_digest"))
    encoding = value.get("encoding")
    size = value.get("plaintext_size")
    ciphertext_size = value.get("ciphertext_size")
    if plaintext is None or ciphertext is None or encoding not in ENCODINGS:
        return None
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_OBJECT_BYTES:
        return None
    if isinstance(ciphertext_size, bool) or not isinstance(ciphertext_size, int) or not 0 <= ciphertext_size <= MAX_CIPHERTEXT_BYTES:
        return None
    return {
        "plaintext_digest": plaintext, "plaintext_size": size, "encoding": encoding,
        "ciphertext_digest": ciphertext, "ciphertext_size": ciphertext_size,
    }


def decode_missing_response(value):
    if not _record(value):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    missing = value.get("missing")
    known = value.get("known")
    if origin is None or not isinstance(missing, list) or not isinstance(known, list):
        return None
    decoded_missing = []
    for item in missing:
        if not _exact_keys(item, ("plaintext_digest", "size")):
            return None
        digest = decode_digest(item.get("plaintext_digest"))
        size = _decode_size(item.get("size"))
        if digest is None or size is None:
            return None
        decoded_missing.append({"plaintext_digest": digest, "size": size})
    decoded_known = []
    for item in known:
        object_row = _decode_remote_object(item)
        if object_row is None:
            return None
        decoded_known.append(object_row)
    return {"protocol": PROTOCOL, "origin": origin, "missing": decoded_missing, "known": decoded_known}


def decode_put_response(value):
    if not _exact_keys(value, ("stored", "object")):
        return None
    stored = value.get("stored")
    object_row = _decode_remote_object(value.get("object"))
    if not isinstance(stored, bool) or object_row is None:
        return None
    return {"stored": stored, "object": object_row}


def _decode_revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2 ** 53 - 1:
        return None
    return value


def _decode_timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 <= value <= 2 ** 53 - 1:
        return None
    return float(value)


def _decode_text(value, maximum):
    return value if isinstance(value, str) and 0 < len(value) <= maximum and "\u0000" not in value else None


def _decode_manifest_chunk(value):
    if not _exact_keys(value, ("offset", "size", "digest")):
        return None
    offset = _decode_revision(value.get("offset"))
    size = _decode_size(value.get("size"))
    digest = decode_digest(value.get("digest"))
    if offset is None or size is None or digest is None or size == 0:
        return None
    return {"offset": offset, "size": size, "digest": digest}


def _decode_manifest_artifact(value):
    if not _exact_keys(value, ("name", "digest", "size")):
        return None
    name = value.get("name")
    if not isinstance(name, str) or not ARTIFACT.match(name):
        return None
    digest = decode_digest(value.get("digest"))
    size = _decode_size(value.get("size"))
    return None if digest is None or size is None else {"name": name, "digest": digest, "size": size}


def decode_generation_manifest(value):
    keys = ("origin_host", "harness", "native_id", "source_path", "source_digest", "source_size",
            "captured_at", "chunks", "artifacts", "session_metadata", "manifest_digest", "manifest_object")
    if not _exact_keys(value, keys):
        return None
    origin = decode_origin(value.get("origin_host"))
    harness = value.get("harness") if value.get("harness") in HARNESSES else None
    native = _decode_text(value.get("native_id"), 512)
    source_path = _decode_text(value.get("source_path"), 4096)
    source_digest = decode_digest(value.get("source_digest"))
    source_size = value.get("source_size")
    if isinstance(source_size, bool) or not isinstance(source_size, int) or not 0 <= source_size <= MAX_SOURCE_BYTES:
        return None
    captured_at = _decode_timestamp(value.get("captured_at"))
    manifest_digest = decode_digest(value.get("manifest_digest"))
    manifest_object = decode_digest(value.get("manifest_object"))
    if None in (origin, harness, native, source_path, source_digest, captured_at, manifest_digest, manifest_object):
        return None
    metadata = value.get("session_metadata")
    if metadata is not None and not isinstance(metadata, (dict, list)):
        return None
    chunks = value.get("chunks")
    artifacts = value.get("artifacts")
    if not isinstance(chunks, list) or len(chunks) > MAX_GENERATION_OBJECTS:
        return None
    if not isinstance(artifacts, list) or len(artifacts) > MAX_GENERATION_ARTIFACTS:
        return None
    clean_chunks = []
    cursor = 0
    for item in chunks:
        chunk = _decode_manifest_chunk(item)
        if chunk is None or chunk["offset"] != cursor:
            return None
        cursor += chunk["size"]
        if cursor > MAX_SOURCE_BYTES:
            return None
        clean_chunks.append(chunk)
    if cursor != source_size:
        return None
    clean_artifacts = []
    names = set()
    for item in artifacts:
        artifact = _decode_manifest_artifact(item)
        if artifact is None or artifact["name"] in names:
            return None
        names.add(artifact["name"])
        clean_artifacts.append(artifact)
    sizes = {}
    for obj in [*clean_chunks, *clean_artifacts]:
        if sizes.setdefault(obj["digest"], obj["size"]) != obj["size"]:
            return None
    if hashlib.sha256("".join(chunk["digest"] for chunk in clean_chunks).encode("ascii")).hexdigest() != source_digest:
        return None
    return {
        "origin_host": origin, "harness": harness, "native_id": native, "source_path": source_path,
        "source_digest": source_digest, "source_size": source_size, "captured_at": captured_at,
        "chunks": clean_chunks, "artifacts": clean_artifacts, "session_metadata": metadata,
        "manifest_digest": manifest_digest, "manifest_object": manifest_object,
    }


def decode_generation_commit(value):
    if not _exact_keys(value, ("protocol", "origin", "base_revision", "manifest")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    base_revision = _decode_revision(value.get("base_revision"))
    manifest = decode_generation_manifest(value.get("manifest"))
    if origin is None or base_revision is None or manifest is None or manifest["origin_host"] != origin:
        return None
    return {"protocol": PROTOCOL, "origin": origin, "base_revision": base_revision, "manifest": manifest}


def decode_tombstone_request(value):
    if not _exact_keys(value, ("protocol", "origin", "base_revision", "tombstones")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    base_revision = _decode_revision(value.get("base_revision"))
    tombstones = value.get("tombstones")
    if origin is None or base_revision is None or not isinstance(tombstones, list):
        return None
    if len(tombstones) > MAX_TOMBSTONES:
        return None
    clean = []
    keys = set()
    for item in tombstones:
        if not _exact_keys(item, ("harness", "native_id", "scope", "forgotten_at")):
            return None
        harness = item.get("harness") if item.get("harness") in HARNESSES else None
        native = _decode_text(item.get("native_id"), 512)
        scope = item.get("scope") if item.get("scope") in SCOPES else None
        forgotten_at = _decode_timestamp(item.get("forgotten_at"))
        if harness is None or native is None or scope is None or forgotten_at is None:
            return None
        key = (harness, native)
        if key in keys:
            return None
        keys.add(key)
        clean.append({"harness": harness, "native_id": native, "scope": scope, "forgotten_at": forgotten_at})
    return {"protocol": PROTOCOL, "origin": origin, "base_revision": base_revision, "tombstones": clean}


def _decode_snapshot_chunk(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("plaintext_digest", "size", "ciphertext_digest")):
        return None
    plaintext = decode_digest(value.get("plaintext_digest"))
    ciphertext = decode_digest(value.get("ciphertext_digest"))
    size = _decode_size(value.get("size"))
    if plaintext is None or ciphertext is None or size is None or size == 0:
        return None
    return {"plaintext_digest": plaintext, "size": size, "ciphertext_digest": ciphertext}


def _decode_snapshot_object(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("plaintext_digest", "size")):
        return None
    digest = decode_digest(value.get("plaintext_digest"))
    size = _decode_size(value.get("size"))
    return None if digest is None or size is None else {"plaintext_digest": digest, "size": size}


def encode_snapshot_page(objects: list[dict[str, Any]]) -> str:
    return "".join(f"{item['plaintext_digest']} {int(item['size'])}\n" for item in objects)


def snapshot_page_count(objects) -> int:
    return -(-int(objects) // MAX_SNAPSHOT_PAGE_OBJECTS)


def decode_snapshot_page(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin", "page", "objects")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    page = value.get("page")
    objects = value.get("objects")
    if origin is None or isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < MAX_SNAPSHOT_PAGES:
        return None
    if not isinstance(objects, list) or not 0 < len(objects) <= MAX_SNAPSHOT_PAGE_OBJECTS:
        return None
    clean = []
    for item in objects:
        obj = _decode_snapshot_object(item)
        if obj is None or (clean and clean[-1]["plaintext_digest"] >= obj["plaintext_digest"]):
            return None
        clean.append(obj)
    return {"protocol": PROTOCOL, "origin": origin, "page": page, "objects": clean}


def decode_snapshot_commit(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin", "base_revision", "snapshot")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    base_revision = _decode_revision(value.get("base_revision"))
    snapshot = value.get("snapshot")
    if origin is None or base_revision is None or not _record(snapshot):
        return None
    if not _exact_keys(snapshot, ("id", "created_at", "schema_version", "db_digest", "db_size", "chunks",
                                  "objects", "object_bytes", "objects_digest")):
        return None
    identifier = decode_digest(snapshot.get("id"))
    db_digest = decode_digest(snapshot.get("db_digest"))
    created_at = _decode_timestamp(snapshot.get("created_at"))
    schema_version = _decode_revision(snapshot.get("schema_version"))
    db_size = snapshot.get("db_size")
    objects = snapshot.get("objects")
    object_bytes = snapshot.get("object_bytes")
    objects_digest = decode_digest(snapshot.get("objects_digest"))
    if identifier is None or db_digest is None or identifier != db_digest:
        return None
    if created_at is None or schema_version is None or schema_version > 1000:
        return None
    if isinstance(db_size, bool) or not isinstance(db_size, int) or not 0 <= db_size <= MAX_SNAPSHOT_BYTES:
        return None
    if isinstance(objects, bool) or not isinstance(objects, int) or not 0 <= objects <= MAX_SNAPSHOT_OBJECTS:
        return None
    if isinstance(object_bytes, bool) or not isinstance(object_bytes, int) or object_bytes < 0 or objects_digest is None:
        return None
    chunks = snapshot.get("chunks")
    if not isinstance(chunks, list) or not 0 < len(chunks) <= MAX_SNAPSHOT_CHUNKS:
        return None
    clean_chunks = []
    cursor = 0
    for item in chunks:
        chunk = _decode_snapshot_chunk(item)
        if chunk is None:
            return None
        cursor += chunk["size"]
        if cursor > MAX_SNAPSHOT_BYTES:
            return None
        clean_chunks.append(chunk)
    if cursor != db_size:
        return None
    return {
        "protocol": PROTOCOL, "origin": origin, "base_revision": base_revision,
        "snapshot": {
            "id": identifier, "created_at": created_at, "schema_version": schema_version,
            "db_digest": db_digest, "db_size": db_size, "chunks": clean_chunks,
            "objects": objects, "object_bytes": object_bytes, "objects_digest": objects_digest,
        },
    }


def decode_snapshot_verify_request(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    return None if origin is None else {"protocol": PROTOCOL, "origin": origin}


def decode_gc_request(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin", "apply")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    if origin is None or not isinstance(value.get("apply"), bool):
        return None
    return {"protocol": PROTOCOL, "origin": origin, "apply": value["apply"]}


def decode_object_replace_request(value) -> dict[str, Any] | None:
    keys = ("protocol", "origin", "plaintext_digest", "plaintext_size", "encoding",
            "ciphertext_digest", "ciphertext_size")
    if not _exact_keys(value, keys):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    plaintext = decode_digest(value.get("plaintext_digest"))
    ciphertext = decode_digest(value.get("ciphertext_digest"))
    plaintext_size = _decode_size(value.get("plaintext_size"))
    ciphertext_size = _decode_size(value.get("ciphertext_size"))
    encoding = value.get("encoding") if value.get("encoding") in ENCODINGS else None
    if None in (origin, plaintext, ciphertext, plaintext_size, ciphertext_size, encoding):
        return None
    return {
        "protocol": PROTOCOL, "origin": origin, "plaintext_digest": plaintext,
        "plaintext_size": plaintext_size, "encoding": encoding,
        "ciphertext_digest": ciphertext, "ciphertext_size": ciphertext_size,
    }


def _decode_snapshot_row(value) -> dict[str, Any] | None:
    keys = ("id", "origin_host", "created_at", "schema_version", "db_digest", "db_size",
            "objects", "object_bytes", "objects_digest", "verified_at", "revision")
    if not _exact_keys(value, keys):
        return None
    identifier = decode_digest(value.get("id"))
    origin = decode_origin(value.get("origin_host"))
    created_at = _decode_timestamp(value.get("created_at"))
    schema_version = _decode_revision(value.get("schema_version"))
    db_digest = decode_digest(value.get("db_digest"))
    db_size = value.get("db_size")
    objects = value.get("objects")
    object_bytes = value.get("object_bytes")
    objects_digest = decode_digest(value.get("objects_digest"))
    verified_at = value.get("verified_at")
    revision = _decode_revision(value.get("revision"))
    if None in (identifier, origin, created_at, schema_version, db_digest, objects_digest, revision):
        return None
    if identifier != db_digest:
        return None
    for number in (db_size, objects, object_bytes):
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
    if verified_at is not None:
        verified_at = _decode_timestamp(verified_at)
        if verified_at is None:
            return None
    return {
        "id": identifier, "origin_host": origin, "created_at": created_at,
        "schema_version": schema_version, "db_digest": db_digest, "db_size": db_size,
        "objects": objects, "object_bytes": object_bytes, "objects_digest": objects_digest,
        "verified_at": verified_at, "revision": revision,
    }


def _decode_snapshot_chunk_row(value) -> dict[str, Any] | None:
    keys = ("ordinal", "plaintext_digest", "size", "ciphertext_digest", "encoding", "ciphertext_size")
    if not _exact_keys(value, keys):
        return None
    ordinal = _decode_revision(value.get("ordinal"))
    plaintext = decode_digest(value.get("plaintext_digest"))
    ciphertext = decode_digest(value.get("ciphertext_digest"))
    size = _decode_size(value.get("size"))
    ciphertext_size = _decode_size(value.get("ciphertext_size"))
    encoding = value.get("encoding") if value.get("encoding") in ENCODINGS else None
    if None in (ordinal, plaintext, ciphertext, size, ciphertext_size, encoding):
        return None
    return {
        "ordinal": ordinal, "plaintext_digest": plaintext, "size": size,
        "ciphertext_digest": ciphertext, "encoding": encoding, "ciphertext_size": ciphertext_size,
    }


def _decode_snapshot_object_row(value) -> dict[str, Any] | None:
    keys = ("plaintext_digest", "size", "ciphertext_digest", "encoding", "ciphertext_size")
    if not _exact_keys(value, keys):
        return None
    plaintext = decode_digest(value.get("plaintext_digest"))
    ciphertext = decode_digest(value.get("ciphertext_digest"))
    size = _decode_size(value.get("size"))
    ciphertext_size = _decode_size(value.get("ciphertext_size"))
    encoding = value.get("encoding") if value.get("encoding") in ENCODINGS else None
    if None in (plaintext, ciphertext, size, ciphertext_size, encoding):
        return None
    return {
        "plaintext_digest": plaintext, "size": size,
        "ciphertext_digest": ciphertext, "encoding": encoding, "ciphertext_size": ciphertext_size,
    }


def decode_snapshot_list_response(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin", "snapshots", "next_cursor")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    rows = value.get("snapshots")
    if origin is None or not isinstance(rows, list):
        return None
    clean = []
    for item in rows:
        row = _decode_snapshot_row(item)
        if row is None or row["origin_host"] != origin:
            return None
        clean.append(row)
    cursor = value.get("next_cursor")
    if cursor is not None:
        parts = decode_cursor(cursor)
        if parts is None or len(parts) != 2 or decode_digest(parts[1]) is None:
            return None
        try:
            created_at = float(parts[0])
        except ValueError:
            return None
        if _decode_timestamp(created_at) is None:
            return None
    return {"protocol": PROTOCOL, "origin": origin, "snapshots": clean, "next_cursor": cursor}


def decode_snapshot_detail_response(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin", "snapshot", "chunks")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    row = _decode_snapshot_row(value.get("snapshot"))
    chunks = value.get("chunks")
    if origin is None or row is None or row["origin_host"] != origin or not isinstance(chunks, list):
        return None
    clean_chunks: list[dict[str, Any]] = []
    cursor: int = 0
    for item in chunks:
        chunk = _decode_snapshot_chunk_row(item)
        if chunk is None or chunk["ordinal"] != len(clean_chunks):
            return None
        cursor += int(chunk["size"])
        clean_chunks.append(chunk)
    if cursor != row["db_size"]:
        return None
    return {"protocol": PROTOCOL, "origin": origin, "snapshot": row, "chunks": clean_chunks}


def decode_snapshot_objects_response(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin", "snapshot", "page", "objects")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    identifier = decode_digest(value.get("snapshot"))
    page = value.get("page")
    objects = value.get("objects")
    if origin is None or identifier is None or isinstance(page, bool) or not isinstance(page, int) or page < 0:
        return None
    if not isinstance(objects, list) or not 0 < len(objects) <= MAX_SNAPSHOT_PAGE_OBJECTS:
        return None
    clean: list[dict[str, Any]] = []
    for item in objects:
        obj = _decode_snapshot_object_row(item)
        if obj is None or (clean and clean[-1]["plaintext_digest"] >= obj["plaintext_digest"]):
            return None
        clean.append(obj)
    return {"protocol": PROTOCOL, "origin": origin, "snapshot": identifier, "page": page, "objects": clean}


def decode_snapshot_page_response(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin", "snapshot", "page", "objects", "duplicate")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    identifier = decode_digest(value.get("snapshot"))
    page = value.get("page")
    objects = value.get("objects")
    duplicate = value.get("duplicate")
    if origin is None or identifier is None or not isinstance(duplicate, bool):
        return None
    for number in (page, objects):
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
    return {"protocol": PROTOCOL, "origin": origin, "snapshot": identifier, "page": page,
            "objects": objects, "duplicate": duplicate}


def decode_snapshot_commit_response(value) -> dict[str, Any] | None:
    keys = ("protocol", "origin", "snapshot", "revision", "duplicate", "chunks", "objects", "object_bytes")
    if not _exact_keys(value, keys):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    identifier = decode_digest(value.get("snapshot"))
    revision = _decode_revision(value.get("revision"))
    duplicate = value.get("duplicate")
    chunks = value.get("chunks")
    objects = value.get("objects")
    object_bytes = value.get("object_bytes")
    if origin is None or identifier is None or revision is None or not isinstance(duplicate, bool):
        return None
    if chunks is not None and (isinstance(chunks, bool) or not isinstance(chunks, int) or chunks < 0):
        return None
    if isinstance(objects, bool) or not isinstance(objects, int) or objects < 0:
        return None
    if isinstance(object_bytes, bool) or not isinstance(object_bytes, int) or object_bytes < 0:
        return None
    return {
        "protocol": PROTOCOL, "origin": origin, "snapshot": identifier, "revision": revision,
        "duplicate": duplicate, "chunks": chunks, "objects": objects, "object_bytes": object_bytes,
    }


def decode_snapshot_verify_response(value) -> dict[str, Any] | None:
    keys = ("protocol", "origin", "snapshot", "verified_at", "duplicate")
    if not _exact_keys(value, keys):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    identifier = decode_digest(value.get("snapshot"))
    verified_at = _decode_timestamp(value.get("verified_at"))
    duplicate = value.get("duplicate")
    if origin is None or identifier is None or verified_at is None or not isinstance(duplicate, bool):
        return None
    return {
        "protocol": PROTOCOL, "origin": origin, "snapshot": identifier,
        "verified_at": verified_at, "duplicate": duplicate,
    }


def decode_gc_response(value) -> dict[str, Any] | None:
    keys = ("protocol", "origin", "apply", "snapshots_retained", "snapshots_pruned",
            "objects_marked", "bytes_marked", "objects_swept", "bytes_swept")
    if not _exact_keys(value, keys):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    apply_value = value.get("apply")
    if origin is None or not isinstance(apply_value, bool):
        return None
    clean = {"protocol": PROTOCOL, "origin": origin, "apply": apply_value}
    for key in ("snapshots_retained", "snapshots_pruned", "objects_marked",
                "bytes_marked", "objects_swept", "bytes_swept"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
        clean[key] = number
    return clean


def decode_object_replace_response(value) -> dict[str, Any] | None:
    if not _exact_keys(value, ("protocol", "origin", "object", "replaced")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    row = _decode_remote_object(value.get("object"))
    replaced = value.get("replaced")
    if origin is None or row is None or not isinstance(replaced, bool):
        return None
    return {"protocol": PROTOCOL, "origin": origin, "object": row, "replaced": replaced}


def _decode_generation_row(value):
    keys = ("id", "origin_host", "harness", "native_id", "manifest_digest", "source_digest",
            "source_size", "captured_at", "committed_at", "revision", "manifest_object")
    if not _exact_keys(value, keys):
        return None
    identifier = _decode_revision(value.get("id"))
    origin = decode_origin(value.get("origin_host"))
    harness = value.get("harness") if value.get("harness") in HARNESSES else None
    native = _decode_text(value.get("native_id"), 512)
    manifest_digest = decode_digest(value.get("manifest_digest"))
    source_digest = decode_digest(value.get("source_digest"))
    source_size = value.get("source_size")
    if identifier is None or origin is None or harness is None or native is None:
        return None
    if manifest_digest is None or source_digest is None:
        return None
    if isinstance(source_size, bool) or not isinstance(source_size, int) or not 0 <= source_size <= MAX_SOURCE_BYTES:
        return None
    captured_at = _decode_timestamp(value.get("captured_at"))
    committed_at = _decode_timestamp(value.get("committed_at"))
    revision = _decode_revision(value.get("revision"))
    manifest_object = decode_digest(value.get("manifest_object"))
    if None in (captured_at, committed_at, revision, manifest_object):
        return None
    return {
        "id": identifier, "origin_host": origin, "harness": harness, "native_id": native,
        "manifest_digest": manifest_digest, "source_digest": source_digest, "source_size": source_size,
        "captured_at": captured_at, "committed_at": committed_at, "revision": revision,
        "manifest_object": manifest_object,
    }


def decode_generation_commit_response(value):
    if not _exact_keys(value, ("protocol", "origin", "generation", "manifest_digest", "revision", "duplicate")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    generation = _decode_revision(value.get("generation"))
    manifest_digest = decode_digest(value.get("manifest_digest"))
    revision = _decode_revision(value.get("revision"))
    duplicate = value.get("duplicate")
    if origin is None or generation is None or manifest_digest is None or revision is None:
        return None
    if not isinstance(duplicate, bool):
        return None
    return {"protocol": PROTOCOL, "origin": origin, "generation": generation,
            "manifest_digest": manifest_digest, "revision": revision, "duplicate": duplicate}


def decode_generation_page(value):
    if not _exact_keys(value, ("protocol", "origin", "generations", "next_cursor")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    rows = value.get("generations")
    next_cursor = value.get("next_cursor")
    if origin is None or not isinstance(rows, list) or len(rows) > MAX_PAGE:
        return None
    if next_cursor is not None and not isinstance(next_cursor, str):
        return None
    clean = []
    for row in rows:
        item = _decode_generation_row(row)
        if item is None or item["origin_host"] != origin:
            return None
        clean.append(item)
    return {"protocol": PROTOCOL, "origin": origin, "generations": clean, "next_cursor": next_cursor}


def _decode_tombstone_row(value):
    if not _exact_keys(value, ("harness", "native_id", "scope", "forgotten_at", "revision")):
        return None
    harness = value.get("harness") if value.get("harness") in HARNESSES else None
    native = _decode_text(value.get("native_id"), 512)
    scope = value.get("scope") if value.get("scope") in SCOPES else None
    forgotten_at = _decode_timestamp(value.get("forgotten_at"))
    revision = _decode_revision(value.get("revision"))
    if None in (harness, native, scope, forgotten_at, revision):
        return None
    return {"harness": harness, "native_id": native, "scope": scope,
            "forgotten_at": forgotten_at, "revision": revision}


def decode_tombstone_page(value):
    if not _exact_keys(value, ("protocol", "origin", "tombstones", "next_cursor", "watermark")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    rows = value.get("tombstones")
    next_cursor = value.get("next_cursor")
    watermark = _decode_revision(value.get("watermark"))
    if origin is None or not isinstance(rows, list) or len(rows) > MAX_PAGE:
        return None
    if next_cursor is not None and not isinstance(next_cursor, str):
        return None
    if watermark is None:
        return None
    clean = []
    for row in rows:
        item = _decode_tombstone_row(row)
        if item is None:
            return None
        clean.append(item)
    return {"protocol": PROTOCOL, "origin": origin, "tombstones": clean,
            "next_cursor": next_cursor, "watermark": watermark}


def decode_tombstone_push_response(value):
    if not _exact_keys(value, ("protocol", "origin", "revision", "applied", "ignored")):
        return None
    if decode_protocol(value.get("protocol")) is None:
        return None
    origin = decode_origin(value.get("origin"))
    revision = _decode_revision(value.get("revision"))
    applied = value.get("applied")
    ignored = value.get("ignored")
    if origin is None or revision is None or not isinstance(applied, list) or not isinstance(ignored, list):
        return None
    clean = []
    for group in (applied, ignored):
        keys = []
        for item in group:
            if not _exact_keys(item, ("harness", "native_id")):
                return None
            harness = item.get("harness") if item.get("harness") in HARNESSES else None
            native = _decode_text(item.get("native_id"), 512)
            if harness is None or native is None:
                return None
            keys.append({"harness": harness, "native_id": native})
        clean.append(keys)
    return {"protocol": PROTOCOL, "origin": origin, "revision": revision,
            "applied": clean[0], "ignored": clean[1]}


def decode_error_response(value):
    if not _record(value) or set(value) not in ({"error"}, {"error", "state"}):
        return None
    error = value.get("error")
    if not _exact_keys(error, ("code", "message")):
        return None
    code, message = error.get("code"), error.get("message")
    if code not in ERROR_CODES or not isinstance(message, str) or len(message) > 500:
        return None
    decoded = MirrorError(code, message)
    if "state" in value:
        state = decode_learning_state_response(value["state"])
        if state is None:
            return None
        decoded.state = state
    return decoded


class ConnectionPool:
    def __init__(self, host, port, timeout, factory=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._factory = factory or (lambda: http.client.HTTPSConnection(host, port, timeout=timeout))
        self._connection = None
        self.connections = 0

    def get(self):
        if self._connection is None:
            self._connection = self._factory()
            self.connections += 1
        return self._connection

    def discard(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def close(self):
        self.discard()


class MirrorClient:
    def __init__(self, config, token=None, timeout=REQUEST_TIMEOUT, attempts=ATTEMPTS,
                 factory=None, sleep=time.sleep):
        self.config = config
        self.token = token if token is not None else load_service_token(config.service_token_file)
        self.attempts = attempts
        self._sleep = sleep
        parts = urlsplit(config.url)
        self.pool = ConnectionPool(parts.hostname, parts.port or 443, timeout, factory)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self.pool.close()

    def _auth_headers(self):
        return {
            "CF-Access-Client-Id": self.token[0],
            "CF-Access-Client-Secret": self.token[1],
            "Accept": "application/json",
            "User-Agent": "agent-sessions-mirror/1",
        }

    def _retry_delay(self, attempt, headers):
        retry_after = headers.get("retry-after") if headers else None
        if isinstance(retry_after, str) and retry_after.isdigit():
            return min(60, max(1, int(retry_after)))
        return min(60, 2 ** attempt)

    def _request(self, method, path, body, headers) -> bytes:
        payload, _response_headers = self._request_with_headers(method, path, body, headers)
        return payload

    def _request_with_headers(self, method, path, body, headers) -> tuple[bytes, dict[str, str]]:
        for attempt in range(self.attempts):
            try:
                connection = self.pool.get()
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                response_headers: dict[str, str] = {
                    str(key).lower(): str(item) for key, item in response.getheaders()}
                if len(payload) > MAX_RESPONSE_BYTES:
                    self.pool.discard()
                    raise MirrorError("invalid_response", "mirror response exceeds the limit")
                if getattr(response, "will_close", False):
                    self.pool.discard()
                if response.status >= 400:
                    error = decode_error_response(_loads(payload))
                    if error is None:
                        error = MirrorError("http_error", "mirror returned status %d" % response.status,
                                            status=response.status)
                    error.status = response.status
                    error.retryable = response.status in RETRY_STATUSES
                    if error.retryable and attempt + 1 < self.attempts:
                        self.pool.discard()
                        self._sleep(self._retry_delay(attempt, response_headers))
                        continue
                    raise error
                return cast(bytes, payload), response_headers
            except MirrorError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                self.pool.discard()
                if attempt + 1 >= self.attempts:
                    raise MirrorError("network", "mirror request failed: " + type(exc).__name__) from exc
                self._sleep(self._retry_delay(attempt, None))
        raise MirrorError("network", "mirror request failed")

    def _json(self, method, path, value=None) -> dict[str, Any]:
        body = None
        headers = self._auth_headers()
        if value is not None:
            body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
            if len(body) > MAX_JSON_BYTES:
                raise MirrorError("invalid_request", "request body exceeds the limit")
            headers["Content-Type"] = "application/json"
        payload = self._request(method, path, body, headers)
        decoded = _loads(payload)
        if not isinstance(decoded, dict):
            raise MirrorError("invalid_response", "mirror response is not an object")
        return decoded

    def status(self) -> dict[str, Any]:
        value = self._json("GET", "/api/v1/status")
        if decode_protocol(value.get("protocol")) is None:
            raise MirrorError("unsupported_version", "mirror status protocol is not supported")
        return value

    def hello(self, origin) -> dict[str, Any]:
        request = {"protocol": PROTOCOL, "origin": origin}
        if decode_hello(request) is None:
            raise MirrorError("invalid_request", "hello request is invalid")
        value = self._json("POST", "/api/v1/sync/hello", request)
        decoded = decode_hello_response(value)
        if decoded is None or decoded["origin"] != origin:
            raise MirrorError("invalid_response", "hello response is invalid")
        return decoded

    def learning_state(self, origin) -> dict[str, Any]:
        if decode_origin(origin) is None:
            raise MirrorError("invalid_request", "learning state request is invalid")
        decoded = decode_learning_state_response(
            self._json("GET", "/api/v1/sync/learning/state?" + urlencode({"origin": origin})))
        if decoded is None or decoded["origin"] != origin:
            raise MirrorError("invalid_response", "learning state response is invalid")
        return decoded

    def learning_delta(self, origin, base_revision, digest, changes) -> dict[str, Any]:
        request = {"protocol": PROTOCOL, "origin": origin, "base_revision": base_revision,
                   "digest": digest, "changes": list(changes)}
        try:
            request = learning_sync.validate_mirror_delta(request)
        except learning_sync.SnapshotError as exc:
            raise MirrorError("invalid_request", "learning delta is invalid: " + str(exc)) from exc
        decoded = decode_learning_delta_response(
            self._json("POST", "/api/v1/sync/learning/deltas", request))
        if decoded is None or decoded["origin"] != origin:
            raise MirrorError("invalid_response", "learning delta response is invalid")
        return decoded

    def learning_keys(self, origin, after=None, limit=MAX_PAGE) -> dict[str, Any]:
        if decode_origin(origin) is None or not 1 <= limit <= MAX_PAGE:
            raise MirrorError("invalid_request", "learning keys request is invalid")
        query = {"origin": origin, "limit": limit}
        if after is not None:
            query["after"] = after
        decoded = decode_learning_key_page(self._json("GET", "/api/v1/sync/learning/keys?" + urlencode(query)))
        if decoded is None or decoded["origin"] != origin:
            raise MirrorError("invalid_response", "learning keys response is invalid")
        return decoded

    def _reconcile(self, origin, value) -> dict[str, Any]:
        request = {"protocol": PROTOCOL, "origin": origin, **value}
        decoded = decode_reconcile_response(self._json("POST", "/api/v1/sync/learning/reconcile", request))
        if decoded is None or decoded["origin"] != origin or decoded["action"] != value["action"]:
            raise MirrorError("invalid_response", "reconciliation response is invalid")
        return decoded

    def reconcile_begin(self, origin, base_revision, digest, records) -> dict[str, Any]:
        if decode_digest(digest) is None or records < 0:
            raise MirrorError("invalid_request", "reconciliation begin is invalid")
        return self._reconcile(origin, {"action": "begin", "base_revision": base_revision,
                                        "digest": digest, "records": records})

    def reconcile_apply(self, origin, epoch, changes) -> dict[str, Any]:
        if not 1 <= epoch or not changes:
            raise MirrorError("invalid_request", "reconciliation apply is invalid")
        try:
            clean = [learning_sync.validate_mirror_record(change, origin) for change in changes]
        except learning_sync.SnapshotError as exc:
            raise MirrorError("invalid_request", "reconciliation record is invalid: " + str(exc)) from exc
        return self._reconcile(origin, {"action": "apply", "epoch": epoch, "changes": clean})

    def reconcile_commit(self, origin, epoch, base_revision, digest, records) -> dict[str, Any]:
        if decode_digest(digest) is None or records < 0 or epoch < 1:
            raise MirrorError("invalid_request", "reconciliation commit is invalid")
        return self._reconcile(origin, {"action": "commit", "epoch": epoch,
                                        "base_revision": base_revision, "digest": digest, "records": records})

    def reconcile_abort(self, origin, epoch) -> dict[str, Any]:
        if epoch < 1:
            raise MirrorError("invalid_request", "reconciliation abort is invalid")
        return self._reconcile(origin, {"action": "abort", "epoch": epoch})

    def missing(self, origin, objects) -> dict[str, Any]:
        request = {
            "protocol": PROTOCOL,
            "origin": origin,
            "objects": [{"plaintext_digest": digest, "size": size} for digest, size in objects],
        }
        if decode_missing_request(request) is None:
            raise MirrorError("invalid_request", "missing-objects request is invalid")
        value = self._json("POST", "/api/v1/sync/objects/missing", request)
        decoded = decode_missing_response(value)
        if decoded is None:
            raise MirrorError("invalid_response", "missing-objects response is invalid")
        return decoded

    def generation_by_manifest(self, origin, manifest_digest):
        if decode_origin(origin) is None or decode_digest(manifest_digest) is None:
            raise MirrorError("invalid_request", "generation lookup is invalid")
        query = urlencode({"origin": origin, "manifest": manifest_digest})
        decoded = decode_generation_page(self._json("GET", "/api/v1/sync/generations?" + query))
        if decoded is None:
            raise MirrorError("invalid_response", "generation lookup response is invalid")
        rows = cast(list[dict[str, Any]], decoded["generations"])
        if len(rows) > 1:
            raise MirrorError("invalid_response", "generation lookup returned several rows")
        return rows[0] if rows else None

    def generations(self, origin, after=0, limit=100) -> dict[str, Any]:
        if decode_origin(origin) is None or after < 0 or not 1 <= limit <= MAX_PAGE:
            raise MirrorError("invalid_request", "generation page request is invalid")
        query = urlencode({"origin": origin, "after": after, "limit": limit})
        decoded = decode_generation_page(self._json("GET", "/api/v1/sync/generations?" + query))
        if decoded is None:
            raise MirrorError("invalid_response", "generation page response is invalid")
        return decoded

    def commit_generation(self, origin, base_revision, manifest) -> dict[str, Any]:
        request = {"protocol": PROTOCOL, "origin": origin, "base_revision": base_revision, "manifest": manifest}
        if decode_generation_commit(request) is None:
            raise MirrorError("invalid_request", "generation commit request is invalid")
        decoded = decode_generation_commit_response(self._json("POST", "/api/v1/sync/generations", request))
        if decoded is None:
            raise MirrorError("invalid_response", "generation commit response is invalid")
        if decoded["origin"] != origin or decoded["manifest_digest"] != manifest["manifest_digest"]:
            raise MirrorError("invalid_response", "generation commit response names a different generation")
        return decoded

    def tombstones(self, origin, after=None, limit=100) -> dict[str, Any]:
        if decode_origin(origin) is None or not 1 <= limit <= MAX_PAGE:
            raise MirrorError("invalid_request", "tombstone page request is invalid")
        query = {"origin": origin, "limit": limit}
        if after is not None:
            query["after"] = after
        decoded = decode_tombstone_page(self._json("GET", "/api/v1/sync/tombstones?" + urlencode(query)))
        if decoded is None:
            raise MirrorError("invalid_response", "tombstone page response is invalid")
        return decoded

    def push_tombstones(self, origin, base_revision, tombstones) -> dict[str, Any]:
        request = {"protocol": PROTOCOL, "origin": origin, "base_revision": base_revision,
                   "tombstones": list(tombstones)}
        if decode_tombstone_request(request) is None:
            raise MirrorError("invalid_request", "tombstone push request is invalid")
        decoded = decode_tombstone_push_response(self._json("POST", "/api/v1/sync/tombstones", request))
        if decoded is None:
            raise MirrorError("invalid_response", "tombstone push response is invalid")
        return decoded

    def get_object(self, ciphertext) -> bytes:
        if decode_digest(ciphertext) is None:
            raise MirrorError("invalid_request", "object digest is invalid")
        headers = self._auth_headers()
        headers["Accept"] = "application/octet-stream"
        return self._request("GET", "/api/v1/sync/objects/" + ciphertext, None, headers)

    def get_object_with_metadata(self, ciphertext) -> tuple[bytes, dict[str, str]]:
        if decode_digest(ciphertext) is None:
            raise MirrorError("invalid_request", "object digest is invalid")
        headers = self._auth_headers()
        headers["Accept"] = "application/octet-stream"
        return self._request_with_headers("GET", "/api/v1/sync/objects/" + ciphertext, None, headers)

    def put_object(self, prepared) -> dict[str, Any]:
        headers = self._auth_headers()
        headers.update({
            "Content-Type": "application/octet-stream",
            "X-Plaintext-Digest": prepared.plaintext_digest,
            "X-Plaintext-Size": str(prepared.plaintext_size),
            "X-Encoding": prepared.encoding,
        })
        payload = self._request(
            "PUT", "/api/v1/sync/objects/" + prepared.ciphertext_digest, prepared.ciphertext, headers)
        decoded = decode_put_response(_loads(payload))
        if decoded is None:
            raise MirrorError("invalid_response", "object upload response is invalid")
        return decoded

    def snapshots(self, origin, limit=100, before=None):
        if decode_origin(origin) is None or not 1 <= limit <= MAX_PAGE:
            raise MirrorError("invalid_request", "snapshot list request is invalid")
        if before is not None and (not isinstance(before, str) or decode_cursor(before) is None):
            raise MirrorError("invalid_request", "snapshot list request is invalid")
        query = {"origin": origin, "limit": limit}
        if before is not None:
            query["before"] = before
        decoded = decode_snapshot_list_response(self._json("GET", "/api/v1/sync/snapshots?" + urlencode(query)))
        if decoded is None:
            raise MirrorError("invalid_response", "snapshot list response is invalid")
        return decoded

    def snapshot(self, origin, identifier):
        if decode_origin(origin) is None or decode_digest(identifier) is None:
            raise MirrorError("invalid_request", "snapshot detail request is invalid")
        query = urlencode({"origin": origin})
        decoded = decode_snapshot_detail_response(
            self._json("GET", "/api/v1/sync/snapshots/" + identifier + "?" + query))
        if decoded is None or decoded["snapshot"]["id"] != identifier:
            raise MirrorError("invalid_response", "snapshot detail response is invalid")
        return decoded

    def put_snapshot_page(self, origin, identifier, page, objects):
        request = {"protocol": PROTOCOL, "origin": origin, "page": page, "objects": list(objects)}
        if decode_digest(identifier) is None or decode_snapshot_page(request) is None:
            raise MirrorError("invalid_request", "snapshot object page is invalid")
        decoded = decode_snapshot_page_response(
            self._json("POST", "/api/v1/sync/snapshots/" + identifier + "/objects", request))
        if decoded is None or decoded["snapshot"] != identifier or decoded["page"] != page:
            raise MirrorError("invalid_response", "snapshot object page response is invalid")
        if decoded["objects"] != len(objects) and not decoded["duplicate"]:
            raise MirrorError("invalid_response", "snapshot object page response names a different page")
        return decoded

    def snapshot_objects(self, origin, identifier, page):
        if decode_origin(origin) is None or decode_digest(identifier) is None or page < 0:
            raise MirrorError("invalid_request", "snapshot objects request is invalid")
        decoded = decode_snapshot_objects_response(self._json(
            "GET", "/api/v1/sync/snapshots/" + identifier + "/objects?" + urlencode({"page": page})))
        if decoded is None or decoded["snapshot"] != identifier or decoded["page"] != page:
            raise MirrorError("invalid_response", "snapshot objects response is invalid")
        return decoded

    def commit_snapshot(self, origin, base_revision, snapshot):
        request = {"protocol": PROTOCOL, "origin": origin, "base_revision": base_revision, "snapshot": snapshot}
        if decode_snapshot_commit(request) is None:
            raise MirrorError("invalid_request", "snapshot commit request is invalid")
        decoded = decode_snapshot_commit_response(self._json("POST", "/api/v1/sync/snapshots", request))
        if decoded is None:
            raise MirrorError("invalid_response", "snapshot commit response is invalid")
        if decoded["origin"] != origin or decoded["snapshot"] != snapshot["id"]:
            raise MirrorError("invalid_response", "snapshot commit response names a different snapshot")
        return decoded

    def verify_snapshot(self, origin, identifier):
        request = {"protocol": PROTOCOL, "origin": origin}
        if decode_snapshot_verify_request(request) is None:
            raise MirrorError("invalid_request", "snapshot verification request is invalid")
        decoded = decode_snapshot_verify_response(
            self._json("POST", "/api/v1/sync/snapshots/" + identifier + "/verify", request))
        if decoded is None or decoded["snapshot"] != identifier:
            raise MirrorError("invalid_response", "snapshot verification response is invalid")
        return decoded

    def gc(self, origin, apply):
        request = {"protocol": PROTOCOL, "origin": origin, "apply": apply}
        if decode_gc_request(request) is None:
            raise MirrorError("invalid_request", "gc request is invalid")
        decoded = decode_gc_response(self._json("POST", "/api/v1/sync/gc", request))
        if decoded is None or decoded["origin"] != origin:
            raise MirrorError("invalid_response", "gc response is invalid")
        return decoded

    def replace_object(self, origin, object_row):
        request = {
            "protocol": PROTOCOL,
            "origin": origin,
            "plaintext_digest": object_row["plaintext_digest"],
            "plaintext_size": object_row["plaintext_size"],
            "encoding": object_row["encoding"],
            "ciphertext_digest": object_row["ciphertext_digest"],
            "ciphertext_size": object_row["ciphertext_size"],
        }
        if decode_object_replace_request(request) is None:
            raise MirrorError("invalid_request", "object replacement request is invalid")
        decoded = decode_object_replace_response(self._json(
            "POST", "/api/v1/sync/objects/" + object_row["plaintext_digest"] + "/replace", request))
        if decoded is None or decoded["object"]["ciphertext_digest"] != object_row["ciphertext_digest"]:
            raise MirrorError("invalid_response", "object replacement response is invalid")
        return decoded


@dataclass(frozen=True)
class UploadObject:
    plaintext_digest: str
    size: int
    kind: str = "chunk"


def _upload_refs(objects) -> list[UploadObject]:
    refs = []
    seen = set()
    for item in objects:
        if isinstance(item, UploadObject):
            ref = item
        else:
            values = tuple(item)
            if len(values) == 2:
                ref = UploadObject(values[0], values[1])
            elif len(values) == 3:
                ref = UploadObject(values[0], values[1], values[2])
            else:
                raise MirrorError("invalid_request", "upload object must carry digest, size, and optional kind")
        if (decode_digest(ref.plaintext_digest) is None or isinstance(ref.size, bool)
                or not isinstance(ref.size, int) or not 0 <= ref.size <= MAX_OBJECT_BYTES):
            raise MirrorError("invalid_request", "upload object is invalid")
        if ref.kind not in OBJECT_CLASSES:
            raise MirrorError("invalid_request", "upload object kind is invalid")
        if ref.plaintext_digest in seen:
            continue
        seen.add(ref.plaintext_digest)
        refs.append(ref)
    return refs


def _empty_class():
    return {
        "objects": 0, "plaintext_bytes": 0, "compressed_bytes": 0, "ciphertext_bytes": 0,
        "zlib_objects": 0, "compress_cpu_ms": 0.0, "encrypt_cpu_ms": 0.0,
    }


def _upload_group(client, refs, archive_dir, recipients, prepare):
    counters = {"stored": 0, "already_stored": 0, "classes": {}}
    for ref in refs:
        data = archive_module.read_object(archive_dir, ref.plaintext_digest)
        if len(data) != ref.size:
            raise MirrorError("invalid_request", "archived object size does not match the request")
        prepared = prepare(data, recipients)
        if prepared.plaintext_digest != ref.plaintext_digest:
            raise MirrorError("invalid_request", "prepared object digest does not match the archive")
        if prepared.ciphertext_size > MAX_CIPHERTEXT_BYTES:
            raise MirrorError("invalid_request", "ciphertext exceeds the upload limit")
        result = client.put_object(prepared)
        object_row = result["object"]
        if object_row["plaintext_digest"] != prepared.plaintext_digest:
            raise MirrorError("invalid_response", "object upload response names a different plaintext")
        if result["stored"] and object_row["ciphertext_digest"] != prepared.ciphertext_digest:
            raise MirrorError("invalid_response", "object upload response names a different ciphertext")
        if result["stored"]:
            counters["stored"] += 1
        else:
            counters["already_stored"] += 1
        entry = counters["classes"].setdefault(ref.kind, _empty_class())
        entry["objects"] += 1
        entry["plaintext_bytes"] += prepared.plaintext_size
        entry["compressed_bytes"] += prepared.compressed_size
        entry["ciphertext_bytes"] += prepared.ciphertext_size
        entry["zlib_objects"] += 1 if prepared.encoding == "zlib" else 0
        entry["compress_cpu_ms"] += prepared.compress_cpu_ms
        entry["encrypt_cpu_ms"] += prepared.encrypt_cpu_ms
    return counters


def upload_objects(config, origin, archive_dir, objects, recipients=None, *, concurrency=12,
                   client_factory=None, prepare=None):
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    refs = _upload_refs(objects)
    counters = {
        "objects_requested": len(refs), "objects_known": 0, "objects_missing": 0,
        "objects_stored": 0, "objects_already_stored": 0,
        "bytes_plaintext": 0, "bytes_compressed": 0, "bytes_ciphertext": 0,
        "compress_cpu_ms": 0.0, "encrypt_cpu_ms": 0.0, "classes": {},
    }
    if not refs:
        return counters
    if recipients is None:
        recipients = mirror_crypto.load_recipients(config.age_recipients_file)
    prepare = prepare or mirror_crypto.prepare_object
    factory = client_factory or (lambda: MirrorClient(config))

    primary = factory()
    known: set[str] = set()
    try:
        requested = [(ref.plaintext_digest, ref.size) for ref in refs]
        for start in range(0, len(requested), MAX_MISSING_OBJECTS):
            answer = primary.missing(origin, requested[start:start + MAX_MISSING_OBJECTS])
            known.update(row["plaintext_digest"] for row in answer["known"])
    finally:
        primary.close()
    missing = [ref for ref in refs if ref.plaintext_digest not in known]
    counters["objects_known"] = len(known)
    counters["objects_missing"] = len(missing)
    if not missing:
        return counters

    workers = min(concurrency, len(missing))
    groups = [missing[index::workers] for index in range(workers)]
    clients = [factory() for _ in range(workers)]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_upload_group, client, group, archive_dir, recipients, prepare)
                for client, group in zip(clients, groups)
            ]
            for future in futures:
                result = future.result()
                counters["objects_stored"] += result["stored"]
                counters["objects_already_stored"] += result["already_stored"]
                for kind, entry in result["classes"].items():
                    total = counters["classes"].setdefault(kind, _empty_class())
                    for key in total:
                        total[key] += entry[key]
    finally:
        for client in clients:
            client.close()
    for entry in counters["classes"].values():
        counters["bytes_plaintext"] += entry["plaintext_bytes"]
        counters["bytes_compressed"] += entry["compressed_bytes"]
        counters["bytes_ciphertext"] += entry["ciphertext_bytes"]
        counters["compress_cpu_ms"] += entry["compress_cpu_ms"]
        counters["encrypt_cpu_ms"] += entry["encrypt_cpu_ms"]
    return counters


def _empty_sync_counters() -> dict[str, Any]:
    return {
        "hello": 0, "generations_checked": 0, "generations_committed": 0,
        "generations_duplicate": 0, "generations_tombstoned": 0,
        "objects_requested": 0, "objects_known": 0, "objects_missing": 0,
        "objects_stored": 0, "manifests_uploaded": 0, "manifests_already_stored": 0,
        "bytes_plaintext": 0, "bytes_compressed": 0, "bytes_ciphertext": 0,
        "manifest_plaintext_bytes": 0, "manifest_compressed_bytes": 0,
        "manifest_ciphertext_bytes": 0,
        "tombstones_fetched": 0, "tombstones_pushed": 0,
        "snapshots_created": 0, "snapshots_duplicate": 0, "snapshots_deferred": 0,
        "snapshot_chunks_uploaded": 0,
        "snapshot_objects_uploaded": 0, "snapshot_bytes": 0,
        "learning_batches": 0, "learning_changes": 0, "learning_deletes": 0,
    }


def _wire_manifest(manifest, manifest_object):
    wire = dict(manifest)
    wire["session_metadata"] = None
    wire["manifest_object"] = manifest_object
    return wire


def pending_report(store, remote_name):
    """Estimate the local backlog without a network request."""
    origin = archive_module.origin_for(store.db)
    state = store.mirror_remote_state(remote_name)
    pinned = state is not None and state.get("store_id") is not None
    remote = state["store_id"] if pinned else remote_name
    rows = store.pending_generations(remote, origin, limit=100_000)
    objects: dict[str, int] = {}
    for row in rows:
        manifest = archive_module.generation_manifest(store.db, row["id"])
        for item in list(manifest["chunks"]) + list(manifest["artifacts"]):
            objects.setdefault(item["digest"], item["size"])
    tombstones = store.pending_tombstones(remote, origin)
    learning = 0
    if learning_sync.has_learning(store.db):
        learning = max(0, learning_sync.journal_head(store.db) - int((state or {}).get("learning_seq") or 0))
    batches = (learning + learning_sync.MAX_LEARNING_CHANGES - 1) // learning_sync.MAX_LEARNING_CHANGES
    requests = 1 + (1 if rows else 0) + len(objects) + len(rows) + (1 if batches else 0) + (1 if tombstones else 0)
    return {
        "origin": origin, "remote": remote_name, "pinned": pinned,
        "store_id": state["store_id"] if pinned else None,
        "revision": state["revision"] if pinned else None,
        "watermark": state["tombstone_watermark"] if pinned else None,
        "learning_seq": state["learning_seq"] if pinned else None,
        "last_sync_at": None if state is None else state["last_sync_at"],
        "last_error": None if state is None else state["last_error"],
        "generations": len(rows), "objects": len(objects), "bytes": sum(objects.values()),
        "tombstones": len(tombstones), "learning_changes": learning, "learning_batches": batches,
        "requests": requests,
    }


def _apply_remote_tombstones(store, runtime, origin, store_id, watermark):
    """Apply newer remote forget decisions with local learning withdrawal."""
    applied = 0
    if watermark is None:
        watermark = 0
    cursor = None
    while True:
        page = runtime.tombstones(origin, cursor)
        for row in page["tombstones"]:
            existing = store.tombstone_forgotten_at(origin, row["harness"], row["native_id"])
            if existing is not None and existing >= float(row["forgotten_at"]):
                continue
            database = getattr(store, "db", None)
            if database is not None and hasattr(database, "execute") and learning_sync.has_learning(database):
                learning_module.apply_remote_forget(
                    store, origin, row["harness"], row["native_id"], row["scope"], row["forgotten_at"])
                store.mark_tombstone_durable(origin, row["harness"], row["native_id"], store_id)
            else:
                store.apply_remote_tombstone(
                    origin, row["harness"], row["native_id"], row["scope"], row["forgotten_at"], store_id)
            applied += 1
        watermark = max(watermark, page["watermark"])
        cursor = page["next_cursor"]
        if cursor is None:
            return watermark, applied


def _snapshot_due(runtime, origin):
    rows = runtime.snapshots(origin, 1)["snapshots"]
    return not rows or time.time() - float(rows[0]["created_at"]) >= SNAPSHOT_MIN_INTERVAL_SECONDS


def snapshot_retry_delay(failures):
    return min(SNAPSHOT_RETRY_BASE_SECONDS * 2 ** (int(failures) - 1), SNAPSHOT_MIN_INTERVAL_SECONDS)


def _snapshot_retry_due(pinned, now):
    return now - float(pinned["snapshot_failed_at"]) >= snapshot_retry_delay(pinned["snapshot_failures"])


def sync(store, config, remote_name, *, refresh=None, client=None, upload_client_factory=None,
         recipients=None, concurrency=12, prepare=None, progress=None):
    """Deliver the local archive to one Cloudflare mirror and pin its identity."""
    if not isinstance(remote_name, str) or not remote_name:
        raise MirrorError("invalid_config", "remote name is required")
    origin = archive_module.origin_for(store.db)
    counters = _empty_sync_counters()
    factory = upload_client_factory
    owner = client is None
    runtime = client
    store_id = None
    revision = 0
    watermark = 0
    pinned = None
    learning_seq = 0
    learning_digest = None
    learning_revision = 0
    try:
        if runtime is None:
            runtime = MirrorClient(config)
        hello = runtime.hello(origin)
        counters["hello"] += 1
        store_id = hello["store_id"]
        revision = hello["revision"]
        pinned = store.mirror_remote_state(remote_name)
        if pinned is not None and pinned["store_id"] != store_id:
            if store.remote_ack_count(pinned["store_id"]) or int(pinned.get("learning_seq") or 0) > 0:
                raise MirrorError(
                    "remote_identity_changed",
                    "remote store identity changed; run `agent-sessions mirror adopt --remote %s`" % remote_name)
            store.forget_mirror_remote(remote_name)
            pinned = None
        if pinned is not None and pinned["store_id"] == store_id:
            watermark = pinned["tombstone_watermark"]
        learning_seq = 0 if pinned is None else int(pinned.get("learning_seq") or 0)
        learning_digest = None if pinned is None else pinned.get("learning_digest")
        learning_revision = int(hello["learning"]["revision"])
        learning_pending = False
        changed = pinned is None
        journal_before = int(store.learning_journal_head())

        if hello["tombstone_watermark"] > watermark:
            watermark, applied = _apply_remote_tombstones(store, runtime, origin, store_id, watermark)
            counters["tombstones_fetched"] += applied
            if applied:
                changed = True

        if refresh is not None:
            refresh()

        pending_tombstones = store.pending_tombstones(store_id, origin)
        if pending_tombstones:
            for start in range(0, len(pending_tombstones), MAX_TOMBSTONES):
                batch = [
                    {"harness": row["harness"], "native_id": row["native_id"],
                     "scope": row["scope"], "forgotten_at": row["forgotten_at"]}
                    for row in pending_tombstones[start:start + MAX_TOMBSTONES]
                ]
                pushed = runtime.push_tombstones(origin, revision, batch)
                revision = pushed["revision"]
                watermark = max(watermark, revision)
                for item in pushed["applied"]:
                    store.mark_tombstone_durable(origin, item["harness"], item["native_id"], store_id)
                    counters["tombstones_pushed"] += 1
                for item in pushed["ignored"]:
                    store.mark_tombstone_durable(origin, item["harness"], item["native_id"], store_id)
                changed = True

        prepare = prepare or mirror_crypto.prepare_object
        while True:
            pending_generations = store.pending_generations(store_id, origin)
            if not pending_generations:
                break
            for row in pending_generations:
                counters["generations_checked"] += 1
                manifest = archive_module.generation_manifest(store.db, row["id"])
                archive_module.validate_manifest(manifest)
                existing = runtime.generation_by_manifest(origin, manifest["manifest_digest"])
                if existing is not None:
                    if existing["manifest_digest"] != manifest["manifest_digest"]:
                        raise MirrorError("invalid_response", "generation lookup names a different manifest")
                    store.mark_durable(row["id"], store_id, existing["id"])
                    counters["generations_duplicate"] += 1
                    counters["hello"] += 1
                    revision = runtime.hello(origin)["revision"]
                    changed = True
                    if progress is not None:
                        progress(counters["generations_duplicate"] + counters["generations_committed"])
                    continue
                refs = [
                    (chunk["digest"], chunk["size"], "chunk") for chunk in manifest["chunks"]
                ] + [
                    (artifact["digest"], artifact["size"], "artifact") for artifact in manifest["artifacts"]
                ]
                try:
                    if recipients is None:
                        recipients = mirror_crypto.load_recipients(config.age_recipients_file)
                    upload = upload_objects(
                        config, origin, store.archive_dir, refs, cast(list[str], recipients), concurrency=concurrency,
                        client_factory=factory, prepare=prepare)
                    for key in ("objects_requested", "objects_known", "objects_missing", "objects_stored",
                                "bytes_plaintext", "bytes_compressed", "bytes_ciphertext"):
                        counters[key] = int(counters[key]) + cast(int, upload[key])
                    manifest_bytes = json.dumps(
                        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                    prepared = prepare(manifest_bytes, recipients)
                    uploaded = runtime.put_object(prepared)
                    stored_manifest = uploaded["object"]
                    if stored_manifest["plaintext_digest"] != prepared.plaintext_digest:
                        raise MirrorError("invalid_response", "object upload names a different plaintext")
                    if uploaded["stored"]:
                        counters["manifests_uploaded"] += 1
                    else:
                        counters["manifests_already_stored"] += 1
                    counters["manifest_plaintext_bytes"] += prepared.plaintext_size
                    counters["manifest_compressed_bytes"] += prepared.compressed_size
                    counters["manifest_ciphertext_bytes"] += prepared.ciphertext_size
                    wire = _wire_manifest(manifest, stored_manifest["ciphertext_digest"])
                    committed = runtime.commit_generation(origin, revision, wire)
                    if committed["manifest_digest"] != manifest["manifest_digest"]:
                        raise MirrorError("invalid_response", "generation commit names a different manifest")
                    store.mark_durable(row["id"], store_id, committed["generation"])
                    counters["generations_committed"] += 1
                    revision = committed["revision"]
                    changed = True
                    if progress is not None:
                        progress(counters["generations_duplicate"] + counters["generations_committed"])
                except MirrorError as exc:
                    if exc.code == "tombstoned":
                        store.mark_durable(row["id"], store_id, 0)
                        counters["generations_tombstoned"] += 1
                        changed = True
                        counters["hello"] += 1
                        revision = runtime.hello(origin)["revision"]
                        continue
                    store.record_attempt(row["id"], store_id, str(exc))
                    raise

        if config.learning_projection and learning_sync.has_learning(store.db):
            head = learning_sync.journal_head(store.db)
            if head > learning_seq:
                labels = learning_sync.project_labels(store.db)
                exported = learning_sync.mirror_changes(store.db, learning_seq, labels, origin)["changes"]
                for start in range(0, len(exported), learning_sync.MAX_LEARNING_CHANGES):
                    batch = exported[start:start + learning_sync.MAX_LEARNING_CHANGES]
                    digest = learning_sync.changes_digest(batch)
                    try:
                        acked = runtime.learning_delta(origin, learning_revision, digest, batch)
                    except MirrorError as exc:
                        if exc.code != "stale_revision":
                            raise
                        learning_revision = runtime.learning_state(origin)["revision"]
                        acked = runtime.learning_delta(origin, learning_revision, digest, batch)
                    learning_revision = acked["revision"]
                    learning_digest = digest
                    counters["learning_batches"] += 1
                    counters["learning_changes"] += acked["applied"]
                    counters["learning_deletes"] += acked["deleted"]
                learning_seq = head
                learning_pending = True
                changed = True
            else:
                learning_pending = False
        elif not config.learning_projection:
            learning_pending = journal_before > 0

        if changed or (pinned is not None and pinned.get("last_error") is not None):
            store.write_mirror_remote(remote_name, store_id, revision, watermark,
                                      learning_seq=learning_seq, learning_digest=learning_digest)
        snapshot_failed = pinned is not None and int(pinned.get("snapshot_failures") or 0) > 0
        needs_snapshot = (
            counters["generations_committed"] > 0 or counters["generations_tombstoned"] > 0
            or counters["tombstones_fetched"] > 0 or counters["tombstones_pushed"] > 0
            or learning_pending or journal_before > 0 or snapshot_failed
            or (pinned is not None and pinned.get("last_error") is not None))
        snapshot_ready = needs_snapshot
        if needs_snapshot:
            snapshot_ready = (_snapshot_retry_due(pinned, time.time()) if snapshot_failed
                              else _snapshot_due(runtime, origin))
            if not snapshot_ready:
                counters["snapshots_deferred"] += 1
        if needs_snapshot and snapshot_ready:
            try:
                snapshot: dict[str, Any] = upload_snapshot(
                    store, config, client=runtime, origin=origin, revision=revision,
                    recipients=recipients, concurrency=concurrency, prepare=prepare,
                    client_factory=factory)
            except MirrorError:
                store.record_snapshot_failure(remote_name)
                raise
            except (OSError, sqlite3.DatabaseError, backup_module.BackupError,
                    archive_module.ArchiveError) as exc:
                store.record_snapshot_failure(remote_name)
                raise MirrorError("snapshot_failed", "snapshot failed: " + str(exc)) from exc
            store.clear_snapshot_failures(remote_name)
            if snapshot["duplicate"]:
                counters["snapshots_duplicate"] += 1
            else:
                counters["snapshots_created"] += 1
            counters["snapshot_chunks_uploaded"] += snapshot["chunks_uploaded"]
            counters["snapshot_objects_uploaded"] += snapshot["objects_uploaded"]
            counters["snapshot_bytes"] += snapshot["db_size"]
            revision = max(revision, snapshot["revision"])
            store.write_mirror_remote(remote_name, store_id, revision, watermark,
                                      learning_seq=learning_seq, learning_digest=learning_digest)
            if journal_before > 0:
                store.prune_learning_journal(through=journal_before)
        counters["store_id"] = store_id
        counters["revision"] = revision
        counters["tombstone_watermark"] = watermark
        counters["learning_revision"] = learning_revision
        counters["learning_seq"] = learning_seq
        counters["unchanged"] = (not changed and counters["snapshots_created"] == 0
                                 and counters["snapshots_duplicate"] == 0)
        return counters
    except MirrorError as exc:
        if store_id is not None and exc.code != "remote_identity_changed":
            store.write_mirror_remote(remote_name, store_id, revision, watermark,
                                      learning_seq=learning_seq, learning_digest=learning_digest,
                                      error=str(exc)[:500])
        elif store_id is None:
            store.record_mirror_error(remote_name, str(exc))
        raise
    finally:
        if owner and runtime is not None:
            runtime.close()


def _reconcile_batches(records, limit, max_bytes):
    batch = []
    size = 0
    for record in records:
        encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if batch and (len(batch) >= limit or size + len(encoded) > max_bytes):
            yield batch
            batch = []
            size = 0
        batch.append(record)
        size += len(encoded)
    if batch:
        yield batch


def reconcile(store, config, remote_name, *, client=None, progress=None,
              chunk_records=200, chunk_bytes=4 * 1024 * 1024):
    """Replace one origin's searchable projection with the local live record set."""
    if not isinstance(remote_name, str) or not remote_name:
        raise MirrorError("invalid_config", "remote name is required")
    if not config.learning_projection:
        raise MirrorError(
            "invalid_config",
            "searchable learning is disabled; run `agent-sessions mirror enable --searchable-learning`")
    origin = archive_module.origin_for(store.db)
    counters = {"current": False, "records": 0, "missing": 0, "extra": 0, "changed": 0,
                "chunks": 0, "applied": 0, "revision": 0, "epoch": 1,
                "tombstones_fetched": 0}
    owner = client is None
    runtime = client or MirrorClient(config)
    try:
        hello = runtime.hello(origin)
        store_id = hello["store_id"]
        pinned = store.mirror_remote_state(remote_name)
        if pinned is not None and pinned["store_id"] != store_id:
            if store.remote_ack_count(pinned["store_id"]) or int(pinned.get("learning_seq") or 0) > 0:
                raise MirrorError(
                    "remote_identity_changed",
                    "remote store identity changed; run `agent-sessions mirror adopt --remote %s`" % remote_name)
            store.forget_mirror_remote(remote_name)
            pinned = None
        watermark = 0 if pinned is None else int(pinned["tombstone_watermark"])
        if hello["tombstone_watermark"] > watermark:
            watermark, applied = _apply_remote_tombstones(
                store, runtime, origin, store_id, watermark)
            counters["tombstones_fetched"] = applied
        for attempt in range(2):
            learning = runtime.learning_state(origin)
            revision = learning["revision"]
            head = learning_sync.journal_head(store.db) if learning_sync.has_learning(store.db) else 0
            labels = learning_sync.project_labels(store.db) if learning_sync.has_learning(store.db) else {}
            records = learning_sync.mirror_snapshot(store.db, labels, origin) \
                if learning_sync.has_learning(store.db) else []
            local = {(record["record"], record["id"]): record["digest"] for record in records}
            remote = {}
            cursor = None
            epoch = learning["epoch"]
            while True:
                page = runtime.learning_keys(origin, cursor, MAX_PAGE)
                epoch = page["epoch"]
                for key in page["keys"]:
                    remote[(key["record"], key["id"])] = key["digest"]
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            counters.update(records=len(records), revision=revision, epoch=epoch)
            counters["missing"] = len([key for key in local if key not in remote])
            counters["extra"] = len([key for key in remote if key not in local])
            counters["changed"] = len([key for key in local if key in remote and remote[key] != local[key]])
            if not (counters["missing"] or counters["extra"] or counters["changed"]):
                counters["current"] = True
                if pinned is not None:
                    store.write_mirror_remote(
                        remote_name, store_id, hello["revision"], watermark,
                        learning_seq=head, learning_digest=learning["digest"])
                return counters
            digest = learning_sync.changes_digest(records)
            begun = runtime.reconcile_begin(origin, revision, digest, len(records))
            epoch = begun["epoch"]
            counters["epoch"] = epoch
            if begun["current"]:
                counters["current"] = True
                counters["revision"] = begun["revision"]
                if pinned is not None:
                    store.write_mirror_remote(
                        remote_name, store_id, hello["revision"], watermark,
                        learning_seq=head, learning_digest=digest)
                return counters
            try:
                for batch in _reconcile_batches(records, chunk_records, chunk_bytes):
                    runtime.reconcile_apply(origin, epoch, batch)
                    counters["chunks"] += 1
                    if progress is not None:
                        progress(counters["chunks"], len(records))
                committed = runtime.reconcile_commit(origin, epoch, revision, digest, len(records))
            except MirrorError as exc:
                if exc.code not in ("stale_revision", "conflict") or attempt > 0:
                    raise
                try:
                    runtime.reconcile_abort(origin, epoch)
                except MirrorError as abort_error:
                    if abort_error.code not in ("stale_revision", "conflict"):
                        raise
                continue
            counters.update(applied=committed["applied"], revision=committed["revision"],
                            epoch=committed["epoch"], current=committed["replayed"])
            store.write_mirror_remote(
                remote_name, store_id, hello["revision"], watermark,
                learning_seq=head, learning_digest=digest)
            return counters
        raise MirrorError("stale_revision", "reconciliation did not settle after a retry")
    finally:
        if owner:
            runtime.close()


def _snapshot_pages(runtime, origin) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = runtime.snapshots(origin, MAX_PAGE, before=cursor)
        rows.extend(page["snapshots"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows


def _collector_origin(client) -> str:
    value = client.status()
    principal = value.get("principal")
    if not isinstance(principal, dict) or principal.get("kind") != "collector":
        raise MirrorError("invalid_response", "mirror status did not report a collector principal")
    origin = decode_origin(principal.get("origin_host"))
    if origin is None:
        raise MirrorError("invalid_response", "mirror status has no collector origin")
    return origin


def _resolve_known(client, origin, refs) -> dict[str, dict[str, Any]]:
    wanted: dict[str, int | None] = {}
    for digest, size in refs:
        if decode_digest(digest) is None:
            raise MirrorError("invalid_request", "object digest is invalid")
        if size is not None:
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_OBJECT_BYTES:
                raise MirrorError("invalid_request", "object size is invalid")
        known_size = wanted.get(digest)
        if known_size is not None and size is not None and known_size != size:
            raise MirrorError("invalid_request", "object sizes conflict")
        wanted[digest] = size if size is not None else known_size
    items = list(wanted.items())
    known: dict[str, dict[str, Any]] = {}
    for start in range(0, len(items), 5000):
        batch = items[start:start + 5000]
        answer: dict[str, Any] = client.missing(origin, [(digest, 0 if size is None else size) for digest, size in batch])
        if answer["origin"] != origin:
            raise MirrorError("invalid_response", "missing-objects response names a different origin")
        for row in answer["known"]:
            digest = row["plaintext_digest"]
            if digest not in wanted:
                raise MirrorError("invalid_response", "missing-objects response carries an unrequested object")
            expected = wanted[digest]
            if expected is not None and row["plaintext_size"] != expected:
                raise MirrorError("invalid_response", "object size does not match the request")
            known[digest] = row
    missing = [digest for digest in wanted if digest not in known]
    if missing:
        raise MirrorError("missing_object", "object is not stored: " + missing[0])
    return known


def _download_object(client, identity_path, row) -> bytes:
    digest = row["plaintext_digest"]
    try:
        ciphertext = client.get_object(row["ciphertext_digest"])
        if len(ciphertext) != row["ciphertext_size"]:
            raise MirrorError("corrupt_object", "object failed verification: " + digest)
        return mirror_crypto.decode_object(
            ciphertext, identity_path, row["encoding"], digest, row["plaintext_size"])
    except mirror_crypto.CryptoError as exc:
        raise MirrorError("corrupt_object", "object failed verification: " + digest) from exc


def _chunk_row(chunk) -> dict[str, Any]:
    return {
        "plaintext_digest": chunk["plaintext_digest"],
        "plaintext_size": chunk["size"],
        "ciphertext_digest": chunk["ciphertext_digest"],
        "ciphertext_size": chunk["ciphertext_size"],
        "encoding": chunk["encoding"],
    }


def _object_row(item) -> dict[str, Any]:
    return {
        "plaintext_digest": item["plaintext_digest"],
        "plaintext_size": item["size"],
        "ciphertext_digest": item["ciphertext_digest"],
        "ciphertext_size": item["ciphertext_size"],
        "encoding": item["encoding"],
    }


def _check_snapshot_image(destination, snapshot) -> Path:
    digest = hashlib.sha256()
    size = 0
    with open(destination, "rb") as stream:
        while True:
            block = stream.read(1 << 20)
            if not block:
                break
            digest.update(block)
            size += len(block)
    if size != snapshot["db_size"] or digest.hexdigest() != snapshot["db_digest"]:
        raise MirrorError("corrupt_object", "snapshot database failed verification: " + snapshot["id"])
    try:
        with contextlib.closing(
                sqlite3.connect("file:%s?mode=ro" % Path(destination).as_posix(), uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MirrorError("corrupt_object", "snapshot database failed integrity_check: " + snapshot["id"])
    except sqlite3.DatabaseError as exc:
        raise MirrorError("corrupt_object", "snapshot database is not readable: " + snapshot["id"]) from exc
    return Path(destination)


def _reassemble_snapshot(client, identity_path, detail, destination) -> Path:
    snapshot = detail["snapshot"]
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as stream:
        for chunk in detail["chunks"]:
            data = _download_object(client, identity_path, _chunk_row(chunk))
            if len(data) != chunk["size"]:
                raise MirrorError("corrupt_object", "object failed verification: " + chunk["plaintext_digest"])
            stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return _check_snapshot_image(destination, snapshot)


def _snapshot_objects(client, origin, row) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for page in range(snapshot_page_count(row["objects"])):
        rows = client.snapshot_objects(origin, row["id"], page)["objects"]
        if objects and rows and objects[-1]["plaintext_digest"] >= rows[0]["plaintext_digest"]:
            raise MirrorError("invalid_response", "snapshot object pages are out of order")
        objects.extend(rows)
    pinned = [{"plaintext_digest": item["plaintext_digest"], "size": item["size"]} for item in objects]
    if (len(pinned) != row["objects"] or sum(item["size"] for item in pinned) != row["object_bytes"]
            or archive_module.sha256(encode_snapshot_page(pinned).encode("ascii")) != row["objects_digest"]):
        raise MirrorError("invalid_response", "snapshot objects do not match the snapshot digest")
    return objects


def _snapshot_backup_dir(client, identity_path, detail, objects_rows, directory) -> dict[str, Any]:
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reassemble_snapshot(client, identity_path, detail, directory / "sessions.sqlite3")
    objects = []
    for item in objects_rows:
        data = _download_object(client, identity_path, _object_row(item))
        archive_module._write_object(directory, data)
        objects.append({"digest": item["plaintext_digest"], "size": item["size"]})
    manifest = {
        "protocol": PROTOCOL,
        "created_at": detail["snapshot"]["created_at"],
        "schema_version": detail["snapshot"]["schema_version"],
        "objects": objects,
        "generations": 0,
        "tombstones": 0,
    }
    path = directory / backup_module.MANIFEST
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return manifest


def _generation_pages(client, origin) -> list[dict[str, Any]]:
    rows = []
    after = 0
    while True:
        page = client.generations(origin, after=after, limit=MAX_PAGE)
        rows.extend(page["generations"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows
        after = int(cursor)


def _object_row_from_response(ciphertext, response_headers) -> dict[str, Any]:
    plaintext = decode_digest(response_headers.get("x-plaintext-digest"))
    encoding = response_headers.get("x-encoding")
    plaintext_size = response_headers.get("x-plaintext-size")
    ciphertext_size = response_headers.get("content-length")
    if plaintext is None or encoding not in ENCODINGS:
        raise MirrorError("invalid_response", "object response metadata is invalid")
    if plaintext_size is None or not plaintext_size.isdigit() or not 0 <= int(plaintext_size) <= MAX_OBJECT_BYTES:
        raise MirrorError("invalid_response", "object response metadata is invalid")
    if ciphertext_size is None or not ciphertext_size.isdigit() or int(ciphertext_size) > MAX_CIPHERTEXT_BYTES:
        raise MirrorError("invalid_response", "object response metadata is invalid")
    return {
        "plaintext_digest": plaintext,
        "plaintext_size": int(plaintext_size),
        "encoding": encoding,
        "ciphertext_digest": ciphertext,
        "ciphertext_size": int(ciphertext_size),
    }


def _download_manifest(client, identities, ciphertext) -> tuple[dict[str, Any], bytes]:
    payload, response_headers = client.get_object_with_metadata(ciphertext)
    row = _object_row_from_response(ciphertext, response_headers)
    if len(payload) != row["ciphertext_size"]:
        raise MirrorError("corrupt_object", "object failed verification: " + row["plaintext_digest"])
    last = None
    for identity in identities:
        try:
            return row, mirror_crypto.decode_object(
                payload, identity, row["encoding"], row["plaintext_digest"], row["plaintext_size"])
        except mirror_crypto.CryptoError as exc:
            last = exc
    raise MirrorError("corrupt_object", "object failed verification: " + row["plaintext_digest"]) from last


def _load_manifests(client, origin, rows, identities, pairs=None) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    if pairs is None:
        pairs = [_download_manifest(client, identities, row["manifest_object"]) for row in rows]
    result = []
    for row, (manifest_row, data) in zip(rows, pairs):
        try:
            manifest = json.loads(data.decode("utf-8"))
            clean = archive_module.validate_manifest(manifest)
        except (UnicodeDecodeError, ValueError, archive_module.ArchiveError) as exc:
            raise MirrorError("corrupt_object", "manifest failed verification: " + row["manifest_digest"]) from exc
        if clean.manifest_digest != row["manifest_digest"] or clean.origin_host != origin:
            raise MirrorError("corrupt_object", "manifest failed verification: " + row["manifest_digest"])
        result.append((row, manifest, manifest_row))
    return result


def _download_with_identities(client, identities, row) -> bytes:
    last = None
    for identity in identities:
        try:
            return _download_object(client, identity, row)
        except MirrorError as exc:
            if exc.code != "corrupt_object":
                raise
            last = exc
    if last is not None:
        raise last
    raise MirrorError("invalid_config", "an age identity is required to decode the mirror")


def upload_snapshot(store, config, *, client, origin, revision, recipients=None, concurrency=12,
                    prepare=None, client_factory=None) -> dict[str, Any]:
    if recipients is None:
        recipients = mirror_crypto.load_recipients(config.age_recipients_file)
    prepare = prepare or mirror_crypto.prepare_object
    factory = client_factory or (lambda: MirrorClient(config))
    with tempfile.TemporaryDirectory(prefix="mirror-snapshot-") as temporary:
        destination = Path(temporary) / "snapshot"

        def publish(directory, metadata: dict[str, Any]) -> dict[str, Any]:
            database = Path(directory) / "sessions.sqlite3"
            if len(metadata["objects"]) > MAX_SNAPSHOT_OBJECTS:
                raise MirrorError(
                    "snapshot_failed",
                    "snapshot pins %d archive objects, above the %d the protocol allows"
                    % (len(metadata["objects"]), MAX_SNAPSHOT_OBJECTS))
            chunk_sizes: dict[str, int] = {}
            for chunk in metadata["chunks"]:
                chunk_sizes.setdefault(chunk["digest"], chunk["size"])
            chunk_refs = list(chunk_sizes.items())
            known: dict[str, dict[str, Any]] = {}
            for start in range(0, len(chunk_refs), MAX_MISSING_OBJECTS):
                answer = client.missing(origin, chunk_refs[start:start + MAX_MISSING_OBJECTS])
                known.update({row["plaintext_digest"]: row for row in answer["known"]})
            chunks_uploaded: int = 0
            chunk_plaintext: int = 0
            chunk_ciphertext: int = 0
            with open(database, "rb") as stream:
                for chunk in metadata["chunks"]:
                    row = known.get(chunk["digest"])
                    if row is not None:
                        if row["plaintext_size"] != chunk["size"]:
                            raise MirrorError("invalid_response", "known snapshot chunk has the wrong size")
                        continue
                    stream.seek(chunk["offset"])
                    data = stream.read(chunk["size"])
                    if archive_module.sha256(data) != chunk["digest"]:
                        raise MirrorError("corrupt_object", "snapshot chunk failed verification: " + chunk["digest"])
                    prepared = prepare(data, recipients)
                    result = client.put_object(prepared)
                    stored = result["object"]
                    if stored["plaintext_digest"] != chunk["digest"] or stored["plaintext_size"] != chunk["size"]:
                        raise MirrorError("invalid_response", "snapshot chunk upload names a different object")
                    known[chunk["digest"]] = stored
                    chunks_uploaded += 1
                    chunk_plaintext += int(prepared.plaintext_size)
                    chunk_ciphertext += int(prepared.ciphertext_size)
            object_refs = [(item["digest"], item["size"], "chunk") for item in metadata["objects"]]
            upload: dict[str, Any] = upload_objects(
                config, origin, store.archive_dir, object_refs, recipients,
                concurrency=concurrency, client_factory=factory, prepare=prepare)
            final: dict[str, dict[str, Any]] = _resolve_known(client, origin, chunk_refs)
            pinned = sorted(({"plaintext_digest": item["digest"], "size": item["size"]} for item in metadata["objects"]),
                            key=lambda item: item["plaintext_digest"])
            for page, start in enumerate(range(0, len(pinned), MAX_SNAPSHOT_PAGE_OBJECTS)):
                client.put_snapshot_page(origin, metadata["db_digest"], page,
                                         pinned[start:start + MAX_SNAPSHOT_PAGE_OBJECTS])
            wire: dict[str, Any] = {
                "id": metadata["db_digest"],
                "created_at": metadata["created_at"],
                "schema_version": metadata["schema_version"],
                "db_digest": metadata["db_digest"],
                "db_size": metadata["db_size"],
                "chunks": [
                    {"plaintext_digest": chunk["digest"], "size": chunk["size"],
                     "ciphertext_digest": final[chunk["digest"]]["ciphertext_digest"]}
                    for chunk in metadata["chunks"]
                ],
                "objects": len(pinned),
                "object_bytes": sum(item["size"] for item in pinned),
                "objects_digest": archive_module.sha256(encode_snapshot_page(pinned).encode("ascii")),
            }
            committed: dict[str, Any] = client.commit_snapshot(origin, revision, wire)
            return {
                "id": metadata["db_digest"], "revision": committed["revision"],
                "duplicate": committed["duplicate"], "db_size": metadata["db_size"],
                "chunks": len(metadata["chunks"]), "chunks_uploaded": chunks_uploaded,
                "objects": len(metadata["objects"]), "objects_uploaded": upload["objects_stored"],
                "object_bytes": metadata["object_bytes"],
                "bytes_plaintext": upload["bytes_plaintext"] + chunk_plaintext,
                "bytes_ciphertext": upload["bytes_ciphertext"] + chunk_ciphertext,
            }

        return backup_module.snapshot(store, destination, publish)


def snapshots(config, *, client=None, limit=100) -> list[dict[str, Any]]:
    owner = client is None
    runtime = client or MirrorClient(config)
    try:
        origin = _collector_origin(runtime)
        return runtime.snapshots(origin, limit)["snapshots"]
    finally:
        if owner:
            runtime.close()


def gc(config, *, apply=False, client=None) -> dict[str, Any]:
    owner = client is None
    runtime = client or MirrorClient(config)
    try:
        origin = _collector_origin(runtime)
        result = runtime.gc(origin, apply)
        if not apply:
            return result
        total = dict(result)
        for _batch in range(10000):
            if result["objects_swept"] < MAX_GC_SWEEP and result["snapshots_pruned"] == 0:
                return total
            result = runtime.gc(origin, True)
            for key in ("snapshots_pruned", "objects_marked", "bytes_marked",
                        "objects_swept", "bytes_swept"):
                total[key] += result[key]
            total["snapshots_retained"] = result["snapshots_retained"]
        raise MirrorError("gc_incomplete", "garbage collection did not finish in 10000 batches")
    finally:
        if owner:
            runtime.close()


def verify(config, *, client=None, objects=False, sample=None, concurrency=12,
           client_factory=None) -> dict[str, Any]:
    owner = client is None
    runtime = client or MirrorClient(config)
    identity = config.age_identity_file
    verified: set[str] = set()

    def parallel(items, work):
        if not items:
            return []
        workers = min(concurrency, len(items))
        if workers < 2 or (client_factory is None and not owner):
            return [work(runtime, item) for item in items]
        make = client_factory or (lambda: MirrorClient(config))
        clients = [make() for _ in range(workers)]
        results: list[Any] = [None] * len(items)
        try:
            def run(worker, group):
                for index, item in group:
                    results[index] = work(worker, item)
            indexed = list(enumerate(items))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(run, worker, indexed[index::workers])
                    for index, worker in enumerate(clients)
                ]
                for future in futures:
                    future.result()
        finally:
            for item in clients:
                item.close()
        return results

    def download(rows) -> None:
        pending = [row for row in rows if row["plaintext_digest"] not in verified]
        parallel(pending, lambda worker, row: _download_object(worker, identity, row))
        verified.update(row["plaintext_digest"] for row in pending)

    def reassemble(detail, destination) -> Path:
        snapshot = detail["snapshot"]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        cursor = 0
        placed = []
        for chunk in detail["chunks"]:
            placed.append((chunk, cursor))
            cursor += chunk["size"]
        with open(destination, "wb") as stream:
            stream.truncate(snapshot["db_size"])
        descriptor = os.open(destination, os.O_WRONLY)

        def write(worker, item):
            chunk, offset = item
            data = _download_object(worker, identity, _chunk_row(chunk))
            if len(data) != chunk["size"]:
                raise MirrorError("corrupt_object", "object failed verification: " + chunk["plaintext_digest"])
            os.pwrite(descriptor, data, offset)

        try:
            parallel(placed, write)
        finally:
            os.close(descriptor)
        return _check_snapshot_image(destination, snapshot)

    try:
        origin = _collector_origin(runtime)
        generations = _generation_pages(runtime, origin)
        identities = [config.age_identity_file]
        pairs = parallel(generations,
                         lambda worker, row: _download_manifest(worker, identities, row["manifest_object"]))
        manifests = _load_manifests(runtime, origin, generations, identities, pairs=pairs)
        referenced: dict[str, int] = {}
        for _row, manifest, _manifest_row in manifests:
            for item in [*manifest["chunks"], *manifest["artifacts"]]:
                known_size = referenced.setdefault(item["digest"], item["size"])
                if known_size != item["size"]:
                    raise MirrorError("corrupt_object", "object size conflicts: " + item["digest"])
        _resolve_known(runtime, origin, list(referenced.items()))
        snapshot_rows: list[dict[str, Any]] = _snapshot_pages(runtime, origin)
        snapshot_objects: dict[str, int] = {}
        chunks: int = 0
        attested: int = 0
        with tempfile.TemporaryDirectory(prefix="mirror-verify-") as temporary:
            for index, snapshot in enumerate(snapshot_rows):
                detail = runtime.snapshot(origin, snapshot["id"])
                pinned = _snapshot_objects(runtime, origin, detail["snapshot"])
                reassemble(detail, Path(temporary) / ("%d.sqlite3" % index))
                chunks += len(detail["chunks"])
                sampled_all = objects and (sample is None or sample >= len(pinned))
                if sampled_all:
                    download([_object_row(item) for item in pinned])
                    runtime.verify_snapshot(origin, snapshot["id"])
                    attested += 1
                for item in pinned:
                    known_size = snapshot_objects.setdefault(item["plaintext_digest"], item["size"])
                    if known_size != item["size"]:
                        raise MirrorError("corrupt_object", "object size conflicts: " + item["plaintext_digest"])
        all_objects: dict[str, int] = dict(referenced)
        for digest, size in snapshot_objects.items():
            known_size = all_objects.setdefault(digest, size)
            if known_size != size:
                raise MirrorError("corrupt_object", "object size conflicts: " + digest)
        known: dict[str, dict[str, Any]] = _resolve_known(runtime, origin, list(all_objects.items()))
        sampled = 0
        if objects:
            count = sample if sample is not None else max(100, (len(all_objects) * 5 + 99) // 100)
            count = min(count, len(all_objects))
            digests = sorted(all_objects)[:count]
            parallel([known[digest] for digest in digests],
                     lambda worker, row: _download_object(worker, identity, row))
            sampled = len(digests)
        return {
            "generations": len(generations), "snapshots": len(snapshot_rows),
            "objects": len(all_objects), "chunks": chunks, "sampled": sampled,
            "attested": attested, "bytes": sum(all_objects.values()),
        }
    finally:
        if owner:
            runtime.close()


def restore(config, remote_name, snapshot_id, destination, *, client=None, identity_path=None) -> dict[str, Any]:
    owner = client is None
    runtime = client or MirrorClient(config)
    identity = Path(identity_path or config.age_identity_file)
    try:
        origin = _collector_origin(runtime)
        detail: dict[str, Any] = runtime.snapshot(origin, snapshot_id)
        pinned = _snapshot_objects(runtime, origin, detail["snapshot"])
        with tempfile.TemporaryDirectory(prefix="mirror-restore-") as temporary:
            backup_dir = Path(temporary) / "backup"
            _snapshot_backup_dir(runtime, identity, detail, pinned, backup_dir)
            runtime.verify_snapshot(origin, snapshot_id)
            backup_module.restore(backup_dir, destination)
        from agent_memory.store import Store
        replayed = 0
        with Store(destination) as store:
            cursor = None
            applied = 0
            while True:
                page: dict[str, Any] = runtime.tombstones(origin, cursor)
                for row in page["tombstones"]:
                    learning_module.apply_remote_forget(
                        store, origin, row["harness"], row["native_id"], row["scope"], row["forgotten_at"])
                    applied += 1
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            rows: list[dict[str, Any]] = _generation_pages(runtime, origin)
            manifests = _load_manifests(runtime, origin, rows, [identity])
            for _row, manifest, _manifest_row in manifests:
                if store.db.execute(
                        "SELECT 1 FROM archive_generation WHERE manifest_digest = ?",
                        (manifest["manifest_digest"],)).fetchone() is not None:
                    continue
                if store.is_tombstoned(origin, manifest["harness"], manifest["native_id"]):
                    continue
                refs = [(item["digest"], item["size"]) for item in [*manifest["chunks"], *manifest["artifacts"]]]
                known: dict[str, dict[str, Any]] = _resolve_known(runtime, origin, refs)
                received: dict[str, bytes] = {}
                for digest, size in refs:
                    if store.object_intact(digest, size):
                        continue
                    received[digest] = _download_object(runtime, identity, known[digest])
                store.commit_generation(manifest, received)
                replayed += 1
            hello: dict[str, Any] = runtime.hello(origin)
            store.write_mirror_remote(
                remote_name, hello["store_id"], hello["revision"], hello["tombstone_watermark"])
        return {
            "snapshot": detail["snapshot"]["id"], "db_size": detail["snapshot"]["db_size"],
            "chunks": len(detail["chunks"]), "objects": len(pinned),
            "replayed": replayed, "tombstones": applied,
        }
    finally:
        if owner:
            runtime.close()


def rotate(config, *, recipients_file, identity_file, old_identity_file=None, client=None) -> dict[str, Any]:
    owner = client is None
    runtime = client or MirrorClient(config)
    old_identity = Path(old_identity_file or config.age_identity_file)
    new_identity = Path(identity_file)
    recipients = mirror_crypto.load_recipients(recipients_file)
    try:
        origin = _collector_origin(runtime)
        generations = _generation_pages(runtime, origin)
        manifests = _load_manifests(runtime, origin, generations, [old_identity, new_identity])
        objects: dict[str, dict[str, Any]] = {}
        for _row, manifest, manifest_row in manifests:
            objects[manifest_row["plaintext_digest"]] = dict(manifest_row)
            refs = [(item["digest"], item["size"]) for item in [*manifest["chunks"], *manifest["artifacts"]]]
            known: dict[str, dict[str, Any]] = _resolve_known(runtime, origin, refs)
            for digest, item in known.items():
                objects[digest] = dict(item)
        for snapshot in _snapshot_pages(runtime, origin):
            detail: dict[str, Any] = runtime.snapshot(origin, snapshot["id"])
            for chunk in detail["chunks"]:
                objects[chunk["plaintext_digest"]] = _chunk_row(chunk)
            for item in _snapshot_objects(runtime, origin, detail["snapshot"]):
                objects[item["plaintext_digest"]] = _object_row(item)
        rotated = 0
        for digest in sorted(objects):
            row: dict[str, Any] = objects[digest]
            data = _download_with_identities(runtime, [old_identity, new_identity], row)
            prepared = mirror_crypto.prepare_object(data, recipients)
            runtime.put_object(prepared)
            runtime.replace_object(origin, {
                "plaintext_digest": prepared.plaintext_digest,
                "plaintext_size": prepared.plaintext_size,
                "encoding": prepared.encoding,
                "ciphertext_digest": prepared.ciphertext_digest,
                "ciphertext_size": prepared.ciphertext_size,
            })
            rotated += 1
        return {"objects": len(objects), "rotated": rotated}
    finally:
        if owner:
            runtime.close()


def _loads(payload):
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
