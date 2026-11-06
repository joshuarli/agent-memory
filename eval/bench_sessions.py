#!/usr/bin/env python3
import argparse
import contextlib
import importlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK_TMP = ROOT / ".work" / "tmp"
WORK_TMP.mkdir(parents=True, exist_ok=True)
SRC_DIR = ROOT / "agent-memory" / "src"
sys.path.insert(0, str(SRC_DIR))
STORE_SPEC = importlib.util.spec_from_file_location("agent_memory.store", SRC_DIR / "agent_memory" / "store.py")
assert STORE_SPEC is not None
STORE_MODULE = importlib.util.module_from_spec(STORE_SPEC)
assert STORE_SPEC.loader is not None
sys.modules[STORE_SPEC.name] = STORE_MODULE
STORE_SPEC.loader.exec_module(STORE_MODULE)
Store = STORE_MODULE.Store
normalize = STORE_MODULE.normalize
project_identity = STORE_MODULE.project_identity
SKILL_USAGE_PATH = ROOT / "tools/skill-usage/src/skill_usage.py"
SKILL_USAGE_LOADER = importlib.machinery.SourceFileLoader("skill_usage_bench", str(SKILL_USAGE_PATH))
SPEC = importlib.util.spec_from_loader("skill_usage_bench", SKILL_USAGE_LOADER)  # ty: ignore[invalid-argument-type] # typeshed LoaderProtocol still requires legacy load_module, which spec_from_loader never calls
assert SPEC is not None
SKILL_USAGE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = SKILL_USAGE
SPEC.loader.exec_module(SKILL_USAGE)
PERF_GATE = ROOT / "dotfiles" / "base" / ".local" / "bin" / "perf-gate-ready"
QUERY = "retentionmarker"


