#!/usr/bin/env python3
"""Normalize and index local agent transcripts."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

DB_FILENAME = "sessions.sqlite3"
SCHEMA_VERSION = 3
_SKIP_TEXT_KEYS = frozenset((
    "encrypted_content", "internal_chat_message_metadata_passthrough", "id", "call_id",
    "type", "status", "role", "timestamp", "ordinal",
))
_REDACTIONS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|sk_live_[A-Za-z0-9]{16,}|rk_live_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|glpat-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"),
    re.compile(r"(?i)\b(api[_-]?key|api[_-]?token|access[_-]?(?:token|key)|refresh[_-]?token|auth(?:orization)?|password|passwd|(?:client[_-]?)?secret(?:[_-]access[_-]?key)?|private[_-]?key|credential)\b\s*[:=]\s*(?:Bearer\s+)?([\"']?)([^\s,;\"']+)"),
)


class StoreError(RuntimeError):
    pass


def default_state_dir():
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "agent-sessions"


def redact(text):
    value = str(text)
    value = _REDACTIONS[2].sub(lambda match: match.group(1) + "=[REDACTED]", value)
    value = _REDACTIONS[0].sub("Bearer [REDACTED]", value)
    value = _REDACTIONS[1].sub("[REDACTED]", value)
    return value


def _text_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_text_value(item) for item in value) if part)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if key in _SKIP_TEXT_KEYS:
                continue
            text = _text_value(item)
            if text:
                parts.append((str(key) + ": " if len(value) > 1 else "") + text)
        return "\n".join(parts)
    return ""


def _content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_content_text(item) for item in value) if part)
    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type in ("image", "input_image", "audio", "input_audio", "encrypted_content"):
            return ""
        if "text" in value:
            return _text_value(value.get("text"))
        if "content" in value:
            return _content_text(value.get("content"))
    return ""


def _record(kind, text="", timestamp=None, call_id=None, raw_type=None, **fields):
    record = {
        "kind": str(kind),
        "text": text if isinstance(text, str) else _text_value(text),
        "timestamp": str(timestamp) if timestamp is not None else None,
        "call_id": str(call_id) if call_id is not None else None,
        "raw_type": str(raw_type) if raw_type is not None else None,
    }
    record.update(fields)
    return record


def _codex_record(obj):
    top_type = obj.get("type", "unknown")
    payload = obj.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    item_type = payload.get("type")
    timestamp = obj.get("timestamp") or payload.get("timestamp")
    if top_type == "response_item":
        if item_type == "message":
            role = payload.get("role") or "unknown"
            return _record(str(role), _content_text(payload.get("content", "")), timestamp, raw_type=top_type, role=role)
        if item_type == "agent_message":
            return _record("assistant", _content_text(payload.get("content", "")), timestamp, raw_type=top_type, role="assistant")
        if item_type in ("function_call", "custom_tool_call"):
            name = payload.get("name") or "tool"
            args = payload.get("arguments", payload.get("input", ""))
            return _record("tool-call", str(name) + ("\n" + _text_value(args) if args else ""), timestamp, payload.get("call_id"), top_type, tool_name=name)
        if item_type in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output", "")
            return _record("tool-output", output, timestamp, payload.get("call_id"), top_type)
        if item_type == "reasoning":
            return _record("reasoning", "", timestamp, raw_type=top_type)
        return _record("response_item:" + str(item_type or "unknown"), _text_value(payload), timestamp, payload.get("call_id"), top_type)
    if top_type == "event_msg":
        if item_type == "task_complete":
            return _record("task_complete", payload.get("last_agent_message", ""), timestamp, raw_type=top_type)
        if item_type == "turn_aborted":
            return _record("turn_aborted", payload.get("reason", ""), timestamp, raw_type=top_type)
        return _record("event_msg:" + str(item_type or "unknown"), "", timestamp, raw_type=top_type)
    if top_type == "session_meta":
        return _record("session_meta", "", timestamp, raw_type=top_type)
    if top_type in ("world_state", "turn_context", "token_usage_record", "inter_agent_communication_metadata"):
        return _record(str(top_type), "", timestamp, raw_type=top_type)
    if top_type == "compacted":
        return _record("compacted", _text_value(payload), timestamp, raw_type=top_type)
    return _record("unknown:" + str(top_type), _text_value(payload), timestamp, raw_type=top_type)


def _claude_record(obj):
    message = obj.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content", obj.get("content", ""))
    texts = [_content_text(content)]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                texts.append(str(name) + "\n" + _text_value(block.get("input", {})))
    kind = obj.get("type") or "unknown"
    role = message.get("role") or kind
    return _record(str(kind), "\n".join(text for text in texts if text), obj.get("timestamp"), raw_type=kind, role=role)


def _muse_record(obj):
    payload = obj.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    event = payload.get("event")
    event = event if isinstance(event, dict) else {}
    event_type = event.get("kind") or payload.get("kind") or "unknown"
    text = ""
    if event_type == "model_user_messages":
        text = _content_text(event.get("messages", []))
    elif event_type == "model_request_configured":
        text = _content_text(event.get("run_context_messages", []))
    elif event_type == "skill_read_observed":
        text = _text_value(event)
    timestamp = obj.get("recorded_at")
    if isinstance(timestamp, (int, float)):
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp / 1_000_000))
    return _record(str(event_type), text, timestamp, raw_type=payload.get("kind", "event"))


def _grok_record(obj):
    kind = obj.get("type") or "unknown"
    content = _content_text(obj.get("content", ""))
    calls = []
    for call in obj.get("tool_calls", []) or []:
        if not isinstance(call, dict):
            continue
        args = call.get("arguments", "")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        calls.append(str(call.get("name") or "tool") + "\n" + _text_value(args))
    text = "\n".join(part for part in (content, *calls) if part)
    return _record(str(kind), text, obj.get("timestamp"), raw_type=kind)


def normalize(obj, harness="codex"):
    if not isinstance(obj, dict):
        return [_record("unknown:non_object", "", raw_type=type(obj).__name__)]
    if harness == "codex":
        return [_codex_record(obj)]
    if harness == "claude":
        return [_claude_record(obj)]
    if harness == "muse":
        return [_muse_record(obj)]
    if harness == "grok":
        return [_grok_record(obj)]
    raise ValueError("unsupported transcript harness: " + str(harness))


def _run_git(path, *args, stats=None):
    if stats is not None:
        stats["git_processes"] += 1
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def project_identity(path, stats=None):
    candidate = Path(path).expanduser().resolve()
    common_dir = _run_git(candidate, "rev-parse", "--path-format=absolute", "--git-common-dir", stats=stats)
    if not common_dir:
        common_dir = _run_git(candidate, "rev-parse", "--git-common-dir", stats=stats)
        if common_dir and not Path(common_dir).is_absolute():
            common_dir = str(candidate / common_dir)
    if common_dir:
        return "git:" + str(Path(common_dir).resolve())
    return "path:" + str(candidate)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _json_line(raw):
    return json.loads(raw.decode("utf-8-sig"))


def _open_source(path, stats=None):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    if stats is not None:
        stats["content_opens"] += 1
    return os.fdopen(fd, "rb")


def _stat_source(path, stats):
    stats["stat_operations"] += 1
    return os.stat(path, follow_symlinks=False)


def _fstat_source(stream, stats):
    if stats is not None:
        stats["stat_operations"] += 1
    return os.fstat(stream.fileno())


def _readline(stream, stats):
    data = stream.readline()
    if stats is not None:
        stats["bytes_read"] += len(data)
    return data


def _prepare_state_dir(state_dir):
    path = Path(os.path.abspath(str(Path(state_dir).expanduser())))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise StoreError("session state path contains a symlink: " + str(current))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise StoreError("session state path must be a real directory: " + str(path))
    os.chmod(path, 0o700)
    return path


def _prepare_database(path):
    if path.is_symlink():
        raise StoreError("session database may not be a symlink: " + str(path))
    if path.exists():
        mode = path.lstat()
        if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
            raise StoreError("session database must be a regular, unlinked file: " + str(path))
    else:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)


def _metadata(path, harness, project_cache, stats=None):
    try:
        with _open_source(path, stats) as stream:
            file_stat = _fstat_source(stream, stats)
            first = _readline(stream, stats)
    except OSError as exc:
        return None, "source_unreadable:" + exc.__class__.__name__
    obj = {}
    if first:
        try:
            obj = _json_line(first)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            obj = {}
    if not isinstance(obj, dict):
        obj = {}
    payload = obj.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    session_id = str(payload.get("id") or payload.get("session_id") or path.stem)
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    project = None
    if cwd:
        if cwd not in project_cache:
            project_cache[cwd] = project_identity(cwd, stats=stats)
        project = project_cache[cwd]
    source = payload.get("source")
    thread_source = payload.get("thread_source")
    if source is None:
        source = thread_source
    try:
        source_json = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if source is not None else None
    except (TypeError, ValueError):
        source_json = None
    parent = payload.get("parent_thread_id")
    if parent is None and isinstance(source, dict):
        spawned = source.get("subagent")
        spawned = spawned.get("thread_spawn") if isinstance(spawned, dict) else None
        parent = spawned.get("parent_thread_id") if isinstance(spawned, dict) else None
    return {
        "id": session_id,
        "cwd": cwd,
        "project": project,
        "parent": str(parent) if parent is not None else None,
        "source": source_json,
        "thread_source": str(thread_source) if thread_source is not None else None,
        "harness": harness,
        "source_path": str(path.absolute()),
        "file_size": file_stat.st_size,
        "file_mtime_ns": file_stat.st_mtime_ns,
        "file_ctime_ns": file_stat.st_ctime_ns,
        "file_dev": file_stat.st_dev,
        "file_ino": file_stat.st_ino,
        "header_digest": _sha256(first) if first else "",
        "updated": file_stat.st_mtime,
    }, None


def _is_learning_worker(meta):
    if not meta.get("source"):
        return False
    try:
        source = json.loads(meta["source"])
    except (TypeError, ValueError):
        return False
    if not isinstance(source, (dict, list)):
        return False
    markers = {"learning_worker", "learning-worker", "agent_session_learning"}

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in markers and child not in (None, False, "", 0):
                    return True
                if key in ("agent_role", "purpose", "worker_kind") and isinstance(child, str) and child.lower() in markers:
                    return True
                if visit(child):
                    return True
        elif isinstance(value, list):
            return any(visit(child) for child in value)
        return False

    return visit(source)


def _ephemeral(meta):
    paths = (meta.get("source_path", ""), meta.get("cwd", ""))
    return any(".ephemeral" in Path(value).parts for value in paths if value)


def _under(path, root):
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        return True
    except (OSError, ValueError):
        return False


class Store:
    def __init__(self, state_dir):
        self.state_dir = _prepare_state_dir(state_dir)
        self.path = self.database_path(self.state_dir)
        try:
            _prepare_database(self.path)
            self.db = sqlite3.connect(str(self.path), timeout=10)
        except StoreError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise StoreError("cannot open session index; run `agent-sessions doctor` for recovery guidance: " + str(exc)) from exc
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 10000")
        try:
            self._initialize()
        except Exception:
            self.db.close()
            raise

    @staticmethod
    def database_path(state_dir):
        return Path(state_dir).expanduser() / DB_FILENAME

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _initialize(self):
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError("session index schema is newer than this tool; upgrade agent-sessions before opening it")
            owned = {"sessions", "events", "events_fts", "index_issues", "unindexed_sources"}
            existing = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and existing & owned:
                raise StoreError("session index has an unversioned schema; run `agent-sessions doctor --rebuild-index` to rebuild only index tables")
            if version not in (0, 1, 2, SCHEMA_VERSION):
                raise StoreError("unsupported session index schema version " + str(version))
            if version == 1:
                if not self._schema_ready(1):
                    raise StoreError("session index schema is incomplete; run `agent-sessions doctor --rebuild-index` to rebuild only index tables")
                self._migrate_v1()
                version = 2
            if version == 2:
                if not self._schema_ready(2):
                    raise StoreError("session index schema is incomplete; run `agent-sessions doctor --rebuild-index` to rebuild only index tables")
                self._migrate_v2()
                return
            if version == SCHEMA_VERSION:
                if not self._schema_ready(SCHEMA_VERSION):
                    raise StoreError("session index schema is incomplete; run `agent-sessions doctor --rebuild-index` to rebuild only index tables")
                return
            self._create_schema()
        except sqlite3.OperationalError as exc:
            self.db.rollback()
            if self._schema_ready():
                return
            if "fts5" in str(exc).lower() or "no such module" in str(exc).lower():
                raise StoreError("this Python SQLite build lacks FTS5; use a Python build with SQLite FTS5 enabled") from exc
            raise StoreError("cannot initialize session index: " + str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            self.db.rollback()
            raise StoreError("session index database is unreadable; preserve learning data and run `agent-sessions doctor`: " + str(exc)) from exc

    def _schema_ready(self, expected_version=SCHEMA_VERSION):
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            existing = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            triggers = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            session_columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
            event_columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        except sqlite3.DatabaseError:
            return False
        return (
            version == expected_version
            and {"sessions", "events", "events_fts", "index_issues"} <= existing
            and {"events_ai", "events_ad", "events_au"} <= triggers
            and {"id", "project", "cwd", "parent", "source", "thread_source", "harness", "source_path", "updated", "indexed_bytes"} <= session_columns
            and {"id", "session", "position", "kind", "text", "timestamp", "call_id", "digest"} <= event_columns
            and (expected_version < 2 or "file_ctime_ns" in session_columns)
            and (expected_version < 3 or "unindexed_sources" in existing)
        )

    def _migrate_v1(self):
        try:
            self.db.executescript(
                "BEGIN IMMEDIATE;"
                "ALTER TABLE sessions ADD COLUMN file_ctime_ns INTEGER;"
                "PRAGMA user_version = 2;COMMIT;"
            )
        except sqlite3.DatabaseError:
            self.db.rollback()
            raise

    def _migrate_v2(self):
        try:
            self.db.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE TABLE unindexed_sources ("
                "id TEXT NOT NULL, project TEXT, cwd TEXT NOT NULL, parent TEXT, source TEXT, thread_source TEXT, "
                "harness TEXT NOT NULL, source_path TEXT PRIMARY KEY, updated REAL NOT NULL, file_size INTEGER NOT NULL, "
                "file_mtime_ns INTEGER NOT NULL, file_ctime_ns INTEGER NOT NULL, file_dev INTEGER NOT NULL, "
                "file_ino INTEGER NOT NULL, header_digest TEXT NOT NULL);"
                "PRAGMA user_version = 3;COMMIT;"
            )
        except sqlite3.DatabaseError:
            self.db.rollback()
            raise

    def _create_schema(self):
        self.db.executescript(
            "BEGIN IMMEDIATE;"
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, project TEXT, cwd TEXT NOT NULL, parent TEXT, source TEXT, "
            "thread_source TEXT, harness TEXT NOT NULL, source_path TEXT NOT NULL, updated REAL NOT NULL, "
            "file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL, file_ctime_ns INTEGER, file_dev INTEGER NOT NULL, "
            "file_ino INTEGER NOT NULL, indexed_bytes INTEGER NOT NULL DEFAULT 0, header_digest TEXT NOT NULL, "
            "checkpoint_position INTEGER, checkpoint_digest TEXT, malformed_lines INTEGER NOT NULL DEFAULT 0, "
            "partial_bytes INTEGER NOT NULL DEFAULT 0, coverage TEXT NOT NULL DEFAULT 'complete');"
            "CREATE TABLE unindexed_sources ("
            "id TEXT NOT NULL, project TEXT, cwd TEXT NOT NULL, parent TEXT, source TEXT, thread_source TEXT, "
            "harness TEXT NOT NULL, source_path TEXT PRIMARY KEY, updated REAL NOT NULL, file_size INTEGER NOT NULL, "
            "file_mtime_ns INTEGER NOT NULL, file_ctime_ns INTEGER NOT NULL, file_dev INTEGER NOT NULL, "
            "file_ino INTEGER NOT NULL, header_digest TEXT NOT NULL);"
            "CREATE INDEX sessions_project_updated ON sessions(project, updated DESC);"
            "CREATE INDEX sessions_parent ON sessions(parent);"
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, "
            "position INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, timestamp TEXT, call_id TEXT, "
            "raw_type TEXT, digest TEXT NOT NULL, UNIQUE(session, position));"
            "CREATE INDEX events_session_position ON events(session, position);"
            "CREATE VIRTUAL TABLE events_fts USING fts5(text, content='events', content_rowid='id');"
            "CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN "
            "INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text); END;"
            "CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN "
            "INSERT INTO events_fts(events_fts, rowid, text) VALUES ('delete', old.id, old.text); END;"
            "CREATE TRIGGER events_au AFTER UPDATE ON events BEGIN "
            "INSERT INTO events_fts(events_fts, rowid, text) VALUES ('delete', old.id, old.text); "
            "INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text); END;"
            "CREATE TABLE index_issues ("
            "session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, position INTEGER NOT NULL, "
            "kind TEXT NOT NULL, PRIMARY KEY(session, position));"
            "PRAGMA user_version = " + str(SCHEMA_VERSION) + ";COMMIT;"
        )

    def refresh(self, roots=None, project=None, projects=None, *, updated_after=None):
        return self._refresh(roots, project, projects, updated_after=updated_after)

    def refresh_headers(self, roots=None):
        return self._refresh(roots, None, set(), headers_only=True)

    def _refresh(self, roots=None, project=None, projects=None, *, updated_after=None, headers_only=False):
        if project is not None and projects is not None:
            raise ValueError("choose either project or projects")
        if roots is None:
            codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
            roots = [codex_home / "sessions", codex_home / "archived_sessions"]
        elif isinstance(roots, (str, os.PathLike)):
            roots = [Path(roots)]
        else:
            roots = [Path(root) for root in roots]
        project = str(project) if project is not None else None
        projects = frozenset(str(value) for value in projects) if projects is not None else None
        if updated_after is not None:
            updated_after = {str(project): float(timestamp) for project, timestamp in updated_after.items()}
            if projects is None or not set(updated_after) <= projects:
                raise ValueError("updated_after requires selected project identifiers")
        else:
            updated_after = {}
        cache = {}
        grouped = {}
        unreadable_paths = set()
        sqlite_changes = self.db.total_changes
        stats = {
            "sessions_seen": 0, "sessions_indexed": 0, "events_added": 0,
            "malformed_lines": 0, "partial_files": 0, "resets": 0,
            "deleted": 0, "skipped_project": 0, "skipped_ephemeral": 0,
            "skipped_learning_workers": 0, "unreadable": 0,
            "stat_operations": 0, "content_opens": 0, "bytes_read": 0,
            "git_processes": 0, "sqlite_changes": 0,
        }
        indexed_by_path = {
            row["source_path"]: dict(row)
            for row in self.db.execute("SELECT * FROM sessions")
        }
        unindexed_by_path = {
            row["source_path"]: dict(row)
            for row in self.db.execute("SELECT * FROM unindexed_sources")
        }
        present_paths = set()
        for root_order, root in enumerate(roots):
            if not root.is_dir():
                continue
            walk_errors = []
            paths = []
            for directory, directories, filenames in os.walk(str(root.resolve()), onerror=walk_errors.append, followlinks=False):
                if ".ephemeral" in Path(directory).parts:
                    directories[:] = []
                    stats["skipped_ephemeral"] += 1
                    continue
                for filename in filenames:
                    if filename.startswith("rollout-") and filename.endswith(".jsonl"):
                        paths.append(Path(directory) / filename)
            for error_path in walk_errors:
                stats["unreadable"] += 1
                unreadable_paths.add(str(Path(error_path.filename or root).absolute()))
            for path in sorted(paths):
                if any(part == ".ephemeral" for part in path.parts):
                    stats["skipped_ephemeral"] += 1
                    continue
                source_path = str(path.absolute())
                try:
                    source_stat = _stat_source(path, stats)
                except OSError:
                    stats["unreadable"] += 1
                    unreadable_paths.add(source_path)
                    continue
                if not stat.S_ISREG(source_stat.st_mode):
                    stats["unreadable"] += 1
                    unreadable_paths.add(source_path)
                    continue
                present_paths.add(source_path)
                existing = indexed_by_path.get(source_path)
                unindexed = unindexed_by_path.get(source_path)
                if self._cached_metadata_matches(existing, source_stat):
                    meta = existing
                elif self._cached_metadata_matches(unindexed, source_stat):
                    meta = unindexed
                else:
                    meta, error = _metadata(path, "codex", cache, stats)
                    if error:
                        stats["unreadable"] += 1
                        unreadable_paths.add(source_path)
                        continue
                if _ephemeral(meta):
                    stats["skipped_ephemeral"] += 1
                    continue
                selected = (project is None or meta["project"] == project) and (
                    projects is None or meta["project"] in projects
                )
                cutoff = updated_after.get(meta["project"])
                if cutoff is not None and meta["updated"] < cutoff:
                    selected = False
                learning_worker = _is_learning_worker(meta)
                if existing is None and (learning_worker or not selected):
                    self._cache_unindexed_metadata(meta, unindexed_by_path)
                if learning_worker:
                    if headers_only:
                        with self.db:
                            self.db.execute("DELETE FROM unindexed_sources WHERE source_path = ?", (source_path,))
                    stats["skipped_learning_workers"] += 1
                    continue
                if headers_only:
                    if not self._cached_metadata_matches(existing, source_stat):
                        self._cache_unindexed_metadata(meta, unindexed_by_path)
                    continue
                grouped.setdefault(meta["id"], []).append((root_order, path, meta))
        candidates = {}
        for session_id, paths in grouped.items():
            ordered = sorted(paths, key=lambda item: (item[0], str(item[1])))
            selected = ordered[0]
            candidates[session_id] = selected[2]
        stats["sessions_seen"] = len(candidates)
        for meta in candidates.values():
            if project is not None and meta["project"] != project or projects is not None and meta["project"] not in projects:
                stats["skipped_project"] += 1
                continue
            cutoff = updated_after.get(meta["project"])
            if cutoff is not None and meta["updated"] < cutoff:
                stats["skipped_project"] += 1
                continue
            changed, added, malformed, partial = self._refresh_file(meta, stats)
            if meta["source_path"] in unindexed_by_path:
                with self.db:
                    self.db.execute("DELETE FROM unindexed_sources WHERE source_path = ?", (meta["source_path"],))
            stats["sessions_indexed"] += 1
            stats["events_added"] += added
            stats["malformed_lines"] += malformed
            stats["partial_files"] += int(partial > 0)
            stats["resets"] += int(changed)
        target_project = project
        scanned_roots = [root.resolve() for root in roots if root.is_dir()]
        rows = self.db.execute("SELECT id, project, source_path FROM sessions").fetchall()
        remove = []
        degraded = []
        for row in rows:
            if target_project is not None and row["project"] != target_project:
                continue
            if projects is not None and row["project"] not in projects:
                continue
            if not any(_under(row["source_path"], root) for root in scanned_roots):
                continue
            if any(row["source_path"] == path or _under(row["source_path"], path) for path in unreadable_paths):
                degraded.append(row["id"])
                continue
            if row["id"] not in candidates or (
                target_project is not None and candidates[row["id"]]["project"] != target_project
            ) or (
                projects is not None and candidates[row["id"]]["project"] not in projects
            ):
                remove.append(row["id"])
        if remove:
            with self.db:
                self.db.executemany("DELETE FROM sessions WHERE id = ?", ((value,) for value in remove))
            stats["deleted"] = len(remove)
        if degraded:
            with self.db:
                self.db.executemany("UPDATE sessions SET coverage = 'degraded' WHERE id = ?", ((value,) for value in degraded))
        stale_headers = [
            row["source_path"]
            for row in self.db.execute("SELECT source_path FROM unindexed_sources")
            if any(_under(row["source_path"], root) for root in scanned_roots)
            and row["source_path"] not in present_paths
            and not any(row["source_path"] == path or _under(row["source_path"], path) for path in unreadable_paths)
        ]
        if stale_headers:
            with self.db:
                self.db.executemany("DELETE FROM unindexed_sources WHERE source_path = ?", ((value,) for value in stale_headers))
        stats["sqlite_changes"] = self.db.total_changes - sqlite_changes
        return stats

    def _cache_unindexed_metadata(self, meta, cached):
        if self._file_metadata_matches(cached.get(meta["source_path"]), meta):
            return
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO unindexed_sources (id, project, cwd, parent, source, thread_source, harness, "
                "source_path, updated, file_size, file_mtime_ns, file_ctime_ns, file_dev, file_ino, header_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (meta["id"], meta["project"], meta["cwd"], meta["parent"], meta["source"], meta["thread_source"],
                 meta["harness"], meta["source_path"], meta["updated"], meta["file_size"], meta["file_mtime_ns"],
                 meta["file_ctime_ns"], meta["file_dev"], meta["file_ino"], meta["header_digest"]),
            )
        cached[meta["source_path"]] = meta

    @staticmethod
    def _cached_metadata_matches(existing, source_stat):
        return Store._file_metadata_matches(existing, {
            "file_size": source_stat.st_size,
            "file_mtime_ns": source_stat.st_mtime_ns,
            "file_ctime_ns": source_stat.st_ctime_ns,
            "file_dev": source_stat.st_dev,
            "file_ino": source_stat.st_ino,
        })

    @staticmethod
    def _file_metadata_matches(existing, meta):
        return existing is not None and existing["file_ctime_ns"] is not None and all(
            existing[key] == meta[key]
            for key in ("file_size", "file_mtime_ns", "file_ctime_ns", "file_dev", "file_ino")
        )

    def _refresh_file(self, meta, stats):
        existing = self.db.execute("SELECT * FROM sessions WHERE id = ?", (meta["id"],)).fetchone()
        reset = existing is not None and (
            existing["source_path"] != meta["source_path"]
            or existing["file_dev"] != meta["file_dev"]
            or existing["file_ino"] != meta["file_ino"]
            or meta["file_size"] < existing["indexed_bytes"]
            or existing["header_digest"] != meta["header_digest"]
            or (meta["file_size"] == existing["file_size"] and (
                meta["file_mtime_ns"] != existing["file_mtime_ns"]
                or existing["file_ctime_ns"] is not None and meta["file_ctime_ns"] != existing["file_ctime_ns"]
            ))
        )
        if (
            existing is not None
            and not reset
            and existing["project"] == meta["project"]
            and self._file_metadata_matches(existing, meta)
            and existing["indexed_bytes"] + existing["partial_bytes"] == meta["file_size"]
        ):
            return False, 0, 0, existing["partial_bytes"]
        if existing is not None and not reset and not self._checkpoint_valid(meta, existing, stats):
            reset = True
        if existing is not None and (reset or existing["project"] != meta["project"]):
            with self.db:
                self.db.execute("DELETE FROM sessions WHERE id = ?", (meta["id"],))
            existing = None
        if (
            existing is not None
            and existing["indexed_bytes"] + existing["partial_bytes"] == meta["file_size"]
            and self._file_metadata_matches(existing, meta)
        ):
            return False, 0, 0, existing["partial_bytes"]
        if existing is None:
            with self.db:
                self.db.execute(
                    "INSERT INTO sessions (id, project, cwd, parent, source, thread_source, harness, source_path, updated, "
                    "file_size, file_mtime_ns, file_ctime_ns, file_dev, file_ino, indexed_bytes, header_digest, coverage) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'degraded')",
                    (meta["id"], meta["project"], meta["cwd"], meta["parent"], meta["source"], meta["thread_source"],
                     meta["harness"], meta["source_path"], meta["updated"], meta["file_size"], meta["file_mtime_ns"],
                     meta["file_ctime_ns"], meta["file_dev"], meta["file_ino"], meta["header_digest"]),
                )
                self.db.execute("DELETE FROM unindexed_sources WHERE source_path = ?", (meta["source_path"],))
            existing = self.db.execute("SELECT * FROM sessions WHERE id = ?", (meta["id"],)).fetchone()
        offset = existing["indexed_bytes"]
        added = 0
        malformed = 0
        checkpoint_position = existing["checkpoint_position"]
        checkpoint_digest = existing["checkpoint_digest"]
        partial = 0
        try:
            with _open_source(meta["source_path"], stats) as stream:
                opened = _fstat_source(stream, stats)
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                    meta["file_dev"], meta["file_ino"], meta["file_size"], meta["file_mtime_ns"], meta["file_ctime_ns"]
                ):
                    raise StoreError("transcript changed while refresh was starting; retry refresh")
                stream.seek(offset)
                with self.db:
                    while True:
                        position = stream.tell()
                        raw = _readline(stream, stats)
                        if not raw:
                            break
                        if not raw.endswith(b"\n"):
                            partial = len(raw)
                            break
                        digest = _sha256(raw)
                        try:
                            obj = _json_line(raw)
                            records = normalize(obj, meta["harness"])
                        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                            self.db.execute(
                                "INSERT OR REPLACE INTO index_issues(session, position, kind) VALUES (?, ?, 'malformed_json')",
                                (meta["id"], position),
                            )
                            malformed += 1
                        else:
                            for record in records:
                                self.db.execute(
                                    "INSERT OR REPLACE INTO events(session, position, kind, text, timestamp, call_id, raw_type, digest) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (meta["id"], position, record["kind"], record["text"], record["timestamp"],
                                     record["call_id"], record["raw_type"], digest),
                                )
                                added += 1
                        offset = stream.tell()
                        checkpoint_position = position
                        checkpoint_digest = digest
                    malformed_total = self.db.execute(
                        "SELECT count(*) FROM index_issues WHERE session = ?", (meta["id"],)
                    ).fetchone()[0]
                    final_stat = _fstat_source(stream, stats)
                    size = final_stat.st_size
                    mtime_ns = final_stat.st_mtime_ns
                    ctime_ns = final_stat.st_ctime_ns
                    updated = final_stat.st_mtime
                    coverage = "degraded" if malformed_total or partial else "complete"
                    self.db.execute(
                        "UPDATE sessions SET updated = ?, file_size = ?, file_mtime_ns = ?, file_ctime_ns = ?, indexed_bytes = ?, "
                        "checkpoint_position = ?, checkpoint_digest = ?, malformed_lines = ?, partial_bytes = ?, coverage = ? "
                        "WHERE id = ?",
                        (updated, size, mtime_ns, ctime_ns, offset, checkpoint_position, checkpoint_digest, malformed_total, partial, coverage, meta["id"]),
                    )
        except OSError as exc:
            raise StoreError("cannot read transcript " + meta["source_path"] + ": " + str(exc)) from exc
        return reset, added, malformed, partial

    def _checkpoint_valid(self, meta, existing, stats):
        position = existing["checkpoint_position"]
        digest = existing["checkpoint_digest"]
        if position is None or digest is None:
            return True
        try:
            with _open_source(meta["source_path"], stats) as stream:
                opened = _fstat_source(stream, stats)
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                    meta["file_dev"], meta["file_ino"], meta["file_size"], meta["file_mtime_ns"], meta["file_ctime_ns"]
                ):
                    return False
                stream.seek(position)
                raw = _readline(stream, stats)
        except OSError:
            return False
        return bool(raw and hmac.compare_digest(_sha256(raw), digest))

    def resolve_evidence(self, session, position, digest):
        row = self.db.execute("SELECT source_path, harness FROM sessions WHERE id = ?", (str(session),)).fetchone()
        if row is None:
            raise StoreError("session is no longer indexed")
        try:
            with _open_source(row["source_path"]) as stream:
                stream.seek(int(position))
                raw = stream.readline()
        except OSError as exc:
            raise StoreError("indexed transcript source is unavailable: " + str(exc)) from exc
        if not raw or not hmac.compare_digest(_sha256(raw), str(digest)):
            raise StoreError("transcript evidence changed since indexing; refresh the session index")
        try:
            obj = _json_line(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise StoreError("transcript evidence is no longer valid JSON") from exc
        records = normalize(obj, row["harness"])
        if len(records) != 1:
            raise StoreError("transcript evidence does not normalize to one event")
        record = dict(records[0])
        record.update({"session": str(session), "position": int(position), "digest": str(digest)})
        return record

    def _filters(self, kinds, since):
        clauses = []
        params = []
        if kinds:
            values = tuple(str(kind) for kind in kinds)
            clauses.append("e.kind IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        if since is not None:
            clauses.append("COALESCE(CAST(strftime('%s', e.timestamp) AS REAL), s.updated) >= ?")
            params.append(float(since))
        return (" AND " + " AND ".join(clauses)) if clauses else "", params

    def search(self, query, project=None, *, limit=20, regex=False, kinds=None, since=None):
        if int(limit) < 1:
            raise ValueError("limit must be positive")
        project = str(project) if project is not None else None
        if regex:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error as exc:
                raise StoreError("invalid regular expression: " + str(exc)) from exc
            sql = "SELECT e.session, e.position, e.digest, e.kind, e.text, e.timestamp, e.call_id, s.project, s.cwd, s.parent, s.source, s.thread_source, s.source_path, s.updated "
            sql += "FROM events e JOIN sessions s ON s.id=e.session WHERE e.text != ''"
            params = []
            if project is not None:
                sql += " AND s.project = ?"
                params.append(project)
            filter_sql, filter_params = self._filters(kinds, since)
            sql += filter_sql
            params.extend(filter_params)
            sql += " ORDER BY s.updated DESC, e.position"
            matches = []
            for row in self.db.execute(sql, params):
                if pattern.search(row["text"]):
                    matches.append(dict(row))
                    if len(matches) >= limit:
                        break
            return matches
        sql = "SELECT e.session, e.position, e.digest, e.kind, e.text, e.timestamp, e.call_id, s.project, s.cwd, s.parent, s.source, s.thread_source, s.source_path, s.updated "
        sql += "FROM events_fts JOIN events e ON e.id=events_fts.rowid JOIN sessions s ON s.id=e.session "
        sql += "WHERE events_fts MATCH ?"
        params = [query]
        if project is not None:
            sql += " AND s.project = ?"
            params.append(project)
        filter_sql, filter_params = self._filters(kinds, since)
        sql += filter_sql
        params.extend(filter_params)
        sql += " ORDER BY bm25(events_fts), s.updated DESC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            return [dict(row) for row in self.db.execute(sql, params)]
        except sqlite3.OperationalError as exc:
            raise StoreError("FTS5 search failed; quote phrases and use AND/OR between terms: " + str(exc)) from exc

    def recent(self, project=None, *, limit=20, kinds=None, since=None):
        if int(limit) < 1:
            raise ValueError("limit must be positive")
        project = str(project) if project is not None else None
        filter_sql, params = self._filters(kinds, since)
        project_sql = " AND s.project = ?" if project is not None else ""
        if project is not None:
            params.insert(0, project)
        sql = (
            "WITH ranked AS (SELECT e.session, e.position, e.digest, e.kind, e.text, e.timestamp, e.call_id, "
            "s.project, s.cwd, s.parent, s.source, s.thread_source, s.source_path, s.updated, "
            "row_number() OVER (PARTITION BY e.session ORDER BY e.position DESC) AS rank "
            "FROM events e JOIN sessions s ON s.id=e.session WHERE 1=1" + project_sql + filter_sql + ") "
            "SELECT session, position, digest, kind, text, timestamp, call_id, project, cwd, parent, source, thread_source, source_path, updated "
            "FROM ranked WHERE rank=1 ORDER BY updated DESC, position DESC LIMIT ?"
        )
        params.append(max(1, int(limit)))
        return [dict(row) for row in self.db.execute(sql, params)]

    def session_events(self, session, project=None, *, position=None, before=3, after=3):
        if before < 0 or after < 0:
            raise ValueError("event window sizes cannot be negative")
        session = str(session)
        project = str(project) if project is not None else None
        session_row = self.db.execute("SELECT project FROM sessions WHERE id = ?", (session,)).fetchone()
        if session_row is None or (project is not None and session_row["project"] != project):
            raise StoreError("session is not indexed in this project scope")
        if position is None:
            target = self.db.execute("SELECT max(position) FROM events WHERE session = ?", (session,)).fetchone()[0]
            if target is None:
                raise StoreError("session has no complete indexed events")
        else:
            target = int(position)
            if self.db.execute("SELECT 1 FROM events WHERE session = ? AND position = ?", (session, target)).fetchone() is None:
                raise StoreError("event byte offset is not indexed for this session")
        columns = (
            "e.session, e.position, e.digest, e.kind, e.text, e.timestamp, e.call_id, "
            "s.project, s.cwd, s.parent, s.source, s.thread_source, s.source_path, s.updated"
        )
        before_rows = self.db.execute(
            "SELECT " + columns + " FROM events e JOIN sessions s ON s.id=e.session "
            "WHERE e.session = ? AND e.position < ? ORDER BY e.position DESC LIMIT ?",
            (session, target, before),
        ).fetchall()
        target_row = self.db.execute(
            "SELECT " + columns + " FROM events e JOIN sessions s ON s.id=e.session "
            "WHERE e.session = ? AND e.position = ?",
            (session, target),
        ).fetchone()
        after_rows = self.db.execute(
            "SELECT " + columns + " FROM events e JOIN sessions s ON s.id=e.session "
            "WHERE e.session = ? AND e.position > ? ORDER BY e.position LIMIT ?",
            (session, target, after),
        ).fetchall()
        return [dict(row) for row in reversed(before_rows)] + [dict(target_row)] + [dict(row) for row in after_rows]


def rebuild_index(state_dir):
    state_dir = _prepare_state_dir(state_dir)
    path = Store.database_path(state_dir)
    if not path.exists():
        Store(state_dir).close()
        return
    try:
        _prepare_database(path)
        db = sqlite3.connect(str(path), timeout=10)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError("refusing to rebuild an index from a newer schema version")
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise StoreError("SQLite integrity check failed; preserve the database and recover it with SQLite before rebuilding")
        db.execute("PRAGMA foreign_keys = OFF")
        with db:
            for name in ("events_ai", "events_ad", "events_au"):
                db.execute("DROP TRIGGER IF EXISTS " + name)
            for name in ("events_fts", "index_issues", "events", "sessions", "unindexed_sources"):
                db.execute("DROP TABLE IF EXISTS " + name)
            db.execute("PRAGMA user_version = 0")
        db.close()
    except sqlite3.DatabaseError as exc:
        raise StoreError("cannot rebuild a corrupt session index without risking learning data: " + str(exc)) from exc
    Store(state_dir).close()
