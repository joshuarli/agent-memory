#!/usr/bin/env python3
"""Offline retrieval benchmark for agent-learn recall over synthetic multi-harness episodes."""
import argparse
import datetime
import hashlib
import importlib
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = ROOT / "dotfiles" / "base" / ".local" / "bin"
SRC_DIR = ROOT / "agent-memory" / "src"
FIXTURES = ROOT / "dotfiles" / "recall-eval"
WORK_TMP = ROOT / ".work" / "tmp"
SPLITS = ("dev", "heldout")
HARNESSES = ("codex", "claude", "muse", "grok")
CATEGORIES = (
    "exact_identifier", "paraphrase", "path_symbol_command", "short_identifier", "trajectory",
    "nonobvious_success", "contradiction", "stale_source", "valid_after_unrelated_commit",
    "worktree", "similar_project", "id_collision", "malformed", "negative", "injection", "multi_record",
    "duplicate", "temporal", "version", "provenance",
)
STEP_KEYS = {"user", "assistant", "tool"}
sys.path.insert(0, str(SRC_DIR))


def _line(value):
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _micro(epoch):
    return int(epoch * 1_000_000)


def native(episode):
    return episode.get("native_id", episode["id"])


def _tool_text(tool):
    exit_code = tool.get("exit")
    output = tool.get("output", "")
    return output, exit_code


def render_codex(episode, cwd, start):
    session = native(episode)
    meta = {"id": session, "cwd": str(cwd), "source": "cli", "thread_source": "user", "timestamp": _iso(start)}
    parent = episode.get("subagent_of")
    if parent:
        meta.update({"parent_thread_id": parent, "thread_source": "subagent",
                     "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}})
    lines = [{"type": "session_meta", "payload": meta}]
    when = start
    for number, step in enumerate(episode["turns"]):
        when += 30
        stamp = _iso(when)
        if "user" in step:
            lines.append({"type": "response_item", "timestamp": stamp, "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": step["user"]}]}})
        elif "assistant" in step:
            lines.append({"type": "response_item", "timestamp": stamp, "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": step["assistant"]}]}})
        else:
            tool = step["tool"]
            call_id = "call-%s-%d" % (session, number)
            output, exit_code = _tool_text(tool)
            lines.append({"type": "response_item", "timestamp": stamp, "payload": {
                "type": "function_call", "name": "shell", "call_id": call_id,
                "arguments": json.dumps({"command": tool["cmd"]})}})
            body = ("Exit code: %d\n" % exit_code if exit_code is not None else "") + "Output:\n" + output
            lines.append({"type": "response_item", "timestamp": stamp, "payload": {
                "type": "function_call_output", "call_id": call_id, "output": body}})
    return [_line(obj) for obj in lines]


