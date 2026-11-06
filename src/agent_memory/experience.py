#!/usr/bin/env python3
"""Deterministic capture of project experience from indexed session events, with dedupe, supersession, and retirement."""
import dataclasses
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from agent_memory.recall import doc_tokens, stem, technical_tokens
from agent_memory.store import active_branch, redact

MAX_EVENTS = 2000
MAX_CLAIM = 500
MAX_APPLIES = 300
MAX_SESSIONS_PER_RUN = 60
NEAR_DUPLICATE_WORDS = 0.75
NEAR_DUPLICATE_IDENTIFIERS = 0.5
CONTRADICTION_IDENTIFIERS = 0.6
RETIRE_AFTER_DAYS = 180
CORRECTION = re.compile(r"\b(actually|i meant|you (?:should|must|already)|do not|don't|stop|wrong|instead|again|ignored|forgot|why did you)\b", re.I)
ERROR = re.compile(r"(?:exit(?:ed)? (?:code|with code|status)[: ]+[1-9]|(?:command |test |build )?failed|permission denied|not found|no such file|error:)", re.I)
FAILURE = re.compile(r"(?:\bFAILED\b|\bfailed\b|Traceback \(most recent|error(?:\[E\d+\])?:|\bpanic(?:ked)?\b|fatal:|undefined reference|cannot find|ModuleNotFoundError|ImportError|Segmentation fault|OOMKilled|\bKilled\b|exit(?:ed)? (?:code|status)[: ]+[1-9]|npm ERR!|\bE[A-Z]{3,}[A-Z]*\b:)")
SUCCESS = re.compile(r"(?:\b\d+ (?:tests? )?passed\b|test result: ok\.|\bPASS(?:ED)?\b|^ok\b|All checks passed|Build succeeded|BUILD SUCCESSFUL|\bFinished (?:`|dev|release)|\b0 errors?\b|exit(?:ed)? (?:code|status)[: ]+0\b|Successfully )", re.I | re.M)
TRANSIENT = re.compile(r"(?:timed? ?out|timeout|connection (?:reset|refused|aborted)|temporar(?:y|ily)|rate.?limit|\b429\b|\b50[23]\b|network is unreachable|ECONNRESET|flaky|try again|could not resolve host|interrupted|doesn't want to proceed|request was rejected)", re.I)
RUNNER = re.compile(r"\b(?:pytest|py\.test|unittest|tox|nox|cargo\s+(?:test|build|check|clippy|bench|run)|go\s+(?:test|build|vet|run)|npm\s+(?:test|run|ci|install)|pnpm|yarn|bun\s+(?:test|run)|tsc|jest|vitest|eslint|ruff|mypy|make|ninja|cmake|gradle|mvn|bazel|ctest|zig\s+build|swift\s+(?:build|test)|dotnet\s+(?:build|test)|gcc|g\+\+|clang|rustc|docker|git\s+(?:push|pull|rebase|merge|apply)|pip\s+install|uv\s+(?:run|sync)|python3?|node|sh|bash|\./\S+)\b", re.I)
BENCHMARK = re.compile(r"(?:\bbench(?:mark)?\b|hyperfine|criterion|\bwrk\b|\bperf\s+(?:stat|record)|ns/iter|ops/sec|req/s|\bp9[59]\b|flamegraph|profil)", re.I)
TIMING = re.compile(r"(?:\d+(?:\.\d+)?\s?(?:ns|µs|us|ms|s|sec|seconds|ops/s|req/s|MB/s|MiB/s|GB/s)\b)")
EDIT_TOOLS = frozenset(("edit", "write", "multiedit", "apply_patch", "str_replace_editor", "search_replace", "write_file", "edit_file", "notebookedit", "create_file", "replace"))
DISCOVERY = re.compile(r"\b(?:turns out|root cause|the (?:real )?(?:reason|issue|problem|cause) (?:is|was)|it (?:is|was) because|caused by|because the|(?:found|discovered|confirmed|established) that|(?:needs?|requires?|must) (?:be|to)|only (?:works?|happens?|fails?)|(?:is|are) not (?:thread|safe|supported|honou?red)|ignores?|silently)\b", re.I)
ABANDON = re.compile(r"\b(?:abandon(?:ed|ing)?|revert(?:ed|ing)?|rolled? back|dead end|won't work|doesn't work|does not work|not viable|ruled? out|give up on|switch(?:ed|ing)? (?:to|back)|going back to|instead of)\b", re.I)
DECISION = re.compile(r"\b(?:instead of|rather than|not (?:use|using)|won't use|will not use|ruled? out|rejected?|decided|going with|chose|choose|prefer)\b", re.I)
RATIONALE = re.compile(r"\b(?:because|since|due to|so that|as it|as the|which (?:means|would|causes)|otherwise)\b", re.I)
COMPARISON = re.compile(r"\b(?:slower|faster|regress(?:ed|ion)?|worse|better|no (?:improvement|difference)|not the bottleneck|dominated by|bottleneck|speed-?up|overhead)\b", re.I)
SELF_TALK = re.compile(r"^\s*(?:i'?ll|i will|let me|now i|next,? i|i'm going|going to|first,? i|i can|we can|we'?ll|let's)\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_experiences(
    id INTEGER PRIMARY KEY, project TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('success','failure','decision','gotcha','method')),
    claim TEXT NOT NULL, applies_when TEXT NOT NULL, identifiers TEXT NOT NULL, fingerprint TEXT NOT NULL,
    basis TEXT NOT NULL CHECK(basis IN ('deterministic','reviewed')), detector TEXT NOT NULL,
    revision TEXT, worktree TEXT, observed_at REAL NOT NULL, captured_at REAL NOT NULL,
    validity TEXT NOT NULL DEFAULT '[]', tags TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'retained' CHECK(state IN ('retained','retired','superseded','withdrawn')),
    state_reason TEXT, state_at REAL, UNIQUE(project, fingerprint)
);
CREATE INDEX IF NOT EXISTS learning_experiences_project_state ON learning_experiences(project, state);
CREATE TABLE IF NOT EXISTS learning_experience_sources(
    experience INTEGER NOT NULL REFERENCES learning_experiences(id) ON DELETE CASCADE,
    session TEXT NOT NULL, position INTEGER NOT NULL, digest TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('trigger','outcome','context')),
    PRIMARY KEY(experience, session, position)
);
CREATE TABLE IF NOT EXISTS learning_experience_links(
    newer INTEGER NOT NULL REFERENCES learning_experiences(id) ON DELETE CASCADE,
    older INTEGER NOT NULL REFERENCES learning_experiences(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK(relation IN ('supersedes','contradicts')), created_at REAL NOT NULL,
    PRIMARY KEY(newer, older, relation)
);
CREATE TABLE IF NOT EXISTS learning_signatures(
    project TEXT NOT NULL, signature TEXT NOT NULL, session TEXT NOT NULL, position INTEGER NOT NULL,
    digest TEXT NOT NULL, command TEXT NOT NULL, observed_at REAL NOT NULL,
    PRIMARY KEY(project, signature, session)
);
CREATE TABLE IF NOT EXISTS learning_observed(
    session TEXT PRIMARY KEY, signature TEXT NOT NULL, observed_at REAL NOT NULL, experiences INTEGER NOT NULL, suppressed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS learning_forgotten_experiences(
    project TEXT NOT NULL, fingerprint TEXT NOT NULL, forgotten_at REAL NOT NULL, PRIMARY KEY(project, fingerprint)
);
CREATE TABLE IF NOT EXISTS learning_observation(
    project TEXT PRIMARY KEY, cwd TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), enabled_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS learning_experiences_immutable
BEFORE UPDATE OF project, kind, claim, applies_when, identifiers, fingerprint, basis, detector, revision, worktree, observed_at, validity, tags
ON learning_experiences
BEGIN SELECT RAISE(ABORT, 'experience content is immutable; record a superseding experience instead'); END;
"""


def recall_schema():
    statements = [
        "CREATE TABLE IF NOT EXISTS learning_recall_docs(doc INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('memory','experience')), "
        "ref INTEGER NOT NULL, project TEXT NOT NULL, UNIQUE(kind, ref))",
        "CREATE VIRTUAL TABLE IF NOT EXISTS learning_recall_fts USING fts5(applies, claim, action, identifiers, tokenize='unicode61 remove_diacritics 2')",
    ]
    for table, kind, key, active, fields in (
        ("learning_memories", "memory", "finding", "state = 'approved'", "new.applicability, '', new.action, ''"),
        ("learning_experiences", "experience", "id", "state = 'retained'", "new.applies_when, new.claim, '', new.identifiers"),
    ):
        drop = (
            "DELETE FROM learning_recall_fts WHERE rowid IN (SELECT doc FROM learning_recall_docs WHERE kind = '{kind}' AND ref = {old}.{key}); "
            "DELETE FROM learning_recall_docs WHERE kind = '{kind}' AND ref = {old}.{key}; "
        )
        add = (
            "INSERT INTO learning_recall_docs(kind, ref, project) SELECT '{kind}', new.{key}, new.project WHERE new.{active}; "
            "INSERT INTO learning_recall_fts(rowid, applies, claim, action, identifiers) "
            "SELECT doc, {fields} FROM learning_recall_docs WHERE kind = '{kind}' AND ref = new.{key}; "
        )
        fmt = {"kind": kind, "key": key, "active": active, "fields": fields}
        statements.append("CREATE TRIGGER IF NOT EXISTS {t}_recall_ai AFTER INSERT ON {t} BEGIN {drop}{add}END".format(
            t=table, drop=drop.format(old="new", **fmt), add=add.format(**fmt)))
        statements.append("CREATE TRIGGER IF NOT EXISTS {t}_recall_au AFTER UPDATE ON {t} BEGIN {drop}{add}END".format(
            t=table, drop=drop.format(old="old", **fmt), add=add.format(**fmt)))
        statements.append("CREATE TRIGGER IF NOT EXISTS {t}_recall_ad AFTER DELETE ON {t} BEGIN {drop}END".format(
            t=table, drop=drop.format(old="old", **fmt)))
    return statements


def rebuild_recall_projection(db):
    with db:
        db.execute("DELETE FROM learning_recall_fts")
        db.execute("DELETE FROM learning_recall_docs")
        db.execute("INSERT INTO learning_recall_docs(kind, ref, project) SELECT 'memory', finding, project FROM learning_memories WHERE state = 'approved'")
        db.execute("INSERT INTO learning_recall_docs(kind, ref, project) SELECT 'experience', id, project FROM learning_experiences WHERE state = 'retained'")
        db.execute("INSERT INTO learning_recall_fts(rowid, applies, claim, action, identifiers) "
                   "SELECT d.doc, m.applicability, '', m.action, '' FROM learning_recall_docs d JOIN learning_memories m ON d.kind = 'memory' AND m.finding = d.ref")
        db.execute("INSERT INTO learning_recall_fts(rowid, applies, claim, action, identifiers) "
                   "SELECT d.doc, e.applies_when, e.claim, '', e.identifiers FROM learning_recall_docs d JOIN learning_experiences e ON d.kind = 'experience' AND e.id = d.ref")


@dataclasses.dataclass
class Draft:
    kind: str
    claim: str
    applies_when: str
    detector: str
    subject: str
    observed_at: float
    sources: list[dict[str, Any]]
    tags: str = ""
    paths: tuple[str, ...] = ()
    extra: str = ""


@dataclasses.dataclass
class Run:
    call: int
    tool: str
    command: str
    out: Optional[int] = None


def tool_command(text):
    name, _, rest = text.partition("\n")
    command = ""
    match = re.search(r'"(?:command|cmd)"\s*:\s*"((?:[^"\\]|\\.)*)"', rest)
    if match:
        try:
            command = json.loads('"' + match.group(1) + '"')
        except ValueError:
            command = match.group(1)
    else:
        match = re.search(r"(?im)^\s*(?:command|cmd)\s*[:=]\s*(.+?)\s*$", rest)
        if match:
            command = match.group(1)
        elif rest and "\n" not in rest.strip() and RUNNER.search(rest):
            command = rest.strip()
    return name.strip(), command.strip()


def command_prefix(command):
    tokens = [token for token in re.split(r"\s+", re.sub(r"^(?:cd\s+\S+\s*&&\s*)", "", command.strip())) if token]
    tokens = [token for token in tokens if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token) and token not in ("sudo", "time", "env", "uv", "run", "exec", "timeout")]
    if tokens and tokens[0] in ("python", "python3") and len(tokens) > 2 and tokens[1] == "-m":
        return " ".join(tokens[:3])
    return " ".join(tokens[:2])


def pair_runs(events):
    by_id, runs, open_run = {}, [], None
    for index, event in enumerate(events):
        if event["kind"] == "tool-call":
            tool, command = tool_command(event["text"])
            run = Run(index, tool, command)
            runs.append(run)
            open_run = run
            for call_id in (event.get("call_id") or "").split(","):
                if call_id:
                    by_id[call_id] = run
        elif event["kind"] == "tool-output":
            run = next((by_id[call_id] for call_id in (event.get("call_id") or "").split(",") if call_id in by_id), None)
            if run is None and open_run is not None and open_run.out is None:
                run = open_run
            if run is not None and run.out is None:
                run.out = index
    return runs


def run_outcome(events, run):
    if run.out is None:
        return None
    event = events[run.out]
    if TRANSIENT.search(event["text"][:2000]) and event.get("outcome") == "error" and "doesn't want" in event["text"][:300]:
        return None
    if event.get("outcome") in ("ok", "error"):
        return event["outcome"]
    text = event["text"][:4000]
    if FAILURE.search(text):
        return "error"
    if SUCCESS.search(text):
        return "ok"
    return None


WRAPPER_LINE = re.compile(r"^(?:exit(?:ed)? (?:code|status)\b.*|exit: -?\d+|output:|---|script completed|wall time.*)$", re.I)


def error_signature(text, explicit=False):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not WRAPPER_LINE.match(line.strip())]
    priorities = (
        re.compile(r"^(?:E\s+)?[\w.]*(?:Error|Exception)\b.*"),
        re.compile(r"error(?:\[E\d+\])?:.*", re.I),
        re.compile(r"fatal:.*", re.I),
        re.compile(r".*\bpanic(?:ked)?\b.*", re.I),
        re.compile(r".*undefined reference.*"),
        re.compile(r".*\b(?:ENOSPC|ENOENT|EACCES|EADDRINUSE|ECONNREFUSED|OOMKilled|Killed)\b.*"),
        re.compile(r".*\bFAILED\b.*"),
        re.compile(r".*\bfail(?:ed|ure)\b.*", re.I),
    )
    for pattern in priorities:
        for line in lines[:200]:
            if pattern.search(line):
                signature = re.sub(r"0x[0-9a-fA-F]+|\b\d{4,}\b|/(?:tmp|var/folders)/\S+", "N", line)
                signature = re.sub(r"\s+", " ", signature)[:160]
                if len(re.findall(r"[A-Za-z]{4,}", signature)) >= 1 and signature.lower() not in ("error", "failed", "error:"):
                    return signature
    if explicit and lines:
        signature = re.sub(r"0x[0-9a-fA-F]+|\b\d{4,}\b|/(?:tmp|var/folders)/\S+", "N", re.sub(r"\s+", " ", lines[0]))[:160]
        if len(re.findall(r"[A-Za-z]{4,}", signature)) >= 1:
            return signature
    return ""


def signature_key(signature):
    return " ".join(sorted({stem(word) for word in re.findall(r"[a-z]{3,}", signature.lower())})[:10])


def sentences(text):
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def pick_sentence(text, pattern, minimum=40, maximum=320):
    for sentence in sentences(text):
        if minimum <= len(sentence) <= maximum and pattern.search(sentence) and not SELF_TALK.search(sentence):
            return sentence
    return ""


def edited_paths(events, start, stop):
    paths = []
    for event in events[start:stop]:
        if event["kind"] != "tool-call":
            continue
        tool = event["text"].partition("\n")[0].strip().lower()
        if tool not in EDIT_TOOLS and "apply_patch" not in event["text"][:400]:
            continue
        found = re.findall(r"(?:file_path|path|\*\*\* (?:Update|Add) File:|\+\+\+ b/)\s*:?\s*\"?([\w./@+-]+\.[\w]+)", event["text"])
        for path in found:
            if path not in paths:
                paths.append(path)
    return paths[:3]


def clip(text, limit):
    text = " ".join(redact(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def source(events, index, role):
    event = events[index]
    return {"position": event["position"], "digest": event["digest"], "role": role}


def has_code_dump(text):
    return text.count("\n") > 12 or len(re.findall(r"[{};]\s*$", text, re.M)) > 6


def extract(events, project_files=()):
    drafts = []
    runs = pair_runs(events)
    outcomes = {id(run): run_outcome(events, run) for run in runs}
    drafts.extend(_resolved_failures(events, runs, outcomes))
    drafts.extend(_corrections(events))
    drafts.extend(_abandonments(events, runs, outcomes))
    drafts.extend(_discoveries(events, runs, outcomes))
    drafts.extend(_benchmarks(events, runs))
    drafts.extend(_decisions(events))
    return drafts


def _resolved_failures(events, runs, outcomes):
    drafts = []
    claimed = set()
    for number, run in enumerate(runs):
        if outcomes[id(run)] != "error" or number in claimed or not (run.command and RUNNER.search(run.command)):
            continue
        text = events[run.out]["text"]
        signature = error_signature(text, explicit=events[run.out].get("outcome") == "error")
        if not signature or (TRANSIENT.search(text[:3000]) and not re.search(r"Traceback|error\[|assert", text)):
            continue
        prefix = command_prefix(run.command)
        failures = [number]
        resolved = None
        for later in range(number + 1, len(runs)):
            other = runs[later]
            if not other.command or command_prefix(other.command) != prefix:
                continue
            outcome = outcomes[id(other)]
            if outcome == "ok":
                resolved = later
                break
            if outcome == "error":
                failures.append(later)
        if resolved is None:
            continue
        claimed.update(failures)
        success = runs[resolved]
        edits = edited_paths(events, run.out, success.call)
        between = [item for item in runs[number + 1:resolved] if item.command and outcomes[id(item)] == "ok" and command_prefix(item.command) != prefix]
        if not edits and not between and success.command == run.command:
            continue
        if edits:
            change = "fixed by changing " + ", ".join("`%s`" % path for path in edits)
        elif between:
            change = "passed after running `%s`" % clip(between[-1].command, 120)
        else:
            change = "passed as `%s`" % clip(success.command, 120)
        claim = "`%s` failed with `%s`; %s." % (clip(run.command, 120), signature, change)
        applies = "Running `%s`%s." % (prefix, (" after editing " + ", ".join("`%s`" % path for path in edits)) if edits else "")
        context_index = next((index for index in range(run.out, success.call) if events[index]["kind"] == "tool-call" and (edited_paths(events, index, index + 1) or (between and events[index]["text"] == events[between[-1].call]["text"]))), None)
        sources = [source(events, run.out, "trigger")] + [source(events, success.out, "outcome")]
        if context_index is not None:
            sources.append(source(events, context_index, "context"))
        drafts.append(Draft("failure", clip(claim, MAX_CLAIM), clip(applies, MAX_APPLIES), "resolved_failure", "%s|%s" % (prefix, signature_key(signature)),
                            _observed(events, success.out), sources, "recurring" if len(failures) > 1 else "", tuple(edits)))
    return drafts


def _observed(events, index):
    return _timestamp(events[index].get("timestamp"))


def _timestamp(value):
    import datetime
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _corrections(events):
    drafts = []
    for index, event in enumerate(events):
        text = event["text"]
        if event["kind"] != "user" or len(text) > 2000 or not CORRECTION.search(text):
            continue
        recent = events[max(0, index - 8):index]
        if not any(item["kind"] in ("assistant", "tool-call") for item in recent):
            continue
        identifiers = technical_tokens(text, 6)
        causal = bool(re.search(r"\b(?:because|the (?:bug|problem|issue|reason|cause) is|since|so that|that'?s (?:wrong|not)|is really|actually (?:is|means))\b", text, re.I))
        if not identifiers and not (causal and len(text) >= 60):
            continue
        if has_code_dump(text):
            continue
        action = next((item for item in reversed(recent) if item["kind"] == "tool-call"), None)
        applies = "After the agent ran `%s`." % clip(tool_command(action["text"])[1] or action["text"].partition("\n")[0], 100) if action else \
            "When working on %s." % ", ".join("`%s`" % token for token in identifiers[:3])
        sources = [source(events, index, "trigger")]
        previous = next((i for i in range(index - 1, max(-1, index - 6), -1) if events[i]["kind"] in ("assistant", "tool-call")), None)
        if previous is not None:
            sources.append(source(events, previous, "context"))
        drafts.append(Draft("gotcha", clip("User correction: " + text, MAX_CLAIM), clip(applies, MAX_APPLIES), "user_correction", "", _observed(events, index), sources))
    return drafts


def _abandonments(events, runs, outcomes):
    drafts = []
    for index, event in enumerate(events):
        if event["kind"] != "assistant" or not ABANDON.search(event["text"]):
            continue
        sentence = pick_sentence(event["text"], ABANDON)
        if not sentence or not technical_tokens(sentence, 3):
            continue
        failing = next((run for run in reversed(runs) if run.out is not None and run.out < index and index - run.out <= 10 and outcomes[id(run)] == "error"), None)
        if failing is None:
            continue
        text = events[failing.out]["text"]
        signature = error_signature(text)
        if not signature or TRANSIENT.search(text[:3000]):
            continue
        claim = "Abandoned approach: %s (after `%s` failed with `%s`)." % (sentence, clip(failing.command, 100), signature)
        sources = [source(events, failing.out, "trigger"), source(events, index, "context")]
        drafts.append(Draft("decision", clip(claim, MAX_CLAIM), clip("Considering an approach involving %s." % ", ".join("`%s`" % token for token in technical_tokens(sentence, 3)), MAX_APPLIES),
                            "abandoned_approach", "abandon|" + signature_key(signature), _observed(events, index), sources))
    return drafts


def _discoveries(events, runs, outcomes):
    drafts = []
    for index, event in enumerate(events):
        if event["kind"] != "assistant" or not DISCOVERY.search(event["text"]):
            continue
        sentence = pick_sentence(event["text"], DISCOVERY)
        identifiers = technical_tokens(sentence, 4) if sentence else []
        if not sentence or not identifiers:
            continue
        grounded = []
        for back in range(index - 1, max(-1, index - 14), -1):
            other = events[back]
            if other["kind"] == "tool-output" and any(token.lower() in other["text"].lower() for token in identifiers):
                grounded.append(back)
        if not grounded:
            continue
        confirmed = any(outcomes[id(run)] == "ok" and run.call > index for run in runs)
        failure_before = any(run.out is not None and run.out < index and outcomes[id(run)] == "error" for run in runs[-8:])
        if not (confirmed or failure_before):
            continue
        applies = "Working with %s." % ", ".join("`%s`" % token for token in identifiers[:3])
        sources = [source(events, grounded[0], "trigger"), source(events, index, "context")]
        drafts.append(Draft("gotcha", clip(sentence, MAX_CLAIM), clip(applies, MAX_APPLIES), "investigated_fact", "", _observed(events, index), sources))
    return drafts


def _benchmarks(events, runs):
    drafts = []
    measured = [run for run in runs if run.out is not None and (BENCHMARK.search(run.command) or BENCHMARK.search(events[run.out]["text"][:800])) and TIMING.search(events[run.out]["text"])]
    if not measured:
        return drafts
    for index, event in enumerate(events):
        if event["kind"] not in ("assistant", "user") or not COMPARISON.search(event["text"]):
            continue
        sentence = pick_sentence(event["text"], COMPARISON)
        numbers = set(re.findall(r"\d+(?:\.\d+)?", sentence))
        run = next((item for item in reversed(measured) if item.out < index and index - item.out <= 12), None)
        if not sentence or run is None or not numbers & set(re.findall(r"\d+(?:\.\d+)?", events[run.out]["text"])):
            continue
        negative = bool(re.search(r"\b(?:slower|regress|worse|no (?:improvement|difference)|not the bottleneck)\b", sentence, re.I))
        claim = "%s (measured by `%s`)." % (sentence.rstrip("."), clip(run.command, 100))
        drafts.append(Draft("failure" if negative else "success", clip(claim, MAX_CLAIM), clip("Optimizing or benchmarking `%s`." % clip(command_prefix(run.command), 60), MAX_APPLIES),
                            "benchmark_result", "bench|" + command_prefix(run.command) + "|" + ("neg" if negative else "pos"), _observed(events, index),
                            [source(events, run.out, "trigger"), source(events, index, "context")], "performance"))
    return drafts


def _decisions(events):
    drafts = []
    for index, event in enumerate(events):
        if event["kind"] not in ("user", "assistant") or not (DECISION.search(event["text"]) and RATIONALE.search(event["text"])):
            continue
        sentence = pick_sentence(event["text"], DECISION, minimum=50, maximum=360)
        if not sentence or not RATIONALE.search(sentence):
            continue
        identifiers = technical_tokens(sentence, 4)
        if len(identifiers) < 2 or has_code_dump(event["text"]):
            continue
        applies = "Choosing between %s." % " and ".join("`%s`" % token for token in identifiers[:2])
        drafts.append(Draft("decision", clip(sentence, MAX_CLAIM), clip(applies, MAX_APPLIES), "technical_decision", "", _observed(events, index), [source(events, index, "trigger")]))
    return drafts


def _words(text):
    return {stem(word) for word in re.findall(r"[a-z][a-z0-9]{2,}", text.lower())}


def _fingerprint(draft, identifiers):
    subject = draft.subject or " ".join(sorted(token.lower() for token in identifiers)) + "|" + " ".join(sorted(_words(draft.claim))[:8])
    return hashlib.sha256((draft.kind + "|" + draft.detector + "|" + subject).encode()).hexdigest()


def _jaccard(left, right):
    return len(left & right) / max(1, len(left | right))


POSITIVE = re.compile(r"\b(?:works?|fixed|passes?|passed|faster|use|succeeded|resolved)\b", re.I)
NEGATIVE = re.compile(r"\b(?:fails?|failed|broken|slower|regress(?:ed|ion)?|never|avoid|don't|do not|abandoned|wrong|worse)\b", re.I)


RECENT_SESSION_SECONDS = 3 * 86400
MAX_CONDITION_BYTES = 262144


def file_conditions(cwd, paths, when, now=0):
    conditions = []
    recent = bool(now and when and now - when <= RECENT_SESSION_SECONDS)
    for path in paths[:3]:
        try:
            tracked = subprocess.run(["git", "--literal-pathspecs", "-C", str(cwd), "ls-files", "--error-unmatch", "--", path], capture_output=True, timeout=10)
            target = Path(cwd) / path
            if tracked.returncode or target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_CONDITION_BYTES:
                continue
            if recent or not when:
                blob = subprocess.run(["git", "-C", str(cwd), "hash-object", "--", path], capture_output=True, timeout=10)
            else:
                revision = subprocess.run(["git", "-C", str(cwd), "rev-list", "-1", "--before=@%d" % int(when), "HEAD"], capture_output=True, timeout=10).stdout.decode().strip()
                blob = subprocess.run(["git", "-C", str(cwd), "rev-parse", "%s:%s" % (revision or "HEAD", path)], capture_output=True, timeout=10)
            if not blob.returncode:
                conditions.append({"path": path, "blob": blob.stdout.decode().strip()})
        except (subprocess.SubprocessError, OSError, ValueError):
            continue
    return conditions


def add_experience(db, project, draft, session, now, worktree="", revision="", validity=()):
    identifiers = technical_tokens(draft.claim + " " + draft.applies_when, 12)
    fingerprint = _fingerprint(draft, identifiers)
    indexed = technical_tokens(draft.claim + " " + draft.applies_when + " " + draft.extra, 24)
    sources = [dict(item, session=session) for item in draft.sources]
    if db.execute("SELECT 1 FROM learning_forgotten_experiences WHERE project = ? AND fingerprint = ?", (project, fingerprint)).fetchone():
        return "forgotten", None
    existing = db.execute("SELECT id FROM learning_experiences WHERE project = ? AND fingerprint = ?", (project, fingerprint)).fetchone()
    if existing is None:
        existing = _near_duplicate(db, project, draft, identifiers)
    if existing is not None:
        before = db.execute("SELECT count(*) FROM learning_experience_sources WHERE experience = ?", (existing[0],)).fetchone()[0]
        for item in sources:
            db.execute("INSERT OR IGNORE INTO learning_experience_sources(experience, session, position, digest, role) VALUES(?,?,?,?,?)",
                       (existing[0], item["session"], item["position"], item["digest"], item["role"]))
        grew = db.execute("SELECT count(*) FROM learning_experience_sources WHERE experience = ?", (existing[0],)).fetchone()[0] > before
        return ("merged" if grew else "duplicate"), existing[0]
    cursor = db.execute(
        "INSERT INTO learning_experiences(project, kind, claim, applies_when, identifiers, fingerprint, basis, detector, revision, worktree, "
        "observed_at, captured_at, validity, tags) VALUES(?,?,?,?,?,?, 'deterministic', ?,?,?,?,?,?,?)",
        (project, draft.kind, draft.claim, draft.applies_when, " ".join(indexed), fingerprint, draft.detector, revision, worktree,
         draft.observed_at or now, now, json.dumps(list(validity)), draft.tags))
    experience = cursor.lastrowid
    for item in sources:
        db.execute("INSERT OR IGNORE INTO learning_experience_sources(experience, session, position, digest, role) VALUES(?,?,?,?,?)",
                   (experience, item["session"], item["position"], item["digest"], item["role"]))
    _relate(db, experience, project, draft, indexed, now)
    return "created", experience


def _near_duplicate(db, project, draft, identifiers):
    claim_words, idents = _words(draft.claim), {token.lower() for token in identifiers}
    for row in db.execute("SELECT id, claim, identifiers FROM learning_experiences WHERE project = ? AND kind = ? AND state = 'retained' ORDER BY id DESC LIMIT 300", (project, draft.kind)):
        if _jaccard(claim_words, _words(row["claim"])) >= NEAR_DUPLICATE_WORDS and _jaccard(idents, {token.lower() for token in row["identifiers"].split()}) >= NEAR_DUPLICATE_IDENTIFIERS:
            return row
    return None


def _relate(db, experience, project, draft, identifiers, now):
    idents = {token.lower() for token in identifiers}
    if not idents:
        return
    for row in db.execute("SELECT id, kind, claim, identifiers, detector, fingerprint, observed_at FROM learning_experiences WHERE project = ? AND id != ? AND state = 'retained' ORDER BY id DESC LIMIT 300", (project, experience)):
        other = {token.lower() for token in row["identifiers"].split()}
        if _jaccard(idents, other) < CONTRADICTION_IDENTIFIERS:
            continue
        mine, theirs = draft.claim, row["claim"]
        if row["detector"] == draft.detector and draft.subject and row["observed_at"] < (draft.observed_at or now):
            subject_old = db.execute("SELECT fingerprint FROM learning_experiences WHERE id = ?", (row["id"],)).fetchone()[0]
            if subject_old == _fingerprint(dataclasses.replace(draft), []) or False:
                continue
        if bool(NEGATIVE.search(mine)) != bool(NEGATIVE.search(theirs)) and bool(POSITIVE.search(mine)) != bool(POSITIVE.search(theirs)):
            relation = "contradicts"
        elif row["detector"] == draft.detector == "resolved_failure" and _words(mine) != _words(theirs):
            relation = "supersedes"
        else:
            continue
        db.execute("INSERT OR IGNORE INTO learning_experience_links(newer, older, relation, created_at) VALUES(?,?,?,?)", (experience, row["id"], relation, now))
        if relation == "supersedes":
            db.execute("UPDATE learning_experiences SET state = 'superseded', state_reason = ?, state_at = ? WHERE id = ?",
                       ("superseded by experience %d" % experience, now, row["id"]))


def retire_stale(db, now, days=RETIRE_AFTER_DAYS):
    cutoff = now - days * 86400
    with db:
        return db.execute(
            "UPDATE learning_experiences SET state = 'retired', state_reason = 'unused single-session historical candidate', state_at = ? "
            "WHERE state = 'retained' AND validity = '[]' AND observed_at < ? AND captured_at < ? "
            "AND (SELECT count(DISTINCT session) FROM learning_experience_sources WHERE experience = learning_experiences.id) <= 1 "
            "AND NOT EXISTS (SELECT 1 FROM learning_receipts r WHERE r.record = 'experience:' || learning_experiences.id)",
            (now, cutoff, cutoff)).rowcount


def session_events(db, session):
    rows = list(db.execute("SELECT position, digest, kind, text, timestamp, call_id, outcome, entry, parent_entry FROM events WHERE session = ? ORDER BY position DESC LIMIT ?", (session, MAX_EVENTS + 1)))
    found = db.execute("SELECT branch_tip FROM sessions WHERE id = ?", (session,)).fetchone()
    tip = found["branch_tip"] if found is not None else None
    return active_branch([dict(row) for row in reversed(rows[:MAX_EVENTS])], tip), len(rows) > MAX_EVENTS


def record_signatures(db, project, session, events, runs, outcomes, now):
    for run in runs:
        if run.out is None or outcomes[id(run)] != "error" or not (run.command and RUNNER.search(run.command)):
            continue
        text = events[run.out]["text"]
        signature = error_signature(text, explicit=events[run.out].get("outcome") == "error")
        if not signature or TRANSIENT.search(text[:3000]):
            continue
        key = command_prefix(run.command) + "|" + signature_key(signature)
        db.execute("INSERT OR IGNORE INTO learning_signatures(project, signature, session, position, digest, command, observed_at) VALUES(?,?,?,?,?,?,?)",
                   (project, key, session, events[run.out]["position"], events[run.out]["digest"], clip(run.command, 160), now))


def recurring_gotchas(db, project, now):
    created = 0
    for row in db.execute("SELECT signature, count(*) AS sessions FROM learning_signatures WHERE project = ? GROUP BY signature HAVING count(*) >= 2", (project,)).fetchall():
        sources = list(db.execute("SELECT session, position, digest, command, observed_at FROM learning_signatures WHERE project = ? AND signature = ? ORDER BY observed_at", (project, row["signature"])))
        command = sources[0]["command"]
        prefix, _, key = row["signature"].partition("|")
        claim = "`%s` fails repeatedly across %d sessions with the same diagnostic (%s)." % (clip(command, 100), len(sources), key)
        draft = Draft("gotcha", clip(claim, MAX_CLAIM), clip("Running `%s`." % prefix, MAX_APPLIES), "recurring_failure", "recurring|" + row["signature"],
                      sources[-1]["observed_at"], [{"position": item["position"], "digest": item["digest"], "role": "trigger", "session": item["session"]} for item in sources[:4]])
        for item in sources[:4]:
            status, _ = add_experience(db, project, dataclasses.replace(draft, sources=[{"position": item["position"], "digest": item["digest"], "role": "trigger"}]), item["session"], now)
            created += status == "created"
    return created


def observe_session(db, project, cwd, session, now):
    events, truncated = session_events(db, session)
    runs = pair_runs(events)
    outcomes = {id(run): run_outcome(events, run) for run in runs}
    drafts = extract(events)
    created = merged = suppressed = 0
    docs = documented_text(cwd)
    revision = _head(cwd)
    for draft in drafts:
        draft.extra = redact(evidence_text(events, draft))
        if documented(draft, docs):
            suppressed += 1
            continue
        if draft.paths:
            validity = file_conditions(cwd, draft.paths, draft.observed_at, now)
        else:
            validity = []
        status, _ = add_experience(db, project, draft, session, now, worktree=str(cwd), revision=revision, validity=validity)
        created += status == "created"
        merged += status == "merged"
    record_signatures(db, project, session, events, runs, outcomes, now)
    return created, merged, suppressed, truncated


def evidence_text(events, draft):
    positions = {item["position"] for item in draft.sources}
    texts = [redact(event["text"][:6000])[:700] for event in events if event["position"] in positions]
    last = max((index for index, event in enumerate(events) if event["position"] in positions), default=None)
    if last is not None:
        following = [redact(event["text"][:4000])[:500] for event in events[last + 1:last + 4] if event["kind"] == "assistant"]
        texts.extend(following[:1])
    return " ".join(texts)


def _head(cwd):
    try:
        result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, timeout=10)
        return result.stdout.decode().strip() if not result.returncode else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def documented_text(cwd):
    text = []
    for name in ("AGENTS.md", "CLAUDE.md", "README.md", "README"):
        path = Path(cwd) / name
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size < 200000:
                text.append(path.read_text(errors="replace"))
        except OSError:
            continue
    return doc_tokens("\n".join(text)) if text else set()


def documented(draft, docs):
    if not docs:
        return False
    parts = {stem(word) for word in re.findall(r"[a-z][a-z0-9]{3,}", draft.claim.lower())}
    identifiers = {token.lower() for token in technical_tokens(draft.claim, 12)}
    if not parts or len(identifiers) == 0:
        return False
    return len(parts & docs) / len(parts) >= 0.6 and len(identifiers & docs) / len(identifiers) >= 0.9


def observe_project(db, project, cwd, now, stable_seconds=120, limit=MAX_SESSIONS_PER_RUN):
    rows = db.execute(
        "SELECT s.id, s.updated FROM sessions s LEFT JOIN learning_observed o ON o.session = s.id "
        "WHERE s.project = ? AND s.updated <= ? AND s.coverage != 'unsupported' AND (o.signature IS NULL OR o.signature != "
        "(SELECT cast(e.position AS TEXT) || ':' || e.digest FROM events e WHERE e.session = s.id ORDER BY e.position DESC LIMIT 1)) "
        "ORDER BY s.updated DESC LIMIT ?", (project, now - stable_seconds, limit)).fetchall()
    totals = {"sessions": 0, "created": 0, "merged": 0, "suppressed": 0}
    for row in rows:
        last = db.execute("SELECT position, digest FROM events WHERE session = ? ORDER BY position DESC LIMIT 1", (row["id"],)).fetchone()
        if last is None:
            continue
        with db:
            created, merged, suppressed, _ = observe_session(db, project, cwd, row["id"], now)
            db.execute("INSERT OR REPLACE INTO learning_observed(session, signature, observed_at, experiences, suppressed) VALUES(?,?,?,?,?)",
                       (row["id"], "%d:%s" % (last["position"], last["digest"]), now, created + merged, suppressed))
        totals["sessions"] += 1
        totals["created"] += created
        totals["merged"] += merged
        totals["suppressed"] += suppressed
    with db:
        totals["recurring"] = recurring_gotchas(db, project, now)
    totals["retired"] = retire_stale(db, now)
    return totals
