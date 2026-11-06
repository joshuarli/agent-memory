#!/usr/bin/env python3
"""Per-harness transcript discovery, header reading, and event normalization."""
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Optional, cast
from urllib.parse import unquote

from agent_memory.types import (
    DATABASE_HARNESSES,
    HARNESS_ALIASES,
    HARNESSES,
    OPENCODE_DB,
    OPENCODE_SUBPOSITIONS,
    Harness,
)
HEADER_SCAN_LINES = 200
HEADER_SCAN_BYTES = 524288
MAX_NATIVE_ID = 256
ANTIGRAVITY_DB = "conversation_summaries.db"
_CMD_BOOKKEEPING = frozenset(("model_change", "effort_change", "mode_change", "permission_change", "label", "plan"))
_SKIP_TEXT_KEYS = frozenset((
    "encrypted_content", "internal_chat_message_metadata_passthrough", "id", "call_id",
    "type", "status", "role", "timestamp", "ordinal",
))
_CLAUDE_KNOWN = frozenset((
    "summary", "system", "attachment", "queue-operation", "permission-mode", "mode", "atis-latch", "last-prompt",
    "ai-title", "custom-title", "file-history-snapshot", "file-history-delta", "progress", "api-request",
    "api-request-blob", "pr-link", "agent-name", "tag", "api-request-shape", "cost-state", "tools-used",
))
_PI_SESSION_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d-]+Z_(.+)\.jsonl$")
_PI_BOOKKEEPING = frozenset(("model_change", "thinking_level_change", "usage", "context_edit", "label", "session_info", "custom"))
_MUSE_EXIT_CODE = re.compile(r'"exit_code":\s*(-?\d+)')
_MUSE_ERROR_STATUS = re.compile(r'status="error"')
_MUSE_BOOKKEEPING_PAYLOAD_PREFIXES = ("runtime.session.", "runtime.user_intent.", "session.name.changed")
_MUSE_BOOKKEEPING = frozenset((
    "accepted", "agent_tree_initialized", "async_owner_attempt", "cancelled", "command_intake", "command_invoked",
    "completed", "context_block_diagnostic", "context_block_updated", "context_compaction_candidate",
    "context_compaction_installed", "context_projection_checkpoint", "failed", "goal_handoff_safety",
    "goal_progress_nudge_fired", "goal_run_binding", "goal_usage_attribution", "hook_run_started", "hook_run_terminal",
    "inbox_delivery_anomaly", "inbox_item_drained", "inbox_item_queued", "memory_reminder_child_session_linked",
    "model_completed", "model_failure_terminal_intent", "model_input_trace_recorded", "model_reconfigure",
    "model_response_created", "muse", "output", "provider_request_options_configured", "proposed",
    "reasoning_summary_committed", "reasoning_summary_delta", "reminder_cleanup", "reminder_installed",
    "reminder_proposal", "reminder_reconciler_outcome", "rejected", "resource_pressure", "resource_usage_sampled",
    "retained_runtime_fact", "route_facts", "run_fatal_error_classified", "run_model", "scheduled",
    "security_mode", "session_end", "session_opened", "session_start_intake_v3", "session_started",
    "side_effect_intent", "skill_reminder_decision", "started", "startup_phases", "status", "subagent_control",
    "task_backgrounded", "task_stream_linked", "terminal", "todo_snapshot_updated", "tool_batch_effect",
    "tool_delta", "tool_output_ref", "tool_results_cleared", "user_input_prompt_requested",
    "user_input_prompt_settled", "workflow_child_lifecycle", "workflow_child_result_protocol_recorded",
    "workflow_child_result_submitted", "workflow_launch_reconciled", "workflow_run_launched", "workspace_branch",
))
_EXIT_PATTERNS = (
    re.compile(r"(?im)^\s*exit(?:ed)? (?:code|status)[: ]+(-?\d+)\b"),
    re.compile(r"(?i)\b(?:process )?exited with (?:exit )?code (-?\d+)\b"),
    re.compile(r"(?im)^exit: (-?\d+)\s*$"),
)


def canonical_harness(value) -> Harness:
    text = str(value).strip().lower()
    if text in HARNESS_ALIASES:
        return HARNESS_ALIASES[text]
    if text in HARNESSES:
        return cast(Harness, text)
    raise ValueError("unsupported harness: " + str(value))