def render_claude(episode, cwd, start):
    session = episode.get("subagent_of") or native(episode)
    base = {"sessionId": session, "cwd": str(cwd), "isSidechain": bool(episode.get("subagent_of")), "version": "2.1.0"}
    lines = [{"type": "queue-operation", "operation": "enqueue", "sessionId": session, "timestamp": _iso(start)}]
    when = start
    for number, step in enumerate(episode["turns"]):
        when += 30
        uuid = "u-%s-%d" % (session, number)
        common = dict(base, timestamp=_iso(when), uuid=uuid)
        if "user" in step:
            lines.append(dict(common, type="user", message={"role": "user", "content": step["user"]}))
        elif "assistant" in step:
            lines.append(dict(common, type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": step["assistant"]}]}))
        else:
            tool = step["tool"]
            call_id = "toolu_%s_%d" % (session, number)
            output, exit_code = _tool_text(tool)
            lines.append(dict(common, type="assistant", message={"role": "assistant", "content": [
                {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": tool["cmd"], "description": "run"}}]}))
            block = {"type": "tool_result", "tool_use_id": call_id, "content": output}
            if exit_code:
                block["is_error"] = True
                block["content"] = "Exit code %d\n%s" % (exit_code, output)
            lines.append(dict(common, type="user", uuid=uuid + "r", message={"role": "user", "content": [block]}))
    return [_line(obj) for obj in lines]


def render_grok(episode, cwd, start):
    lines = []
    when = start
    for number, step in enumerate(episode["turns"]):
        when += 30
        stamp = _iso(when)
        if "user" in step:
            lines.append({"type": "user", "timestamp": stamp, "content": [{"type": "text", "text": step["user"]}]})
        elif "assistant" in step:
            lines.append({"type": "assistant", "timestamp": stamp, "content": step["assistant"], "model_id": "grok-4"})
        else:
            tool = step["tool"]
            call_id = "call_%s_%d" % (native(episode), number)
            output, exit_code = _tool_text(tool)
            lines.append({"type": "assistant", "timestamp": stamp, "content": "", "tool_calls": [
                {"id": call_id, "name": "run_terminal_command", "arguments": json.dumps({"command": tool["cmd"]})}]})
            lines.append({"type": "tool_result", "timestamp": stamp, "tool_call_id": call_id,
                          "content": ("exit: %d\n---\n" % exit_code if exit_code is not None else "") + output})
    return [_line(obj) for obj in lines]


def render_muse(episode, cwd, start):
    lines = [{"recorded_at": _micro(start), "payload": {"kind": "metadata", "record": {"workspace_root": str(cwd)}}}]
    when = start
    for step in episode["turns"]:
        when += 30
        if "user" in step:
            lines.append({"recorded_at": _micro(when), "payload": {"event": {"kind": "model_user_messages", "messages": [step["user"]]}}})
    return [_line(obj) for obj in lines]


RENDERERS = {"codex": render_codex, "claude": render_claude, "grok": render_grok, "muse": render_muse}


def write_episode(roots, episode, cwd, start):
    harness = episode["harness"]
    session = native(episode)
    raw_lines = RENDERERS[harness](episode, cwd, start)
    day = datetime.datetime.fromtimestamp(start, datetime.timezone.utc)
    if harness == "codex":
        path = Path(roots["codex"]) / day.strftime("%Y/%m/%d") / ("rollout-%s-%s.jsonl" % (day.strftime("%Y-%m-%dT%H-%M-%S"), session))
    elif harness == "claude":
        directory = Path(roots["claude"]) / str(cwd).replace("/", "-")
        path = directory / (episode["subagent_of"] if episode.get("subagent_of") else "") / ("subagents" if episode.get("subagent_of") else "") / (session + ".jsonl")
    elif harness == "grok":
        directory = Path(roots["grok"]) / quote(str(cwd), safe="") / session
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "summary.json").write_text(json.dumps({"info": {"id": session, "cwd": str(cwd)}}))
        (directory / "events.jsonl").write_bytes(_line({"type": "turn_started", "session_relationship": "primary", "ts": _iso(start)}))
        path = directory / "chat_history.jsonl"
    else:
        path = Path(roots["muse"]) / day.strftime("%Y/%m/%d") / session / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(raw_lines)
    if episode.get("malformed") == "partial_tail":
        body += b'{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_te'
    elif episode.get("malformed") == "garbage_line":
        raw = body.split(b"\n")
        raw.insert(max(1, len(raw) // 2), b"{not json")
        body = b"\n".join(raw)
    path.write_bytes(body)
    stamp = start + 30 * (len(episode["turns"]) + 1)
    os.utime(path, (stamp, stamp))
    return path


def _git(path, *args, env=None):
    base = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    base.update(env or {})
    subprocess.run(["git", "-C", str(path), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=dict(os.environ, **base))


def _write_files(root, files):
    for relative, content in files.items():
        target = Path(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def load_fixture(split) -> dict[str, Any]:
    merged: dict[str, Any] = {"projects": {}, "episodes": [], "memories": [], "mutations": [], "cases": []}
    for path in sorted((FIXTURES / split).glob("*.json")):
        part = json.loads(path.read_text())
        merged.setdefault("now", part.get("now"))
        for key in merged["projects"].keys() & part.get("projects", {}).keys():
            raise SystemExit("project %s defined twice (%s)" % (key, path.name))
        merged["projects"].update(part.get("projects", {}))
        for key in ("episodes", "memories", "mutations", "cases"):
            merged[key].extend(part.get(key, []))
    if not merged["cases"]:
        raise SystemExit("no fixtures under " + str(FIXTURES / split))
    return merged


def validate(fixture):
    problems = []
    projects = fixture.get("projects", {})
    episodes = {}
    identities = set()
    for episode in fixture.get("episodes", []):
        key = episode.get("id")
        if not isinstance(key, str) or not key or key in episodes:
            problems.append("episode id missing or duplicated: %r" % key)
            continue
        episodes[key] = episode
        identity = (episode.get("harness"), native(episode))
        if identity in identities:
            problems.append("episode %s repeats harness+native id %s" % (key, identity))
        identities.add(identity)
        if episode.get("project") not in projects:
            problems.append("episode %s names unknown project" % key)
        if episode.get("harness") not in HARNESSES:
            problems.append("episode %s has unknown harness" % key)
        worktree = episode.get("worktree")
        if worktree and worktree not in projects.get(episode.get("project"), {}).get("worktrees", {}):
            problems.append("episode %s names unknown worktree" % key)
        if not episode.get("turns"):
            problems.append("episode %s has no turns" % key)
        for step in episode.get("turns", []):
            if len(step) != 1 or next(iter(step)) not in STEP_KEYS:
                problems.append("episode %s has a malformed turn" % key)
            elif "tool" in step and not {"cmd"} <= set(step["tool"]):
                problems.append("episode %s tool turn needs cmd" % key)
        if episode.get("harness") == "muse" and any("user" not in step for step in episode.get("turns", [])):
            problems.append("episode %s: muse transcripts carry user turns only" % key)
    memories = {}
    for memory in fixture.get("memories", []):
        memories[memory["id"]] = memory
        if memory.get("project") not in projects:
            problems.append("memory %s names unknown project" % memory["id"])
        for source in memory.get("sources", []):
            episode = episodes.get(source.get("episode"))
            if episode is None:
                problems.append("memory %s cites unknown episode" % memory["id"])
            elif not any(source.get("contains", "") in json.dumps(step) for step in episode["turns"]):
                problems.append("memory %s source text not found in %s" % (memory["id"], source["episode"]))
    seen = set()
    for case in fixture.get("cases", []):
        key = case.get("id")
        if key in seen:
            problems.append("duplicate case id " + str(key))
        seen.add(key)
        if case.get("category") not in CATEGORIES:
            problems.append("case %s has unknown category" % key)
        if case.get("expect") not in ("match", "abstain"):
            problems.append("case %s expect must be match or abstain" % key)
        if case.get("project") not in projects:
            problems.append("case %s names unknown project" % key)
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            problems.append("case %s needs a query" % key)
        for field in ("relevant", "misleading", "forbidden"):
            for ref in case.get(field, []):
                if ref not in episodes:
                    problems.append("case %s %s cites unknown episode %s" % (key, field, ref))
        for ref in case.get("forbidden", []):
            if ref in episodes and episodes[ref].get("project") == case.get("project"):
                problems.append("case %s forbids same-project episode %s; use misleading" % (key, ref))
        for group in case.get("required", []):
            for ref in group:
                if ref not in episodes:
                    problems.append("case %s required cites unknown episode %s" % (key, ref))
        for ref in case.get("stale_memories", []) + case.get("valid_memories", []):
            if ref not in memories:
                problems.append("case %s cites unknown memory %s" % (key, ref))
        if case.get("expect") == "match" and not case.get("relevant") and not case.get("valid_memories"):
            problems.append("match case %s has no relevant evidence" % key)
        if case.get("expect") == "abstain" and (case.get("relevant") or case.get("required")):
            problems.append("abstain case %s lists relevant evidence" % key)
        if case.get("worktree") and case["worktree"] not in projects.get(case.get("project"), {}).get("worktrees", {}):
            problems.append("case %s names unknown worktree" % key)
    return problems


class World:
    index_seconds: float = 0.0
    index_stats: Any = None
    capture: dict[str, Any] = {}

    def __init__(self, fixture, directory):
        self.fixture = fixture
        self.directory = Path(directory)
        self.now = datetime.datetime.fromisoformat(fixture["now"].replace("Z", "+00:00")).timestamp()
        self.roots = {name: self.directory / "roots" / name for name in HARNESSES}
        for path in self.roots.values():
            path.mkdir(parents=True)
        self.state_dir = self.directory / "state"
        self.projects = {}
        self.episode_paths = {}
        self.episodes = {episode["id"]: episode for episode in fixture["episodes"]}
        self.session_map = {qualified(episode["harness"], native(episode)): episode["id"] for episode in fixture["episodes"]}
        self.groups = {episode["id"]: episode["dup_group"] for episode in fixture["episodes"] if episode.get("dup_group")}

    def build(self):
        for key, spec in self.fixture["projects"].items():
            path = self.directory / "repos" / key
            path.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
            _write_files(path, spec.get("files", {"README.md": key + "\n"}))
            _git(path, "add", "-A")
            _git(path, "commit", "-q", "-m", "initial")
            worktrees = {}
            for name, wspec in spec.get("worktrees", {}).items():
                wpath = self.directory / "repos" / (key + "--" + name)
                _git(path, "worktree", "add", "-q", "-b", name, str(wpath))
                _write_files(wpath, wspec.get("files", {}))
                _git(wpath, "add", "-A")
                _git(wpath, "commit", "-q", "-m", "branch work", "--allow-empty")
                worktrees[name] = wpath
            self.projects[key] = {"path": path, "worktrees": worktrees}
        for episode in self.fixture["episodes"]:
            project = self.projects[episode["project"]]
            cwd = project["worktrees"][episode["worktree"]] if episode.get("worktree") else project["path"]
            start = self.now - 86400 * float(episode.get("age_days", 5))
            self.episode_paths[episode["id"]] = write_episode(self.roots, episode, cwd, start)

    def apply_mutations(self):
        for mutation in self.fixture.get("mutations", []):
            project = self.projects[mutation["project"]]
            base = project["worktrees"][mutation["worktree"]] if mutation.get("worktree") else project["path"]
            target = base / mutation["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(mutation["content"])
            if mutation.get("commit"):
                _git(base, "add", "-A")
                _git(base, "commit", "-q", "-m", "mutation")

    def cwd(self, project, worktree=None):
        entry = self.projects[project]
        return entry["worktrees"][worktree] if worktree else entry["path"]

    def root_args(self):
        return {name: [path] for name, path in self.roots.items()}


def qualified(harness, native_id):
    module = importlib.import_module("agent_memory.transcripts") if importlib.util.find_spec("agent_memory.transcripts") else None
    if module is None:
        return native_id
    return module.qualified_id(harness, native_id)


def judge(case, results, session_map, groups) -> list[dict[str, Any]]:
    def episode_of(session_id):
        return session_map.get(session_id, session_id)

    relevant = set(case.get("relevant", [])) | {ref for group in case.get("required", []) for ref in group}
    misleading = set(case.get("misleading", []))
    forbidden = set(case.get("forbidden", []))
    stale = set(case.get("stale_memories", []))
    rows = []
    for result in results:
        support = {episode_of(session) for session in result["sessions"]}
        rows.append({
            "support": support,
            "relevant": bool(support & relevant) or bool(set(result.get("memories", ())) & set(case.get("valid_memories", []))),
            "misleading": bool(support & misleading),
            "leak": bool(support & forbidden),
            "stale": bool(set(result.get("memories", ())) & stale) and result["tier"] == "instruction" and result.get("status") == "verified",
            "group": next((groups[ref] for ref in support if ref in groups), None),
        })
    return rows


def case_metrics(case, results, session_map, groups):
    rows = judge(case, results, session_map, groups)
    top3, top5 = rows[:3], rows[:5]
    metrics = {"returned": len(rows), "leak": sum(row["leak"] for row in rows), "stale": sum(row["stale"] for row in rows)}
    if case["expect"] == "abstain":
        metrics["abstain_correct"] = int(not rows)
        return metrics
    metrics["recall1"] = int(any(row["relevant"] for row in rows[:1]))
    metrics["recall5"] = int(any(row["relevant"] for row in top5))
    metrics["precision3"] = (sum(row["relevant"] for row in top3) / len(top3)) if top3 else 0.0
    first = next((index for index, row in enumerate(rows) if row["relevant"]), None)
    metrics["mrr"] = 1 / (first + 1) if first is not None else 0.0
    metrics["misleading_top3"] = sum(row["misleading"] and not row["relevant"] for row in top3)
    if case.get("required"):
        covered = sum(any(row["support"] & set(group) for row in top5) for group in case["required"])
        metrics["coverage"] = covered / len(case["required"])
    seen = {}
    duplicates = 0
    for row in top5:
        if row["group"]:
            duplicates += seen.get(row["group"], 0)
            seen[row["group"]] = 1
    metrics["duplicates"] = duplicates
    return metrics


def aggregate(per_case):
    def mean(key, subset):
        values = [metrics[key] for _, metrics in subset if key in metrics]
        return sum(values) / len(values) if values else None

    matches = [(case, m) for case, m in per_case if case["expect"] == "match"]
    negatives = [(case, m) for case, m in per_case if case["expect"] == "abstain"]
    summary = {
        "cases": len(per_case), "match_cases": len(matches), "abstain_cases": len(negatives),
        "recall@1": mean("recall1", matches), "recall@5": mean("recall5", matches), "precision@3": mean("precision3", matches),
        "mrr": mean("mrr", matches), "coverage": mean("coverage", matches),
        "abstention_correct": mean("abstain_correct", negatives),
        "leakage_violations": sum(m["leak"] for _, m in per_case),
        "stale_instruction_violations": sum(m["stale"] for _, m in per_case),
        "duplicates_per_case": mean("duplicates", matches),
        "misleading_in_top3": sum(m.get("misleading_top3", 0) for _, m in matches),
        "avg_results": sum(m["returned"] for _, m in per_case) / max(1, len(per_case)),
    }
    return summary


class LegacySystem:
    name = "legacy-recall"

    def __init__(self, world, learner, store):
        self.learner, self.store, self.world = learner, store, world
        self.words = importlib.import_module("agent_memory.learning").words
        self.redact = importlib.import_module("agent_memory.store").redact

    def run(self, case, limit, now) -> list[dict[str, Any]]:
        project = importlib.import_module("agent_memory.store").project_identity(self.world.cwd(case["project"], case.get("worktree")))
        terms, matches = self.words(case["query"]), []
        db = self.store.db
        cwd = self.world.cwd(case["project"], case.get("worktree"))
        for memory in db.execute("SELECT * FROM learning_memories WHERE project=? AND state='approved'", (project,)):
            try:
                self.learner._memory_conditions(cwd, json.loads(memory["validity"]))
            except (importlib.import_module("agent_memory.learning").LearningError, OSError, UnicodeError, ValueError):
                continue
            score = len(terms & self.words(memory["action"] + " " + memory["applicability"]))
            if score:
                matches.append((score, memory))
        out = []
        for _, memory in sorted(matches, key=lambda entry: -entry[0])[:3]:
            out.append({"sessions": self.memory_sessions(memory["finding"]), "tier": "instruction", "status": "verified",
                        "memories": [self.world.memory_ids.get(memory["finding"])], "chars": len(memory["action"]) + len(memory["applicability"])})
        return out

    def memory_sessions(self, finding):
        return [row[0] for row in self.store.db.execute("SELECT session FROM learning_incidents WHERE finding=?", (finding,))]


class RawFtsSystem:
    def __init__(self, world, store, mode):
        self.world, self.store, self.mode = world, store, mode
        self.name = "fts5-" + mode

    def run(self, case, limit, now) -> list[dict[str, Any]]:
        project_identity = importlib.import_module("agent_memory.store").project_identity
        project = project_identity(self.world.cwd(case["project"], case.get("worktree")))
        query = case["query"]
        if self.mode == "or":
            terms = [t for t in "".join(c if c.isalnum() or c in "_-" else " " for c in query).split() if len(t) > 2]
            query = " OR ".join('"%s"' % t for t in terms)
        try:
            rows = self.store.search(query, project, limit=limit)
        except importlib.import_module("agent_memory.store").StoreError:
            return []
        seen, out = set(), []
        for row in rows:
            if row["session"] in seen:
                continue
            seen.add(row["session"])
            out.append({"sessions": [row["session"]], "tier": "lead", "chars": len(row["text"][:240]), "status": "unverified"})
        return out


class RecallSystem:
    name = "new-recall"

    def __init__(self, world, learner, weak=False):
        self.world, self.learner, self.weak = world, learner, weak
        if weak:
            self.name = "new-recall-weak"

    def run(self, case, limit, now) -> list[dict[str, Any]]:
        cwd = self.world.cwd(case["project"], case.get("worktree"))
        project = importlib.import_module("agent_memory.store").project_identity(cwd)
        response = self.learner.retrieve(project, case["query"], cwd=cwd, limit=limit, now=now, weak=self.weak)
        out = []
        for item in response["results"]:
            out.append({
                "sessions": [support["session"] for support in item["support"]], "tier": item["tier"], "status": item["status"],
                "memories": [self.world.memory_ids.get(int(item["ref"].split(":")[1]))] if item["ref"].startswith("memory:") else [],
                "score": None, "dup_group": None, "chars": len(json.dumps(item)),
            })
        return out


def install_memories(world, learner, store, capture_ids):
    db = store.db
    world.memory_ids = {}
    project_ids = {}
    identity = importlib.import_module("agent_memory.store").project_identity
    for key, entry in world.projects.items():
        project_ids[key] = identity(entry["path"])
    with db:
        for key, entry in world.projects.items():
            db.execute("INSERT OR REPLACE INTO learning_projects(project,cwd,enabled,enabled_at,repository,grant_source) VALUES(?,?,1,0,?,'project')",
                       (project_ids[key], str(entry["path"]), str(ROOT)))
        for memory in world.fixture.get("memories", []):
            project = project_ids[memory["project"]]
            fingerprint = hashlib.sha256(("fixture" + memory["id"]).encode()).hexdigest()
            proposal = {"owner": "memory", "target": "memory", "future_action": memory["action"], "applicability": memory["applicability"], "scope": "project"}
            finding = db.execute(
                "INSERT INTO learning_findings(project,signal,fingerprint,terms,state,created_at,updated_at,proposal,integrated_at) VALUES(?,?,?,?,'memory',?,?,?,?)",
                (project, "correction", fingerprint, memory["id"], world.now, world.now, json.dumps(proposal), world.now - 86400 * memory.get("age_days", 1))).lastrowid
            world.memory_ids[finding] = memory["id"]
            db.execute("INSERT INTO learning_memories(finding,project,action,applicability,revision,state,created_at,validity) VALUES(?,?,?,?,?,'approved',?,?)",
                       (finding, project, memory["action"], memory["applicability"], "fixture", world.now - 86400 * memory.get("age_days", 1),
                        json.dumps(memory.get("validity", []))))
            for source in memory.get("sources", []):
                resolved = capture_ids(source["episode"], source.get("contains", ""))
                if resolved:
                    session, position, digest = resolved
                    db.execute("INSERT OR IGNORE INTO learning_incidents(finding,session,root_session,position,digest,signal,observed_at) VALUES(?,?,?,?,?,?,?)",
                               (finding, session, session, position, digest, "correction", world.now))


def locate_event(store, world):
    reverse = {value: key for key, value in world.session_map.items()}

    def locate(episode_id, contains):
        row = store.db.execute(
            "SELECT e.session,e.position,e.digest FROM events e WHERE e.session=? AND e.text LIKE ? ORDER BY e.position LIMIT 1",
            (reverse[episode_id], "%" + contains.replace("%", "") + "%")).fetchone()
        return tuple(row) if row else None
    return locate


def prepare(split, directory, system_names, haystack=0):
    fixture = load_fixture(split)
    problems = validate(fixture)
    if problems:
        raise SystemExit("invalid fixture %s:\n  " % split + "\n  ".join(problems))
    if haystack:
        fixture["episodes"].extend(haystack_episodes(list(fixture["projects"]), haystack, 12))
    world = World(fixture, directory)
    world.build()
    store_module = importlib.import_module("agent_memory.store")
    learning = importlib.import_module("agent_memory.learning")
    store = store_module.Store(world.state_dir)
    learner = learning.Learner(store, None)
    started = time.perf_counter()
    stats = store.refresh(roots=world.root_args() if ("new" in system_names or "new-weak" in system_names) else [world.roots["codex"]])
    world.index_seconds = time.perf_counter() - started
    world.index_stats = stats
    if hasattr(learner, "observe_project") and ("new" in system_names or "new-weak" in system_names):
        identity = importlib.import_module("agent_memory.store").project_identity
        world.capture = {}
        for key, entry in world.projects.items():
            world.capture[key] = learner.observe_project(identity(entry["path"]), entry["path"], world.now, 0)
    install_memories(world, learner, store, locate_event(store, world))
    world.apply_mutations()
    return world, store, learner


def retry_requests(split, haystack=0):
    WORK_TMP.mkdir(parents=True, exist_ok=True)
    requests = []
    with tempfile.TemporaryDirectory(dir=str(WORK_TMP)) as directory:
        world, store, learner = prepare(split, directory, ["new"], haystack)
        system = RecallSystem(world, learner)
        identity = importlib.import_module("agent_memory.store").project_identity
        for case in world.fixture["cases"]:
            if system.run(case, 3, world.now):
                continue
            project = world.fixture["projects"][case["project"]]
            files = sorted(set(project.get("files", {})) | set(project.get("worktrees", {}).get(case.get("worktree") or "", {}).get("files", {})))
            requests.append({
                "id": case["id"], "query": case["query"], "repository_files": files,
                "hint": agent_learning_module().RETRY_HINT,
                "vocabulary": learner.vocabulary(identity(world.cwd(case["project"], case.get("worktree")))),
            })
        store.close()
    return requests


def agent_learning_module():
    return importlib.import_module("agent_memory.learning")


def run_systems(split, names, limit=5, repeats=1, haystack=0, retries=None) -> dict[str, Any]:
    retries = retries or {}
    WORK_TMP.mkdir(parents=True, exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(dir=str(WORK_TMP)) as directory:
        world, store, learner = prepare(split, directory, names, haystack)
        systems = []
        for name in names:
            if name == "legacy":
                systems.append(LegacySystem(world, learner, store))
            elif name == "fts-raw":
                systems.append(RawFtsSystem(world, store, "raw"))
            elif name == "fts-or":
                systems.append(RawFtsSystem(world, store, "or"))
            elif name == "new":
                systems.append(RecallSystem(world, learner))
            elif name == "new-weak":
                systems.append(RecallSystem(world, learner, weak=True))
        cases = world.fixture["cases"]
        for system in systems:
            per_case, timings, chars, details, retried = [], [], [], {}, []
            for case in cases:
                best = None
                found: list[Any] = []
                for _ in range(repeats):
                    started = time.perf_counter()
                    found = system.run(case, limit, world.now)
                    elapsed = time.perf_counter() - started
                    best = elapsed if best is None else min(best, elapsed)
                per_case.append((case, case_metrics(case, found, world.session_map, world.groups)))
                if retries is not None and system.name == "new-recall":
                    second = system.run(dict(case, query=retries[case["id"]]), limit, world.now) if not found and retries.get(case["id"]) else found
                    retried.append((case, case_metrics(case, second, world.session_map, world.groups)))
                details[case["id"]] = [{"episodes": sorted(world.session_map.get(session, session) for session in item["sessions"]), "tier": item["tier"], "status": item.get("status"),
                                        "memories": item.get("memories"), "score": item.get("score")} for item in found[:5]]
                timings.append(best)
                chars.append(sum(item["chars"] for item in found[:3]))
            summary = aggregate(per_case)
            summary["context_chars_avg"] = sum(chars) / max(1, len(chars))
            summary["latency_ms_median"] = statistics.median(timings) * 1000
            summary["latency_ms_p95"] = sorted(timings)[int(0.95 * (len(timings) - 1))] * 1000
            by_category = {}
            for case, metrics in per_case:
                by_category.setdefault(case["category"], []).append((case, metrics))
            summary["by_category"] = {key: aggregate(value) for key, value in sorted(by_category.items())}
            if retried:
                summary["after_one_retry"] = aggregate(retried)
                summary["retries_used"] = sum(1 for case, _ in per_case if retries.get(case["id"]) and not details[case["id"]])
            summary["details"] = details
            summary["failures"] = [case["id"] for case, m in per_case if (case["expect"] == "match" and not m["recall5"]) or (case["expect"] == "abstain" and not m["abstain_correct"])]
            results[system.name] = summary
        results["_index"] = {"seconds": world.index_seconds, "bytes": os.path.getsize(store.path), "stats": world.index_stats}
        store.close()
    return results


HAY_WORDS = (
    "update refactor module function class method variable string number list dict array loop branch merge commit rebase config setting option flag "
    "build compile link package install dependency version upgrade release deploy server client request response handler route endpoint database query "
    "schema migration table column index cache queue worker thread process memory cpu disk network socket timeout retry error warning exception stack trace "
    "log debug info test fixture mock assert expect pass fail skip suite coverage lint format type check parse render template component view model "
    "controller service repository interface implementation abstraction pattern design review document readme comment example tutorial script command shell "
    "terminal editor plugin extension theme layout style color font image icon button form input output buffer stream reader writer encoder decoder token "
    "session cookie header payload body json yaml toml csv xml html css markdown regex path directory file folder name value key item entry record field"
).split()
HAY_TEMPLATES = (
    "please {a} the {b} {c} and then {d} the {e}", "why does the {a} {b} fail when the {c} is empty", "add a {a} {b} for the {c} {d}",
    "{a} {b}: the {c} {d} returns {e} instead of {f}", "can you {a} this {b} and {c} the {d} output", "rename the {a} {b} to {c} everywhere",
)


def haystack_episodes(project_keys, per_project, turns, seed=7):
    import random
    rng = random.Random(seed)
    episodes = []
    for key in project_keys:
        for number in range(per_project):
            steps = []
            for _ in range(turns):
                words = {letter: rng.choice(HAY_WORDS) for letter in "abcdef"}
                text = rng.choice(HAY_TEMPLATES).format(**words)
                kind = rng.random()
                if kind < 0.35:
                    steps.append({"user": text})
                elif kind < 0.6:
                    steps.append({"assistant": text + " " + " ".join(rng.choice(HAY_WORDS) for _ in range(rng.randint(6, 30)))})
                else:
                    noise = "\n".join("%s %s 0x%06x %s" % (rng.choice(HAY_WORDS), rng.choice(HAY_WORDS), rng.randrange(1 << 24), rng.choice(HAY_WORDS)) for _ in range(rng.randint(3, 12)))
                    steps.append({"tool": {"cmd": "%s %s" % (rng.choice(("pytest -q", "cargo test", "make", "npm test", "go test ./...", "git status")), rng.choice(HAY_WORDS)), "output": noise, "exit": 0}})
            episodes.append({"id": "hay-%s-%d" % (key, number), "project": key, "harness": ("codex", "claude", "grok")[number % 3],
                             "age_days": 1 + (number * 37) % 380, "turns": steps})
    return episodes


def explain_cases(split, case_ids, haystack=0):
    WORK_TMP.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(WORK_TMP)) as directory:
        world, store, learner = prepare(split, directory, ["new"], haystack)
        identity = importlib.import_module("agent_memory.store").project_identity
        for case in world.fixture["cases"]:
            if case["id"] not in case_ids:
                continue
            cwd = world.cwd(case["project"], case.get("worktree"))
            response = learner.retrieve(identity(cwd), case["query"], cwd=cwd, limit=5, now=world.now, explain=True)
            print("CASE %s [%s] expect=%s relevant=%s misleading=%s q=%r" % (case["id"], case["category"], case["expect"], case.get("relevant"), case.get("misleading"), case["query"]))
            print(json.dumps({"query": response["explain"]["query"], "abstained": response["abstained"], "reason": response["reason"]}))
            for item in response["explain"]["candidates"][:8]:
                print("  cand", item["ref"], world.session_map.get(next(iter(item.get("sessions", [])), ""), ""), item["tier"], item["score"], item["features"])
            for item in response["results"]:
                print("  RESULT", item["ref"], item["tier"], item["status"], [world.session_map.get(s["session"], s["session"]) for s in item["support"]], item["summary"][:100])
            print("  excluded", response["explain"]["excluded"][:6])
        store.close()


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", metavar="command")
    commands.add_parser("validate")
    why = commands.add_parser("explain")
    why.add_argument("--split", choices=SPLITS, default="dev")
    why.add_argument("case", nargs="+")
    why.add_argument("--haystack", type=int, default=0)
    run = commands.add_parser("run")
    run.add_argument("--split", choices=SPLITS, default="dev")
    run.add_argument("--system", action="append", choices=("legacy", "fts-raw", "fts-or", "new", "new-weak"))
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--haystack", type=int, default=0, metavar="N", help="add N synthetic routine sessions per project to approximate a realistic corpus size")
    run.add_argument("--json", action="store_true")
    run.add_argument("--detail", action="store_true", help="print failing cases with returned results")
    run.add_argument("--retries", metavar="FILE", help="JSON map of case id to the retry query an agent wrote after an abstention")
    requests = commands.add_parser("retry-requests", help="print the abstained cases an agent would be asked to retry")
    requests.add_argument("--split", choices=SPLITS, default="dev")
    requests.add_argument("--haystack", type=int, default=0)
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 2
    if args.command == "validate":
        bad = 0
        for split in SPLITS:
            if not any((FIXTURES / split).glob("*.json")):
                continue
            problems = validate(load_fixture(split))
            bad += len(problems)
            print("%s: %d problems" % (split, len(problems)))
            for problem in problems:
                print("  " + problem)
        return 1 if bad else 0
    if args.command == "retry-requests":
        print(json.dumps(retry_requests(args.split, args.haystack), indent=1))
        return 0
    if args.command == "explain":
        explain_cases(args.split, set(args.case), args.haystack)
        return 0
    names = args.system or ["legacy", "fts-raw", "fts-or", "new"]
    retries = json.loads(Path(args.retries).read_text()) if args.retries else None
    report = run_systems(args.split, names, repeats=args.repeats, haystack=args.haystack, retries=retries)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for name, summary in report.items():
            if name.startswith("_"):
                continue
            keys = ("recall@1", "recall@5", "precision@3", "mrr", "coverage", "abstention_correct", "leakage_violations", "stale_instruction_violations", "context_chars_avg", "latency_ms_median")
            if args.detail:
                cases = {case["id"]: case for case in load_fixture(args.split)["cases"]}
                for case_id in summary["failures"]:
                    case = cases[case_id]
                    print("FAIL %s [%s] expect=%s q=%r relevant=%s misleading=%s" % (case_id, case["category"], case["expect"], case["query"], case.get("relevant"), case.get("misleading")))
                    for item in summary["details"][case_id]:
                        print("     -> %s tier=%s status=%s score=%s mem=%s" % (item["episodes"], item["tier"], item["status"], item["score"], item["memories"]))
            print(name + ": " + " ".join("%s=%s" % (key, "n/a" if summary.get(key) is None else round(summary[key], 3)) for key in keys))
    return 0


if __name__ == "__main__":
    sys.exit(main())