def _line(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def _make_corpus(root, session_count, message_count, project_count):
    projects = []
    for project_number in range(project_count):
        project = root / ("project-%03d" % project_number)
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        projects.append(project)
    transcripts = root / "sessions"
    transcripts.mkdir()
    event_count = 0
    for session_number in range(session_count):
        session = "bench-%05d" % session_number
        project = projects[session_number % len(projects)]
        path = transcripts / ("rollout-%s.jsonl" % session)
        with path.open("wb") as stream:
            stream.write(_line({"type": "session_meta", "payload": {
                "id": session, "cwd": str(project), "source": "cli", "thread_source": "user",
            }}))
            stream.write(_line({"type": "response_item", "payload": {
                "type": "message", "role": "developer", "content": [{
                    "type": "input_text", "text": "Available skills\n- benchmark: synthetic\n",
                }],
            }}))
            event_count += 2
            for message_number in range(message_count):
                marker = " retentionmarker" if message_number % 10 == 0 else ""
                stream.write(_line({"type": "response_item", "timestamp": "2026-10-23T00:00:00Z", "payload": {
                    "type": "message", "role": "user", "content": [{
                        "type": "input_text", "text": "synthetic prompt session %d message %d%s" % (session_number, message_number, marker),
                    }],
                }}))
                event_count += 1
            stream.write(_line({"type": "response_item", "payload": {
                "type": "function_call", "name": "read_file", "call_id": "call-%d" % session_number,
                "arguments": json.dumps({"path": "/synthetic/.agents/skills/benchmark/SKILL.md"}),
            }}))
            event_count += 1
    return projects, transcripts, event_count


def _direct_search(root, query):
    matches = []
    bytes_read = 0
    for path in sorted(Path(root).rglob("rollout-*.jsonl")):
        session = path.stem.removeprefix("rollout-")
        with path.open("rb") as stream:
            position = 0
            for raw in stream:
                bytes_read += len(raw)
                try:
                    obj = json.loads(raw)
                except ValueError:
                    position += len(raw)
                    continue
                for record in normalize(obj, "codex"):
                    if query.casefold() in record["text"].casefold():
                        matches.append((session, position))
                position += len(raw)
    return matches, bytes_read


def _skill_usage(root):
    usage = SKILL_USAGE.Usage(False)
    SKILL_USAGE.scan_codex(root, 0, usage)
    return len(usage.sessions["codex"]), len(usage.loaded["benchmark"])


def _gate(after=False):
    args = ["sh", str(PERF_GATE)] + (["-1"] if after else [])
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.returncode == 0


class NoModel:
    def review(self, context):
        raise RuntimeError("uneventful benchmark unexpectedly requested a model")


def _learner(store, projects, project_ids):
    learner = importlib.import_module("agent_memory.learning").Learner(store, NoModel())
    with store.db:
        store.db.executemany(
            "INSERT OR REPLACE INTO learning_projects(project,cwd,enabled,enabled_at,repository) VALUES(?,?,1,0,?)",
            ((project_id, str(project), str(ROOT)) for project, project_id in zip(projects, project_ids)),
        )
    return learner


def _work_counters(stats_list):
    return {key: sum(stats[key] for stats in stats_list) for key in ("stat_operations", "content_opens", "bytes_read", "git_processes", "sqlite_changes")}


def _load_baseline(ref, scratch_root):
    result = subprocess.run(
        ["git", "show", ref + ":dotfiles/base/.local/bin/agent_session_store.py"],
        check=True,
        stdout=subprocess.PIPE,
    )
    path = Path(scratch_root) / "agent_session_store_baseline.py"
    path.write_bytes(result.stdout)
    spec = importlib.util.spec_from_file_location("agent_session_store_baseline", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _legacy_refresh(module, store, roots, project_ids, transcripts):
    counters = {"stat_operations": 0, "content_opens": 0, "bytes_read": 0, "git_processes": 0}
    original_open = module._open_source
    original_stat = module.os.stat
    original_fstat = module.os.fstat
    original_run = module.subprocess.run
    source_fds = set()

    class CountedStream:
        def __init__(self, stream):
            self.stream = stream
            self.fd = stream.fileno()
            source_fds.add(self.fd)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            try:
                return self.stream.__exit__(*args)
            finally:
                source_fds.discard(self.fd)

        def readline(self, *args):
            data = self.stream.readline(*args)
            counters["bytes_read"] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def source_path(path):
        try:
            candidate = Path(os.fspath(path)).absolute()
            candidate.relative_to(Path(transcripts).absolute())
            return candidate.name.startswith("rollout-") and candidate.suffix == ".jsonl"
        except (TypeError, ValueError, OSError):
            return False

    def counted_open(path, stats=None):
        stream = original_open(path)
        counters["content_opens"] += 1
        return CountedStream(stream)

    def counted_stat(path, *args, **kwargs):
        if source_path(path):
            counters["stat_operations"] += 1
        return original_stat(path, *args, **kwargs)

    def counted_fstat(fd):
        result = original_fstat(fd)
        if fd in source_fds:
            counters["stat_operations"] += 1
        return result

    def counted_run(args, *call_args, **kwargs):
        is_git = bool(args and args[0] == "git")
        try:
            result = original_run(args, *call_args, **kwargs)
        except subprocess.SubprocessError:
            if is_git:
                counters["git_processes"] += 1
            raise
        else:
            if is_git:
                counters["git_processes"] += 1
            return result

    changes_before = store.db.total_changes
    module._open_source = counted_open
    module.os.stat = counted_stat
    module.os.fstat = counted_fstat
    module.subprocess.run = counted_run
    try:
        for project_id in project_ids:
            store.refresh(roots, project=project_id)
    finally:
        module._open_source = original_open
        module.os.stat = original_stat
        module.os.fstat = original_fstat
        module.subprocess.run = original_run
    counters["sqlite_changes"] = store.db.total_changes - changes_before
    return counters


def _legacy_refresh_plain(store, roots, project_ids):
    return sum(store.refresh(roots, project=project_id)["events_added"] for project_id in project_ids)


def _timed(operation, action, timings, deferred):
    if not _gate():
        deferred[0] = True
        return None
    started = time.perf_counter()
    cpu_started = time.process_time()
    result = action()
    elapsed = time.perf_counter() - started
    cpu_elapsed = time.process_time() - cpu_started
    if not _gate(after=True):
        deferred[0] = True
        return result
    timings.setdefault(operation, []).append({"wall": elapsed, "cpu": cpu_elapsed})
    return result


def _summary(values):
    if not values:
        return None
    output = {"runs": len(values)}
    for key, suffix in (("wall", "wall"), ("cpu", "cpu")):
        samples = [value[key] for value in values]
        output[suffix + "_median_ms"] = round(statistics.median(samples) * 1000, 3)
        output[suffix + "_range_ms"] = [round(min(samples) * 1000, 3), round(max(samples) * 1000, 3)]
    return output


def run(args):
    if args.sessions < 1 or args.messages < 1 or args.projects < 1:
        raise ValueError("sessions, messages, and projects must be positive")
    with tempfile.TemporaryDirectory(dir=str(WORK_TMP), prefix="agent-sessions-bench-") as temporary:
        root = Path(temporary)
        projects, transcripts, generated_events = _make_corpus(root, args.sessions, args.messages, min(args.projects, args.sessions))
        project_ids = [project_identity(project) for project in projects]
        mixed_count = max(1, len(project_ids) // 2)
        mixed_project_ids = project_ids[:mixed_count]
        source_bytes = sum(path.stat().st_size for path in transcripts.rglob("rollout-*.jsonl"))
        direct, direct_bytes = _direct_search(transcripts, QUERY)
        usage_sessions, usage_loads = _skill_usage(transcripts)
        state = root / "state"
        baseline_module = _load_baseline(args.baseline_ref, root) if args.baseline_ref else None
        baseline_state = root / "baseline-state"
        baseline_context = baseline_module.Store(baseline_state) if baseline_module else contextlib.nullcontext(None)
        baseline_work = {}
        mixed_state = root / "mixed-state"
        mixed_baseline_state = root / "mixed-baseline-state"
        mixed_baseline_context = baseline_module.Store(mixed_baseline_state) if baseline_module else contextlib.nullcontext(None)
        with Store(mixed_state) as mixed_store, mixed_baseline_context as mixed_baseline:
            mixed_fresh = mixed_store.refresh([transcripts], projects=mixed_project_ids)
            if mixed_baseline:
                baseline_work["mixed_enabled_initial"] = _legacy_refresh(
                    baseline_module, mixed_baseline, [transcripts], mixed_project_ids, transcripts
                )
            expected_mixed_sessions = sum(index % len(project_ids) < mixed_count for index in range(args.sessions))
            if mixed_fresh["sessions_indexed"] != expected_mixed_sessions:
                raise RuntimeError("mixed enabled-project refresh indexed an unexpected session count")
            mixed_noop = mixed_store.refresh([transcripts], projects=mixed_project_ids)
            if mixed_baseline:
                baseline_work["mixed_enabled_noop"] = _legacy_refresh(
                    baseline_module, mixed_baseline, [transcripts], mixed_project_ids, transcripts
                )
            if mixed_noop["content_opens"] or mixed_noop["bytes_read"] or mixed_noop["git_processes"] or mixed_noop["sqlite_changes"]:
                raise RuntimeError("mixed enabled-project no-op repeated content or database work")
            mixed_database_bytes = mixed_store.path.stat().st_size
            mixed_baseline_database_bytes = mixed_baseline.path.stat().st_size if mixed_baseline else None
        with Store(state) as store, baseline_context as baseline_store:
            fresh = store.refresh([transcripts], projects=project_ids)
            if baseline_store:
                baseline_work["fresh"] = _legacy_refresh(baseline_module, baseline_store, [transcripts], project_ids, transcripts)
                if baseline_store.db.execute("SELECT count(*) FROM sessions").fetchone()[0] != args.sessions:
                    raise RuntimeError("baseline refresh indexed an unexpected session count")
                if baseline_store.db.execute("SELECT count(*) FROM events").fetchone()[0] != generated_events:
                    raise RuntimeError("baseline refresh indexed an unexpected event count")
            indexed = store.search(QUERY, limit=args.sessions * args.messages)
            indexed_keys = {(row["session"], row["position"]) for row in indexed}
            if set(direct) != indexed_keys:
                raise RuntimeError("direct scan and FTS search returned different source events")
            if indexed:
                hit = indexed[0]
                evidence = store.resolve_evidence(hit["session"], hit["position"], hit["digest"])
                if QUERY not in evidence["text"].casefold():
                    raise RuntimeError("resolved evidence does not contain the indexed query")
            database_bytes = store.path.stat().st_size
            target = transcripts / "rollout-bench-00000.jsonl"
            appended = _line({"type": "response_item", "timestamp": "2026-10-23T00:00:01Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "incrementmarker"}],
            }})
            with target.open("ab") as stream:
                stream.write(appended)
            append = store.refresh([transcripts], projects=project_ids)
            if baseline_store:
                baseline_work["append"] = _legacy_refresh(baseline_module, baseline_store, [transcripts], project_ids, transcripts)
            unchanged = store.refresh([transcripts], projects=project_ids)
            if baseline_store:
                baseline_work["unchanged"] = _legacy_refresh(baseline_module, baseline_store, [transcripts], project_ids, transcripts)
            many_projects = store.refresh([transcripts], projects=project_ids)
            if baseline_store:
                baseline_work["many_enabled_projects"] = _legacy_refresh(baseline_module, baseline_store, [transcripts], project_ids, transcripts)
            if append["events_added"] != 1 or unchanged["events_added"] != 0:
                raise RuntimeError("append or unchanged refresh processed the wrong number of events")
            if len(store.search(QUERY, limit=args.sessions * args.messages)) != len(direct):
                raise RuntimeError("FTS result count changed after unrelated append")
            learner = _learner(store, projects, project_ids)
            first_reconcile = learner.reconcile(time.time()+1000, roots=[transcripts], stable_seconds=0)
            while first_reconcile["processed"]:
                first_reconcile = learner.reconcile(time.time()+1000, roots=[transcripts], stable_seconds=0)
            learning_noop = learner.reconcile(time.time()+1000, roots=[transcripts], stable_seconds=0)
            if learning_noop != {"processed": 0, "signals": 0, "reviews": 0, "experiences": 0}:
                raise RuntimeError("learning no-op repeated work")
            missed_hook = transcripts / "rollout-missed-hook.jsonl"
            missed_hook.write_bytes(_line({"type": "session_meta", "payload": {
                "id": "missed-hook", "cwd": str(projects[0]), "source": "cli", "thread_source": "user",
            }}) + _line({"type": "response_item", "timestamp": "2026-10-23T00:00:03Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic prompt for a stable transcript"}],
            }}))
            if baseline_store:
                baseline_work["missed_hook_reconciliation"] = _legacy_refresh(
                    baseline_module, baseline_store, [transcripts], project_ids, transcripts
                )
            observed_refreshes = []
            original_refresh = store.refresh
            def observe_refresh(*refresh_args, **refresh_kwargs):
                stats = original_refresh(*refresh_args, **refresh_kwargs)
                observed_refreshes.append(stats)
                return stats
            store.refresh = observe_refresh
            missed_reconcile = learner.reconcile(time.time()+1000, roots=[transcripts], stable_seconds=0)
            store.refresh = original_refresh
            if missed_reconcile["processed"] != 1 or not observed_refreshes:
                raise RuntimeError("missed-hook reconciliation did not process the unindexed session")
            database_bytes_with_learning = store.path.stat().st_size
            final_database_bytes = store.path.stat().st_size
        results = {
            "corpus": {"sessions": args.sessions, "projects": len(projects), "messages_per_session": args.messages, "events": generated_events, "source_bytes": source_bytes},
            "correctness": {"direct_hits": len(direct), "fts_hits": len(indexed), "direct_scan_bytes": direct_bytes, "skill_usage_sessions": usage_sessions, "skill_usage_loads": usage_loads},
            "refresh_work": {
                "fresh": _work_counters([fresh]),
                "append": _work_counters([append]),
                "unchanged": _work_counters([unchanged]),
                "many_enabled_projects": _work_counters([many_projects]),
                "mixed_enabled_noop": _work_counters([mixed_noop]),
                "missed_hook_reconciliation": _work_counters(observed_refreshes),
            },
            "baseline_ref": args.baseline_ref,
            "baseline_refresh_work": baseline_work or None,
            "index": {"build_sessions": fresh["sessions_indexed"], "build_events_added": fresh["events_added"], "increment_events_added": append["events_added"], "unchanged_events_added": unchanged["events_added"], "database_bytes": database_bytes, "database_bytes_after_increment": final_database_bytes, "database_bytes_with_learning_tables": database_bytes_with_learning},
            "mixed_history_database_bytes": {"candidate": mixed_database_bytes, "baseline": mixed_baseline_database_bytes},
            "learning_no_op": learning_noop,
            "missed_hook_reconcile": missed_reconcile,
        }
        if args.timings:
            timings = {}
            deferred = [False]
            if baseline_module:
                with Store(state) as timed_store, baseline_module.Store(baseline_state) as timed_baseline:
                    for index in range(5):
                        variants = (
                            ("baseline_noop_per_project", lambda: _legacy_refresh_plain(timed_baseline, [transcripts], project_ids)),
                            ("shared_projects_noop", lambda: timed_store.refresh([transcripts], projects=project_ids)["events_added"]),
                        )
                        if index % 2:
                            variants = tuple(reversed(variants))
                        for name, action in variants:
                            result = _timed(name, action, timings, deferred)
                            if result is not None and result != 0:
                                raise RuntimeError("timed comparison no-op changed the index")
            for index in range(5):
                build_state = root / ("timed-build-%d" % index)
                def build_once(path=build_state):
                    with Store(path) as timed_store:
                        return timed_store.refresh([transcripts], projects=project_ids)["events_added"]
                built_events = _timed("fresh", build_once, timings, deferred)
                if built_events is not None and built_events != generated_events + 3 + 3 * index:
                    raise RuntimeError("timed fresh refresh indexed an unexpected number of events")
                appended_line = _line({"type": "response_item", "timestamp": "2026-10-23T00:00:04Z", "payload": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": "timedincrement%d" % index}],
                }})
                with target.open("ab") as stream:
                    stream.write(appended_line)
                with Store(state) as timed_store:
                    incremented = _timed("append", lambda: timed_store.refresh([transcripts], projects=project_ids)["events_added"], timings, deferred)
                    if incremented is None:
                        incremented = timed_store.refresh([transcripts], projects=project_ids)["events_added"]
                    if incremented != 1:
                        raise RuntimeError("timed append indexed an unexpected event count")
                    unchanged_events = _timed("unchanged", lambda: timed_store.refresh([transcripts], projects=project_ids)["events_added"], timings, deferred)
                    if unchanged_events is not None and unchanged_events != 0:
                        raise RuntimeError("timed unchanged refresh processed events")
                    multi_events = _timed("many_enabled_projects", lambda: timed_store.refresh([transcripts], projects=project_ids)["events_added"], timings, deferred)
                    if multi_events is not None and multi_events != 0:
                        raise RuntimeError("timed multi-project refresh processed events")
                    timed_learner = _learner(timed_store, projects, project_ids)
                    timed_learner.reconcile(time.time()+1000, roots=[transcripts], stable_seconds=0)
                    missed_path = transcripts / ("rollout-missed-%d.jsonl" % index)
                    missed_path.write_bytes(_line({"type": "session_meta", "payload": {
                        "id": "missed-%d" % index, "cwd": str(projects[0]), "source": "cli", "thread_source": "user",
                    }}) + _line({"type": "response_item", "timestamp": "2026-10-23T00:00:05Z", "payload": {
                        "type": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic missed hook"}],
                    }}))
                    reconciled = _timed("missed_hook_reconciliation", lambda timed_learner=timed_learner: timed_learner.reconcile(time.time()+1000, roots=[transcripts], stable_seconds=0), timings, deferred)
                    if reconciled is not None and reconciled["processed"] != 1:
                        raise RuntimeError("timed missed-hook reconciliation processed an unexpected count")
                shutil.rmtree(build_state, ignore_errors=True)
            results["timings"] = {name: _summary(values) for name, values in timings.items()}
            results["timing_status"] = "perf gate deferred: host busy" if deferred[0] else "gate passed"
        else:
            results["timing_status"] = "not requested"
        print(json.dumps(results, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure incremental session discovery and indexing work.")
    parser.add_argument("--sessions", type=int, default=1000)
    parser.add_argument("--messages", type=int, default=10)
    parser.add_argument("--projects", type=int, default=20)
    parser.add_argument("--baseline-ref", help="compare with a committed agent_session_store.py revision")
    parser.add_argument("--timings", action="store_true", help="run five perf-gated samples per case")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print("agent-sessions-bench: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