def qualified_id(harness: Harness, native_id: str) -> str:
    if harness == "codex" and not _has_harness_prefix(native_id):
        return native_id
    return harness + ":" + native_id


def _has_harness_prefix(value):
    prefix, separator, _ = str(value).partition(":")
    return bool(separator) and prefix in HARNESSES


def clean_native_id(value, path):
    text = str(value)
    if not text or len(text) > MAX_NATIVE_ID or any(ord(char) < 32 for char in text):
        return "path-" + hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:24]
    return text


def default_roots(environ: Optional[Mapping[str, str]] = None) -> dict[Harness, list[Path]]:
    environ = os.environ if environ is None else environ
    home = Path(environ.get("HOME") or str(Path.home()))
    codex_home = Path(environ.get("CODEX_HOME") or str(home / ".codex")).expanduser()
    claude_home = Path(environ.get("CLAUDE_CONFIG_DIR") or str(home / ".claude")).expanduser()
    data_home = Path(environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")).expanduser()
    grok_home = Path(environ.get("GROK_HOME") or str(home / ".grok")).expanduser()
    pi_agent_home = Path(environ.get("PI_CODING_AGENT_DIR") or str(home / ".pi" / "agent")).expanduser()
    pi_sessions = Path(environ.get("PI_CODING_AGENT_SESSION_DIR") or str(pi_agent_home / "sessions")).expanduser()
    commandcode_home = Path(environ.get("COMMANDCODE_HOME") or str(home / ".commandcode")).expanduser()
    antigravity_home = Path(environ.get("ANTIGRAVITY_HOME") or str(home / ".gemini" / "antigravity-cli")).expanduser()
    return {
        "codex": [codex_home / "sessions", codex_home / "archived_sessions"],
        "claude": [claude_home / "projects"],
        "muse": [data_home / "muse" / "sessions"],
        "grok": [grok_home / "sessions"],
        "opencode": [data_home / "opencode"],
        "pi": [pi_sessions],
        "cmd": [commandcode_home / "projects"],
        "agy": [antigravity_home / "brain"],
    }


def coerce_roots(roots: Optional[dict[str, Any]] | list[str | os.PathLike[str]] | str | os.PathLike[str]) -> dict[Harness, list[Path]]:
    if roots is None:
        return default_roots()
    if isinstance(roots, dict):
        mapped: dict[Harness, list[Path]] = {}
        for name, value in roots.items():
            try:
                harness = canonical_harness(name)
            except ValueError as exc:
                raise ValueError(str(exc)) from None
            mapped.setdefault(harness, []).extend(Path(root) for root in _listed(value))
        return mapped
    return {"codex": [Path(root) for root in _listed(roots)]}


def _listed(value):
    return [value] if isinstance(value, (str, os.PathLike)) else list(value)


def opencode_database(root):
    root = Path(root)
    candidate = root if root.name.endswith(".db") else root / OPENCODE_DB
    return candidate if candidate.is_file() and not candidate.is_symlink() else None


def is_source_name(harness: Harness, name: str) -> bool:
    if harness in DATABASE_HARNESSES:
        return False
    if harness == "codex":
        return name.startswith("rollout-") and name.endswith(".jsonl")
    if harness == "claude":
        return name.endswith(".jsonl")
    if harness == "muse":
        return name == "session.jsonl"
    if harness == "pi":
        return _PI_SESSION_NAME.match(name) is not None
    if harness == "cmd":
        return name.endswith(".jsonl") and not name.endswith(".checkpoints.jsonl")
    if harness == "agy":
        return name in ("transcript.jsonl", "transcript_full.jsonl")
    return name == "chat_history.jsonl"


def prefers_sibling(harness: Harness, path) -> bool:
    """True when a better representation of the same native session exists beside this one."""
    if harness != "agy" or Path(path).name != "transcript.jsonl":
        return False
    sibling = Path(path).parent / "transcript_full.jsonl"
    try:
        return stat_is_regular(sibling)
    except OSError:
        return False


def stat_is_regular(path):
    return stat.S_ISREG(os.lstat(path).st_mode)


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
        if block_type in ("image", "input_image", "audio", "input_audio", "encrypted_content", "thinking", "redacted_thinking"):
            return ""
        if "text" in value:
            return _text_value(value.get("text"))
        if "content" in value:
            return _content_text(value.get("content"))
    return ""


def exit_outcome(text):
    for pattern in _EXIT_PATTERNS:
        match = pattern.search(text[:4000])
        if match:
            return "ok" if int(match.group(1)) == 0 else "error"
    return None


def _record(kind, text="", timestamp=None, call_id=None, raw_type=None, outcome=None, **fields) -> dict[str, Any]:
    record = {
        "kind": str(kind),
        "text": text if isinstance(text, str) else _text_value(text),
        "timestamp": str(timestamp) if timestamp is not None else None,
        "call_id": str(call_id) if call_id is not None else None,
        "raw_type": str(raw_type) if raw_type is not None else None,
        "outcome": outcome,
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
            text = output if isinstance(output, str) else _content_text(output) or _text_value(output)
            return _record("tool-output", text, timestamp, payload.get("call_id"), top_type, outcome=exit_outcome(text))
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


def _claude_blocks(content):
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _claude_record(obj):
    kind = obj.get("type") or "unknown"
    timestamp = obj.get("timestamp")
    message = obj.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content", obj.get("content", ""))
    blocks = _claude_blocks(content)
    if kind == "user":
        results = [block for block in blocks if block.get("type") == "tool_result"]
        if results:
            text = "\n".join(part for part in (_content_text(block.get("content", "")) for block in results) if part)
            failed = any(block.get("is_error") for block in results)
            call_id = ",".join(str(block["tool_use_id"]) for block in results if block.get("tool_use_id"))
            return _record("tool-output", text, timestamp, call_id or None, kind, outcome="error" if failed else "ok", role="user")
        text = _content_text(content)
        injected = obj.get("isMeta") or obj.get("sourceToolUseID") or obj.get("isCompactSummary")
        return _record("context" if injected else "user", text, timestamp, raw_type=kind, role="user")
    if kind == "assistant":
        calls = [block for block in blocks if block.get("type") == "tool_use"]
        text = "\n".join(part for part in (_content_text(block) for block in blocks if block.get("type") == "text") if part)
        if calls:
            rendered = "\n".join(str(call.get("name") or "tool") + "\n" + _text_value(call.get("input", {})) for call in calls)
            call_id = ",".join(str(call["id"]) for call in calls if call.get("id"))
            if text:
                return _record("assistant", text + "\n" + rendered, timestamp, call_id or None, kind, role="assistant")
            return _record("tool-call", rendered, timestamp, call_id or None, kind, tool_name=str(calls[0].get("name") or "tool"))
        if text:
            return _record("assistant", text, timestamp, raw_type=kind, role="assistant")
        return _record("reasoning" if any(block.get("type") in ("thinking", "redacted_thinking") for block in blocks) else "assistant", "", timestamp, raw_type=kind)
    if kind in _CLAUDE_KNOWN:
        return _record(str(kind), "", timestamp, raw_type=kind)
    return _record("unknown:" + str(kind), "", timestamp, raw_type=kind)


def _pi_message_record(message, timestamp):
    role = message.get("role")
    content = message.get("content", "")
    if role == "user":
        return _record("user", _content_text(content), timestamp, raw_type="message", role="user")
    if role == "system":
        return _record("system", "", timestamp, raw_type="message", role="system")
    if role == "custom":
        return _record("context", _content_text(content), timestamp, raw_type="message")
    if role == "toolResult":
        text = _content_text(content)
        failed = message.get("isError")
        outcome = ("error" if failed else "ok") if isinstance(failed, bool) else exit_outcome(text)
        return _record("tool-output", text, timestamp, message.get("toolCallId"), "message", outcome=outcome)
    if role == "assistant":
        blocks = _claude_blocks(content)
        calls = [block for block in blocks if block.get("type") == "toolCall"]
        text = "\n".join(part for part in (_content_text(block) for block in blocks if block.get("type") == "text") if part)
        if calls:
            rendered = "\n".join(str(call.get("name") or "tool") + "\n" + _text_value(call.get("arguments", {})) for call in calls)
            call_id = ",".join(str(call["id"]) for call in calls if call.get("id"))
            if text:
                return _record("assistant", text + "\n" + rendered, timestamp, call_id or None, "message", role="assistant")
            return _record("tool-call", rendered, timestamp, call_id or None, "message", tool_name=str(calls[0].get("name") or "tool"))
        if text:
            return _record("assistant", text, timestamp, raw_type="message", role="assistant")
        if message.get("errorMessage"):
            return _record("error", _text_value(message["errorMessage"]), timestamp, raw_type="message")
        return _record("reasoning" if any(block.get("type") == "thinking" for block in blocks) else "assistant", "", timestamp, raw_type="message")
    return _record("unknown:message:" + str(role), "", timestamp, raw_type="message")


def _pi_record(obj):
    record = _pi_entry_record(obj)
    if obj.get("type") != "session":
        record.update(entry=_pi_entry_id(obj.get("id")), parent_entry=_pi_entry_id(obj.get("parentId")))
    return record


def _pi_entry_id(value):
    return value if isinstance(value, str) else None


def _pi_entry_record(obj):
    kind = obj.get("type") or "unknown"
    timestamp = obj.get("timestamp")
    if kind == "session":
        return _record("session_meta", "", timestamp, raw_type=kind)
    if kind == "message":
        message = obj.get("message")
        return _pi_message_record(message if isinstance(message, dict) else {}, timestamp)
    if kind == "compaction":
        return _record("compacted", _text_value(obj.get("summary", "")), timestamp, raw_type=kind)
    if kind == "branch_summary":
        return _record("branch_summary", _text_value(obj.get("summary", "")), timestamp, raw_type=kind)
    if kind == "custom_message":
        return _record("context", _content_text(obj.get("content", "")), timestamp, raw_type=kind)
    if kind in _PI_BOOKKEEPING:
        return _record(str(kind), "", timestamp, raw_type=kind)
    return _record("unknown:" + str(kind), "", timestamp, raw_type=kind)


def _muse_record(obj):
    payload = obj.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    event = payload.get("event")
    event = event if isinstance(event, dict) else {}
    timestamp = obj.get("recorded_at")
    if isinstance(timestamp, (int, float)):
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp / 1_000_000))
    if payload.get("kind") == "metadata" and not event:
        return _record("session_meta", "", timestamp, raw_type="metadata")
    event_type = event.get("kind") or payload.get("kind")
    if not event_type:
        envelope = next((obj[key] for key in ("retained_marker", "retained_frame") if isinstance(obj.get(key), str)), None)
        payload_type = obj.get("payload_type")
        if envelope is None and isinstance(payload_type, str) and payload_type.startswith(_MUSE_BOOKKEEPING_PAYLOAD_PREFIXES):
            envelope = payload_type
        if envelope is not None:
            return _record(envelope, "", timestamp, raw_type=envelope)
        return _record("unknown:muse", "", timestamp, raw_type=payload.get("kind", "event"))
    if event_type == "model_user_messages":
        return _record("user", _content_text(event.get("messages", [])), timestamp, raw_type=event_type, role="user")
    if event_type == "skill_read_observed":
        return _record(str(event_type), _text_value(event), timestamp, raw_type=event_type)
    if event_type == "assistant_message_committed":
        return _record("assistant", _text_value(event.get("text")), timestamp, raw_type=event_type, role="assistant")
    if event_type == "assistant_tool_calls_committed":
        calls = [call for call in event.get("tool_calls", []) or [] if isinstance(call, dict)]
        rendered = "\n".join(str(call.get("name") or "tool") + "\n" + _muse_arguments(call.get("args")) for call in calls)
        call_id = ",".join(str(call["call_id"]) for call in calls if call.get("call_id"))
        return _record("tool-call", rendered, timestamp, call_id or None, event_type, tool_name=str(calls[0].get("name") or "tool") if calls else "tool")
    if event_type == "tool_result_batch_committed":
        results = [result for result in event.get("results", []) or [] if isinstance(result, dict)]
        texts = [_text_value(result.get("text")) for result in results]
        call_id = ",".join(str(result["tool_call_id"]) for result in results if result.get("tool_call_id"))
        return _record("tool-output", "\n".join(part for part in texts if part), timestamp, call_id or None, event_type, outcome=_muse_outcome(texts))
    if event_type == "reasoning_committed":
        return _record("reasoning", "", timestamp, raw_type=event_type)
    if event_type == "model_request_configured" or event_type in _MUSE_BOOKKEEPING:
        return _record(str(event_type), "", timestamp, raw_type=event_type)
    return _record("unknown:" + str(event_type), "", timestamp, raw_type=event_type)


def _muse_arguments(args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args
    return _text_value(args)


def _muse_outcome(texts):
    codes = [int(match.group(1)) for match in (_MUSE_EXIT_CODE.search(text[:4000]) for text in texts) if match]
    if any(code != 0 for code in codes) or any(_MUSE_ERROR_STATUS.search(text[:400]) for text in texts):
        return "error"
    return "ok" if codes else None


def _cmd_blocks(content):
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _cmd_record(obj):
    kind = obj.get("type") or "unknown"
    timestamp = obj.get("timestamp")
    entry = obj.get("id") if isinstance(obj.get("id"), str) else None
    parent_entry = obj.get("parentId") if isinstance(obj.get("parentId"), str) else None
    message = obj.get("message")
    message = message if isinstance(message, dict) else {}
    if kind == "session":
        return _record("session_meta", "", timestamp, raw_type=kind)
    if kind != "message":
        if kind in _CMD_BOOKKEEPING:
            return _record(str(kind), "", timestamp, raw_type=kind)
        return _record("unknown:" + str(kind), "", timestamp, raw_type=kind)
    role = message.get("role")
    content = message.get("content")
    blocks = _cmd_blocks(content)
    raw_meta = message.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    injected = bool(meta.get("injected")) or bool(meta.get("isAutomated"))
    results = [block for block in blocks if block.get("type") == "tool_result"]
    if results:
        text = "\n".join(part for part in (_content_text(block.get("content", "")) for block in results) if part)
        call_id = ",".join(str(block["tool_use_id"]) for block in results if block.get("tool_use_id"))
        return _record("tool-output", text, timestamp, call_id or None, kind, outcome=exit_outcome(text),
                       entry=entry, parent_entry=parent_entry, role="user")
    calls = [block for block in blocks if block.get("type") == "tool_use"]
    text = "\n".join(part for part in (_content_text(block) for block in blocks if block.get("type") == "text") if part)
    if calls:
        rendered = "\n".join(str(call.get("name") or "tool") + "\n" + _text_value(call.get("input", {})) for call in calls)
        call_id = ",".join(str(call["id"]) for call in calls if call.get("id"))
        if text:
            return _record("assistant", text + "\n" + rendered, timestamp, call_id or None, kind,
                           entry=entry, parent_entry=parent_entry, role="assistant")
        return _record("tool-call", rendered, timestamp, call_id or None, kind,
                       entry=entry, parent_entry=parent_entry, tool_name=str(calls[0].get("name") or "tool"))
    if text:
        normal = "assistant" if role == "assistant" else "context" if injected else "user"
        return _record(normal, text, timestamp, raw_type=kind, entry=entry, parent_entry=parent_entry, role=role)
    if any(block.get("type") == "thinking" for block in blocks):
        return _record("reasoning", "", timestamp, raw_type=kind, entry=entry, parent_entry=parent_entry)
    return _record("context" if injected else ("assistant" if role == "assistant" else "user"), "",
                   timestamp, raw_type=kind, entry=entry, parent_entry=parent_entry)


def _agy_record(obj):
    kind = obj.get("type") or "unknown"
    timestamp = obj.get("created_at")
    source = obj.get("source")
    status = obj.get("status")
    step = obj.get("step_index")
    truncated = bool(obj.get("truncated_fields"))
    text = _content_text(obj.get("content", ""))
    if kind == "USER_INPUT":
        return _record("user", text, timestamp, raw_type=kind, role="user", truncated=truncated)
    if kind == "PLANNER_RESPONSE":
        calls = [call for call in obj.get("tool_calls") or [] if isinstance(call, dict)]
        if calls:
            rendered = "\n".join(str(call.get("name") or "tool") + "\n" + _text_value(call.get("args", {})) for call in calls)
            call_id = ",".join("%s:%d" % (step, index) for index in range(len(calls)))
            if text:
                return _record("assistant", text + "\n" + rendered, timestamp, call_id, kind,
                               role="assistant", synthetic_id=True, truncated=truncated)
            return _record("tool-call", rendered, timestamp, call_id, kind,
                           tool_name=str(calls[0].get("name") or "tool"), synthetic_id=True, truncated=truncated)
        return _record("assistant", text, timestamp, raw_type=kind, role="assistant", truncated=truncated)
    if kind == "CHECKPOINT":
        return _record("compacted", text, timestamp, raw_type=kind, truncated=truncated)
    if kind == "SYSTEM_MESSAGE":
        return _record("context", text, timestamp, raw_type=kind, truncated=truncated)
    if kind == "GENERIC":
        if source == "SYSTEM":
            return _record("context", text, timestamp, raw_type=kind, truncated=truncated)
        outcome = {"ERROR": "error", "DONE": "ok"}.get(str(status)) if status else None
        return _record("tool-output", text, timestamp, raw_type=kind, outcome=outcome, truncated=truncated)
    return _record("unknown:" + str(kind), text, timestamp, raw_type=kind, truncated=truncated)


def _grok_record(obj):
    kind = obj.get("type") or "unknown"
    timestamp = obj.get("timestamp")
    if kind == "user":
        text = _content_text(obj.get("content", ""))
        return _record("context" if obj.get("synthetic_reason") else "user", text, timestamp, raw_type=kind, role="user")
    if kind == "assistant":
        text = _content_text(obj.get("content", ""))
        calls = [call for call in obj.get("tool_calls", []) or [] if isinstance(call, dict)]
        rendered = []
        for call in calls:
            args = call.get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            rendered.append(str(call.get("name") or "tool") + "\n" + _text_value(args))
        if rendered:
            call_id = ",".join(str(call["id"]) for call in calls if call.get("id"))
            if text:
                return _record("assistant", text + "\n" + "\n".join(rendered), timestamp, call_id or None, kind, role="assistant")
            return _record("tool-call", "\n".join(rendered), timestamp, call_id or None, kind, tool_name=str(calls[0].get("name") or "tool"))
        return _record("assistant", text, timestamp, raw_type=kind, role="assistant")
    if kind == "tool_result":
        text = _content_text(obj.get("content", ""))
        return _record("tool-output", text, timestamp, obj.get("tool_call_id"), kind, outcome=exit_outcome(text))
    if kind in ("reasoning", "system", "backend_tool_call"):
        return _record(str(kind), "", timestamp, raw_type=kind)
    return _record("unknown:" + str(kind), "", timestamp, raw_type=kind)


def _millis(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value / 1000))
    return None


def _tool_outcome(block):
    raw_state = block.get("state")
    state = raw_state if isinstance(raw_state, dict) else {}
    status = state.get("status")
    raw_metadata = state.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    exit_code = metadata.get("exit")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return "ok" if exit_code == 0 else "error"
    if status == "error":
        return "error"
    return "ok" if status == "completed" else None


def opencode_events(message_type, data):
    if not isinstance(data, dict):
        return [(0, _record("unknown:non_object", "", raw_type=message_type))]
    created = (data.get("time") or {}).get("created") if isinstance(data.get("time"), dict) else None
    stamp = _millis(created)
    if message_type == "user":
        return [(0, _record("user", _text_value(data.get("text", "")), stamp, raw_type="user", role="user"))]
    if message_type in ("system", "synthetic"):
        return [(0, _record("context", _text_value(data.get("text", "")), stamp, raw_type=message_type))]
    if message_type == "compaction":
        return [(0, _record("compacted", _text_value(data.get("summary", "")), stamp, raw_type=message_type))]
    if message_type == "idle":
        return [(0, _record("idle", "", stamp, raw_type=message_type))]
    if message_type != "assistant":
        return [(0, _record("unknown:" + str(message_type), "", stamp, raw_type=message_type))]
    events = []
    blocks = data.get("content") if isinstance(data.get("content"), list) else []
    for index, block in enumerate(blocks[: OPENCODE_SUBPOSITIONS // 2]):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            events.append((index * 2, _record("assistant", _text_value(block.get("text", "")), stamp, raw_type="assistant", role="assistant")))
        elif kind == "tool":
            state = block.get("state") if isinstance(block.get("state"), dict) else {}
            name = str(block.get("name") or "tool")
            arguments = _text_value(state.get("input", {}))
            call_id = block.get("id")
            events.append((index * 2, _record("tool-call", name + ("\n" + arguments if arguments else ""), stamp, call_id, "assistant", tool_name=name)))
            if state.get("status") in ("completed", "error"):
                output = _content_text(state.get("content", []))
                if not output and state.get("error"):
                    output = _text_value(state.get("error"))
                events.append((index * 2 + 1, _record("tool-output", output, stamp, call_id, "assistant", outcome=_tool_outcome(block))))
        elif kind == "reasoning":
            continue
        else:
            events.append((index * 2, _record("unknown:" + str(kind), "", stamp, raw_type="assistant")))
    return events or [(0, _record("assistant", "", stamp, raw_type="assistant"))]


def opencode_record(message_type, data_text, subposition):
    try:
        data = json.loads(data_text)
    except ValueError:
        return None
    for index, record in opencode_events(message_type, data):
        if index == subposition:
            return record
    return None


def normalize(obj: dict[str, Any], harness: Harness = "codex") -> list[dict[str, Any]]:
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
    if harness == "pi":
        return [_pi_record(obj)]
    if harness == "cmd":
        return [_cmd_record(obj)]
    if harness == "agy":
        return [_agy_record(obj)]
    if harness in DATABASE_HARNESSES:
        raise ValueError("database transcripts are read with opencode_events, not normalize")
    raise ValueError("unsupported transcript harness: " + str(harness))


def _json_object(raw) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _head_lines(path, open_source, stats, wanted):
    lines, used = [], 0
    with open_source(path, stats) as stream:
        info = os.fstat(stream.fileno())
        while len(lines) < wanted and used < HEADER_SCAN_BYTES:
            raw = stream.readline()
            if not raw:
                break
            used += len(raw)
            if stats is not None:
                stats["bytes_read"] += len(raw)
            lines.append(raw)
    return lines, info


def _sibling_json(path, name, open_source, stats, limit=65536):
    try:
        with open_source(Path(path).parent / name, stats) as stream:
            data = stream.read(limit)
        if stats is not None:
            stats["bytes_read"] += len(data)
        return _json_object(data)
    except OSError:
        return None


def _grok_primary(path, open_source, stats):
    try:
        with open_source(Path(path).parent / "events.jsonl", stats) as stream:
            for _ in range(HEADER_SCAN_LINES):
                raw = stream.readline()
                if not raw:
                    return False
                if stats is not None:
                    stats["bytes_read"] += len(raw)
                obj = _json_object(raw)
                if obj and obj.get("type") == "turn_started":
                    return obj.get("session_relationship") == "primary"
    except OSError:
        return False
    return False


def _json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if value is not None else None
    except (TypeError, ValueError):
        return None


def _cmd_header(path, objects, header, open_source):
    first = objects[0] if objects and objects[0].get("type") == "session" else {}
    native = first.get("id") or path.stem
    header["native_id"] = str(native)
    header["cwd"] = first.get("cwd") if isinstance(first.get("cwd"), str) else ""
    meta = _sibling_json(path, path.stem + ".meta.json", open_source, None)
    meta = meta if isinstance(meta, dict) else {}
    branch_point = meta.get("branchPoint")
    header["branch_tip"] = str(branch_point) if isinstance(branch_point, str) else None
    parent = meta.get("parentSessionId")
    if not isinstance(parent, str) and isinstance(first.get("parentSession"), str):
        raw = Path(first["parentSession"]).name
        parent = raw[:-len(".jsonl")] if raw.endswith(".jsonl") else raw
    header["parent_native"] = str(parent) if parent else None
    header["thread_source"] = "child" if parent else "user"
    header["source"] = _json_text({"subagent": {"parent": parent}} if parent else "user")
    return header


def _antigravity_row(path):
    conversation = path.parents[2].name
    database = path.parents[4] / ANTIGRAVITY_DB
    if not database.is_file() or database.is_symlink():
        return conversation, None
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % database.as_posix(), uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            return conversation, connection.execute(
                "SELECT workspace_uris, parent_conversation_id, nesting_depth, step_count FROM conversation_summaries WHERE conversation_id = ?",
                (conversation,)).fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return conversation, None


def _antigravity_header(path, header):
    conversation, row = _antigravity_row(path)
    header["native_id"] = conversation
    header["ancestry"] = "known"
    workspaces = []
    if row is not None:
        try:
            raw = json.loads(row["workspace_uris"] or "[]")
        except (TypeError, ValueError):
            raw = []
        if isinstance(raw, list):
            for value in raw:
                if not isinstance(value, str):
                    continue
                text = unquote(value[7:]) if value.startswith("file://") else value
                if text.startswith("/") and text not in workspaces:
                    workspaces.append(text)
        parent = row["parent_conversation_id"]
        header["parent_native"] = str(parent) if parent else None
        header["step_count"] = int(row["step_count"] or 0)
        header["thread_source"] = "subagent" if parent else "user"
        header["source"] = _json_text({"subagent": {"parent": parent}} if parent else "user")
    else:
        header["thread_source"] = None
        header["source"] = None
        header["ancestry"] = "unknown"
        header["step_count"] = None
    header["workspaces"] = workspaces
    header["cwd"] = workspaces[0] if len(workspaces) == 1 else ""
    return header


def read_header(harness, path, open_source, stats=None):
    lines, info = _head_lines(path, open_source, stats, HEADER_SCAN_LINES if harness in ("claude", "muse") else 1)
    first = lines[0] if lines else b""
    header = {
        "native_id": path.stem, "cwd": "", "parent": None, "source": None, "thread_source": None,
        "ancestry": "known", "first": first, "file_stat": info, "parent_native": None,
        "branch_tip": None, "workspaces": [], "step_count": None,
    }
    objects = [obj for obj in (_json_object(raw) for raw in lines) if obj is not None]
    if harness == "codex":
        obj = objects[0] if objects and _json_object(first) is not None else {}
        raw_payload = obj.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        header["native_id"] = str(payload.get("id") or payload.get("session_id") or path.stem)
        header["cwd"] = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
        source = payload.get("source")
        thread_source = payload.get("thread_source")
        if source is None:
            source = thread_source
        header["source"] = _json_text(source)
        header["thread_source"] = str(thread_source) if thread_source is not None else None
        parent = payload.get("parent_thread_id")
        if parent is None and isinstance(source, dict):
            raw_spawned = source.get("subagent")
            spawned = raw_spawned if isinstance(raw_spawned, dict) else None
            raw_thread_spawn = spawned.get("thread_spawn") if isinstance(spawned, dict) else None
            thread_spawn = raw_thread_spawn if isinstance(raw_thread_spawn, dict) else None
            parent = thread_spawn.get("parent_thread_id") if isinstance(thread_spawn, dict) else None
        header["parent_native"] = str(parent) if parent is not None else None
        return header
    if harness == "claude":
        sidechain = False
        session = None
        for obj in objects:
            session = session or (obj.get("sessionId") if isinstance(obj.get("sessionId"), str) else None)
            if isinstance(obj.get("cwd"), str) and obj["cwd"]:
                header["cwd"] = obj["cwd"]
                sidechain = bool(obj.get("isSidechain"))
                break
        sidechain = sidechain or "subagents" in path.parts
        if sidechain:
            header["native_id"] = path.stem
            header["parent_native"] = session if session and session != path.stem else None
            header["source"] = _json_text({"subagent": {"sidechain": True}})
            header["thread_source"] = "subagent"
        else:
            header["native_id"] = session or path.stem
            header["thread_source"] = "user"
            header["source"] = _json_text("user")
        return header
    if harness == "muse":
        header["native_id"] = path.parent.name
        header["ancestry"] = "unknown"
        for obj in objects:
            raw_payload = obj.get("payload")
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            raw_record = payload.get("record")
            record = raw_record if isinstance(raw_record, dict) else {}
            workspace_root = record.get("workspace_root")
            if payload.get("kind") == "metadata" and isinstance(workspace_root, str):
                header["cwd"] = workspace_root
                break
        return header
    if harness == "pi":
        header["native_id"] = _PI_SESSION_NAME.sub(r"\1", path.name)
        obj = objects[0] if objects and objects[0].get("type") == "session" else {}
        if isinstance(obj.get("id"), str):
            header["native_id"] = obj["id"]
        header["cwd"] = obj.get("cwd") if isinstance(obj.get("cwd"), str) else ""
        parent_session = obj.get("parentSession")
        match = _PI_SESSION_NAME.match(Path(parent_session).name) if isinstance(parent_session, str) else None
        if match:
            header["parent_native"] = match.group(1)
            header["thread_source"] = None
            header["source"] = _json_text("child")
        else:
            header["thread_source"] = "user"
            header["source"] = _json_text("user")
        return header
    if harness == "cmd":
        return _cmd_header(path, objects, header, open_source)
    if harness == "agy":
        return _antigravity_header(path, header)
    header["native_id"] = path.parent.name
    summary = _sibling_json(path, "summary.json", open_source, stats)
    raw_info = summary.get("info") if isinstance(summary, dict) else None
    info_block = raw_info if isinstance(raw_info, dict) else {}
    cwd = info_block.get("cwd")
    if isinstance(cwd, str):
        header["cwd"] = cwd
    else:
        header["cwd"] = unquote(path.parent.parent.name)
    if not header["cwd"].startswith("/"):
        header["cwd"] = ""
    header["ancestry"] = "known" if _grok_primary(path, open_source, stats) else "unknown"
    return header
