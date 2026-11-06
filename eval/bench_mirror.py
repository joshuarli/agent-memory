#!/usr/bin/env python3
"""Measure the mirror's local hot paths and the Worker over real HTTP."""
import argparse
import hashlib
import http.client
import json
import os
import platform
import resource
import select
import shutil
import ssl
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "agent-memory" / "src"
WORK_TMP = ROOT / ".work" / "tmp"
PERF_GATE = ROOT / "dotfiles" / "base" / ".local" / "bin" / "perf-gate-ready"
SCHEMA_VERSION = 1
QUERY = "retentionmarker"
sys.path.insert(0, str(SRC_DIR))

from agent_memory import learning, learning_sync, mirror_crypto  # noqa: E402
from agent_memory.store import Store, project_identity  # noqa: E402

DEFERRED = {
    "recall": "P5.3",
    "inspect": "P3.3",
    "learning-delta": "P3.1",
    "mirror-noop": "P2.3",
    "mirror-append": "P2.3",
    "recovery": "P2.4",
    "html": "P4.1",
    "mirror-concurrent": "P5.2",
}
LOCAL_SCENARIOS = ("capture", "learning-import", "mirror-initial")


def _line(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _percentile(values, fraction):
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(samples):
    walls = [sample["wall_ms"] for sample in samples if sample["wall_ms"] is not None]
    cpus = [sample["cpu_ms"] for sample in samples if sample["cpu_ms"] is not None]
    entry = {"runs": len(walls)}
    if not walls:
        return entry
    median = statistics.median(walls)
    entry.update({
        "median": round(median, 3),
        "p50": round(_percentile(walls, 0.50), 3),
        "p95": round(_percentile(walls, 0.95), 3),
        "p99": round(_percentile(walls, 0.99), 3),
        "min": round(min(walls), 3),
        "max": round(max(walls), 3),
        "spread": round(_percentile(walls, 0.95) - _percentile(walls, 0.50), 3),
        "mad": round(statistics.median([abs(value - median) for value in walls]), 3),
    })
    if cpus:
        entry.update({
            "cpu_median": round(statistics.median(cpus), 3),
            "cpu_p95": round(_percentile(cpus, 0.95), 3),
            "cpu_min": round(min(cpus), 3),
            "cpu_max": round(max(cpus), 3),
        })
    return entry


def _peak_rss_kib():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak // 1024 if sys.platform == "darwin" else peak


def _version(argv):
    executable = shutil.which(argv[0])
    if executable is None:
        return None
    try:
        result = subprocess.run([executable, *argv[1:]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else None


def _git(*args):
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _runtime():
    return {
        "python": platform.python_version(),
        "node": _version(["node", "--version"]),
        "workerd": None,
        "rage": _version([os.environ.get("AGE_BIN", "rage"), "--version"]) or _version(["age", "--version"]),
    }


def _cores():
    try:
        return os.cpu_count() or 1
    except OSError:
        return 1


def _load1():
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


def _gate():
    try:
        result = subprocess.run(["sh", str(PERF_GATE), "-1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _timed(action, repeat, gate):
    load_before = _load1()
    before = gate()
    started = time.perf_counter()
    cpu_started = time.process_time()
    counters = action()
    timings = counters.pop("_timings", {}) if isinstance(counters, dict) else {}
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    cpu_ms = round((time.process_time() - cpu_started) * 1000, 3)
    after = gate()
    load_after = _load1()
    passed = before and after
    return {
        "repeat": repeat,
        "wall_ms": wall_ms if passed else None,
        "cpu_ms": cpu_ms if passed else None,
        "counters": counters,
        "timings": timings,
        "gate": {
            "before": before,
            "after": after,
            "load1": [None if load_before is None else round(load_before, 2),
                      None if load_after is None else round(load_after, 2)],
        },
    }


def _make_sessions(root, session_count, message_count, project_count):
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


def _work(stats):
    return {key: stats[key] for key in ("bytes_read", "content_opens", "git_processes", "sqlite_changes", "stat_operations")}


def _capture(scenario, args, root):
    samples = []
    dataset = None
    for repeat in range(args.repeats):
        repeat_root = root / ("repeat-%d" % repeat)
        repeat_root.mkdir()
        projects, transcripts, events = _make_sessions(repeat_root, args.sessions, args.messages, min(args.projects, args.sessions))
        project_ids = [project_identity(project) for project in projects]
        source_bytes = sum(path.stat().st_size for path in transcripts.rglob("rollout-*.jsonl"))
        if dataset is None:
            dataset = {
                "sessions": args.sessions, "messages_per_session": args.messages, "projects": len(projects),
                "events": events, "source_bytes": source_bytes,
            }
        store = Store(repeat_root / "state")
        try:
            fresh = _timed(lambda store=store, transcripts=transcripts, project_ids=project_ids: _work(store.refresh([transcripts], projects=project_ids)), repeat, _gate)
            target = transcripts / "rollout-bench-00000.jsonl"
            with target.open("ab") as stream:
                stream.write(_line({"type": "response_item", "timestamp": "2026-10-23T00:00:01Z", "payload": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": "incrementmarker"}],
                }}))
            append = _timed(lambda store=store, transcripts=transcripts, project_ids=project_ids: _work(store.refresh([transcripts], projects=project_ids)), repeat, _gate)
            unchanged = _timed(lambda store=store, transcripts=transcripts, project_ids=project_ids: _work(store.refresh([transcripts], projects=project_ids)), repeat, _gate)
        finally:
            store.close()
        for operation, sample in (("fresh", fresh), ("append", append), ("unchanged", unchanged)):
            sample["operation"] = operation
            samples.append(sample)
    return dataset, samples, ("fresh", "append", "unchanged")


def _seed_learning(store, records):
    experiences = []
    memories = []
    sources = []
    incidents = []
    links = []
    now = 1_767_225_600.0
    for index in range(records):
        project = "git:/synthetic/project-%02d" % (index % 10)
        digest = hashlib.sha256(("source-%d" % index).encode()).hexdigest()
        session = "bench-%05d" % (index % 200)
        if index % 10 == 0:
            memories.append((index, project, "synthetic action %d retentionmarker" % index,
                             "when synthetic condition %d holds" % index, "synthetic-revision",
                             "approved", now + index))
            incidents.append((index, session, session, index, digest, "synthetic", now + index))
        else:
            experiences.append((
                index, project, "gotcha", "synthetic observation %d retentionmarker" % index,
                "synthetic condition %d" % index, "synthetic-identifier-%d" % index,
                hashlib.sha256(("fingerprint-%d" % index).encode()).hexdigest(),
                "deterministic", "synthetic-detector", "synthetic-revision", None,
                now + index, now + index, "[]", "", "retained", None, None,
            ))
            sources.append((index, session, index, digest, "trigger"))
            if index % 50 == 3:
                links.append((index, index - 2, "supersedes", now + index))
    learning.initialize(store.db)
    db = store.db
    with db:
        db.executemany(
            "INSERT INTO learning_findings(id, project, signal, fingerprint, terms, state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ((finding, project, "synthetic", "fingerprint-%d" % finding, "synthetic", "memory", now, now)
             for finding, project, *_rest in memories))
        db.executemany(
            "INSERT INTO learning_experiences(id, project, kind, claim, applies_when, identifiers, fingerprint, basis, detector, revision, worktree, observed_at, captured_at, validity, tags, state, state_reason, state_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", experiences)
        db.executemany(
            "INSERT INTO learning_memories(finding, project, action, applicability, revision, state, created_at) VALUES(?,?,?,?,?,?,?)",
            memories)
        db.executemany(
            "INSERT INTO learning_experience_sources(experience, session, position, digest, role) VALUES(?,?,?,?,?)", sources)
        db.executemany(
            "INSERT INTO learning_experience_links(newer, older, relation, created_at) VALUES(?,?,?,?)", links)
        db.executemany(
            "INSERT INTO learning_incidents(finding, session, root_session, position, digest, signal, observed_at) VALUES(?,?,?,?,?,?,?)",
            incidents)
    return {"records": records, "experiences": len(experiences), "memories": len(memories),
            "sources": len(sources) + len(incidents), "links": len(links)}


def _learning_import(scenario, args, root):
    state = root / "state"
    store = Store(state)
    try:
        dataset = _seed_learning(store, args.records)
        samples = []
        for repeat in range(args.repeats):
            def export():
                started = time.perf_counter()
                records = learning_sync.export_snapshot(store.db)
                exported_ms = (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                body = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                return {
                    "records": len(records), "bytes": len(body.encode("utf-8")), "digest": digest,
                    "_timings": {
                        "export_ms": round(exported_ms, 3),
                        "serialization_ms": round((time.perf_counter() - started) * 1000, 3),
                        "records_per_second": round(len(records) / (exported_ms / 1000), 1) if exported_ms else None,
                    },
                }
            sample = _timed(export, repeat, _gate)
            sample["operation"] = "export"
            samples.append(sample)
    finally:
        store.close()
    return dataset, samples, ("export",)


def _mirror_initial(scenario, args, root):
    try:
        mirror_crypto.binary()
    except mirror_crypto.CryptoError as exc:
        raise RuntimeError(str(exc)) from exc
    identity = root / "collector.key"
    recipient = mirror_crypto.keygen(identity)
    recipients = [recipient]
    per_class = max(1, args.objects)
    size = 64 * 1024
    lines = [("synthetic transcript message %d with varying identifiers and prose for the mirror\n" % index).encode()
             for index in range(512)]
    corpus = b"".join(lines)
    text = (corpus * (size // len(corpus) + 1))[:size]
    classes = {"chunk": text, "artifact": os.urandom(size)}
    samples = []
    for repeat in range(args.repeats):
        def prepare():
            counters = {
                "objects": 0, "bytes_plaintext": 0, "bytes_compressed": 0,
                "zlib_objects": 0, "classes": {},
            }
            timings = {"bytes_ciphertext": 0, "compress_cpu_ms": 0.0, "encrypt_cpu_ms": 0.0, "classes": {}}
            for kind, data in classes.items():
                entry = counters["classes"].setdefault(kind, {
                    "objects": 0, "bytes_plaintext": 0, "bytes_compressed": 0, "zlib_objects": 0,
                })
                timings["classes"].setdefault(kind, {
                    "bytes_ciphertext": 0, "compress_cpu_ms": 0.0, "encrypt_cpu_ms": 0.0,
                })
                for _index in range(per_class):
                    prepared = mirror_crypto.prepare_object(data, recipients)
                    counters["objects"] += 1
                    counters["bytes_plaintext"] += prepared.plaintext_size
                    counters["bytes_compressed"] += prepared.compressed_size
                    timings["bytes_ciphertext"] += prepared.ciphertext_size
                    counters["zlib_objects"] += 1 if prepared.encoding == "zlib" else 0
                    entry["objects"] += 1
                    entry["bytes_plaintext"] += prepared.plaintext_size
                    entry["bytes_compressed"] += prepared.compressed_size
                    timings["classes"][kind]["bytes_ciphertext"] += prepared.ciphertext_size
                    entry["zlib_objects"] += 1 if prepared.encoding == "zlib" else 0
                    timings["compress_cpu_ms"] += prepared.compress_cpu_ms
                    timings["encrypt_cpu_ms"] += prepared.encrypt_cpu_ms
                    timings["classes"][kind]["compress_cpu_ms"] += prepared.compress_cpu_ms
                    timings["classes"][kind]["encrypt_cpu_ms"] += prepared.encrypt_cpu_ms
            counters["compression_ratio"] = round(
                counters["bytes_compressed"] / counters["bytes_plaintext"], 6
            ) if counters["bytes_plaintext"] else 1.0
            for entry in counters["classes"].values():
                entry["compression_ratio"] = round(
                    entry["bytes_compressed"] / entry["bytes_plaintext"], 6
                ) if entry["bytes_plaintext"] else 1.0
            counters["_timings"] = timings
            return counters
        sample = _timed(prepare, repeat, _gate)
        sample["operation"] = "prepare"
        samples.append(sample)
    dataset = {
        "objects_per_class": per_class, "classes": sorted(classes), "object_bytes": size,
        "zlib_class": "chunk", "identity_class": "artifact",
    }
    return dataset, samples, ("prepare",)


def _document(environment, scenario, dataset, samples, operations, runtime=None):
    moment = _now()
    counters = {}
    variance = []
    for operation in operations:
        seen = [sample["counters"] for sample in samples if sample["operation"] == operation]
        counters[operation] = seen[0] if seen else {}
        if any(item != seen[0] for item in seen):
            variance.append(operation)
    timed = [sample for sample in samples if sample["wall_ms"] is not None]
    return {
        "schema_version": SCHEMA_VERSION,
        "commit": _git("rev-parse", "HEAD"),
        "generated_at": _iso(moment),
        "environment": environment,
        "scenario": scenario,
        "runtime": {**(runtime or _runtime()), "dirty": bool(_git("status", "--porcelain", "--untracked-files=no"))},
        "dataset": dataset,
        "perf_gate": "passed" if len(timed) == len(samples) and all(
            summary.get("runs", 0) >= 5 for summary in
            [_summary([sample for sample in samples if sample["operation"] == operation]) for operation in operations]
        ) else "deferred",
        "load": {"source": "perf-gate-ready", "cores": _cores(), "max_load1": max(
            (sample["gate"]["load1"][0] or 0) for sample in samples) if samples else 0},
        "samples": samples,
        "summary": {operation: _summary([sample for sample in samples if sample["operation"] == operation])
                    for operation in operations},
        "counters": counters,
        "peak_rss_kib": _peak_rss_kib(),
        "counter_variance": variance,
    }


def _write(document, out):
    out.mkdir(parents=True, exist_ok=True)
    path = out / ("%s-%s-%s.json" % (document["environment"], document["scenario"], _stamp(_now())))
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


def _load(path):
    document = json.loads(Path(path).read_text())
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported benchmark schema in " + str(path))
    return document


def _threshold(baseline):
    return max(baseline["median"] * 0.05, baseline["mad"] * 2)


def _compare(baseline, candidate):
    if baseline["scenario"] != candidate["scenario"]:
        raise ValueError("benchmark scenarios differ: %s and %s" % (baseline["scenario"], candidate["scenario"]))
    verdict = {"scenario": baseline["scenario"], "environment": baseline["environment"], "passed": True,
               "deferred": baseline["perf_gate"] != "passed" or candidate["perf_gate"] != "passed", "violations": []}
    for operation, base_counters in baseline["counters"].items():
        candidate_counters = candidate["counters"].get(operation)
        if candidate_counters != base_counters:
            verdict["passed"] = False
            verdict["violations"].append({"operation": operation, "kind": "counters", "baseline": base_counters,
                                          "candidate": candidate_counters})
    for operation, base in baseline["summary"].items():
        candidate_summary = candidate["summary"].get(operation)
        if not base.get("runs") or not candidate_summary or not candidate_summary.get("runs"):
            verdict["deferred"] = True
            continue
        allowed = base["median"] + _threshold(base)
        if candidate_summary["median"] > allowed:
            verdict["passed"] = False
            verdict["violations"].append({
                "operation": operation, "kind": "wall", "baseline_median": base["median"],
                "candidate_median": candidate_summary["median"], "allowed": round(allowed, 3),
                "baseline_mad": base["mad"],
            })
    if verdict["deferred"]:
        verdict["passed"] = False
    return verdict


def _print_compare(verdict):
    print("scenario %s (%s): %s" % (verdict["scenario"], verdict["environment"],
                                    "deferred" if verdict["deferred"] else ("pass" if verdict["passed"] else "fail")))
    for violation in verdict["violations"]:
        if violation["kind"] == "wall":
            print("  %s: %.3f ms -> %.3f ms (allowed <= %.3f)" % (
                violation["operation"], violation["baseline_median"], violation["candidate_median"], violation["allowed"]))
        else:
            print("  %s: deterministic counters changed" % violation["operation"])


def _report(directory):
    documents = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            documents.append((path, _load(path)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print("skipping %s: %s" % (path, exc), file=sys.stderr)
    groups = {}
    for path, document in documents:
        groups.setdefault((document["environment"], document["scenario"]), []).append((document["generated_at"], path, document))
    if not groups:
        print("no benchmark results under %s" % directory)
        return 1
    for (environment, scenario), runs in sorted(groups.items()):
        runs.sort()
        print("## %s %s" % (environment, scenario))
        print("| operation | runs | median ms | p95 ms | counters |")
        print("| --- | ---: | ---: | ---: | --- |")
        latest = runs[-1][2]
        for operation, summary in latest["summary"].items():
            counters = json.dumps(latest["counters"].get(operation, {}), sort_keys=True)
            print("| %s | %s | %s | %s | %s |" % (operation, summary.get("runs", 0), summary.get("median", "-"),
                                                 summary.get("p95", "-"), counters))
        if len(runs) > 1:
            verdict = _compare(runs[0][2], latest)
            print("")
            _print_compare(verdict)
        print("")
    return 0


def _bench_http(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        connection.request(method, path, headers=headers or {}, body=body)
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def _bench_counters(port):
    status, _headers, body = _bench_http(port, "GET", "/__bench/status")
    if status != 200:
        raise RuntimeError("worker bench counter request failed")
    return json.loads(body)


def _worker_server():
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is required for worker benchmarks")
    worker_dir = ROOT / "agent-memory" / "cloudflare"
    bundle = worker_dir / ".cloudflare" / "output" / "v0" / "workers" / "default" / "bundle" / "index.js"
    if not bundle.is_file():
        raise RuntimeError("missing worker bundle; run `npm run build` in agent-memory/cloudflare")
    process = subprocess.Popen([node, "test/serve.mjs"], cwd=worker_dir, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("worker bench server has no output stream")
    ready, _, _ = select.select([stdout], [], [], 30)
    if not ready:
        process.kill()
        _, stderr = process.communicate(timeout=10)
        raise RuntimeError("worker bench server did not start: " + (stderr.strip() or "timeout"))
    line = stdout.readline()
    try:
        port = int(json.loads(line)["port"])
    except (ValueError, KeyError, TypeError):
        process.kill()
        _, stderr = process.communicate(timeout=10)
        raise RuntimeError("worker bench server did not start: " + (stderr.strip() or line.strip())) from None
    return process, port


def _worker(args):
    scenario = args.scenario or "status"
    if scenario not in ("status", "search"):
        owner = DEFERRED.get(scenario, "a later slice")
        print("bench_mirror: worker scenario %s lands in %s" % (scenario, owner), file=sys.stderr)
        return 2
    if args.repeats < 1:
        raise ValueError("repeats must be positive")
    process, port = _worker_server()
    try:
        auth_status, _headers, _body = _bench_http(port, "GET", "/api/v1/status")
        if auth_status != 403:
            raise RuntimeError("unauthenticated worker request was not refused")
        _bench_http(port, "POST", "/__bench/reset")
        if scenario == "search":
            for offset in range(0, args.records, 10000):
                count = min(10000, args.records - offset)
                seeded, _headers, body = _bench_http(
                    port, "POST", "/__bench/seed-learning", {"content-type": "application/json"},
                    json.dumps({"records": count, "offset": offset, "total": args.records}).encode())
                if seeded != 200:
                    raise RuntimeError("could not seed the learning projection: " + body.decode("utf-8", "replace"))
            operations = (
                ("search-page", "/api/v1/memories?limit=20&state=retained"),
                ("search-fts", "/api/v1/memories?limit=20&state=retained&q=" + QUERY),
            )
        else:
            operations = (("status", "/api/v1/status"),)
        for _name, path in operations:
            _bench_http(port, "GET", path, {"x-bench-principal": "reader"})
        _bench_http(port, "POST", "/__bench/reset")
        samples = []
        for repeat in range(args.repeats):
            for operation, path in operations:
                before = _bench_counters(port)
                sample = _timed(lambda port=port, path=path: _worker_get(port, path), repeat, _gate)
                sample["operation"] = operation
                after = _bench_counters(port)
                delta = {}
                for key in set(before["counters"]) | set(after["counters"]):
                    delta[key] = after["counters"].get(key, 0) - before["counters"].get(key, 0)
                delta["requests"] = after["requests"] - before["requests"]
                if delta["requests"] != 1:
                    raise RuntimeError("worker request counter disagrees with the harness")
                raw_counters = sample.get("counters")
                counters = raw_counters if isinstance(raw_counters, dict) else {}
                raw_timings = sample.get("timings")
                timings = raw_timings if isinstance(raw_timings, dict) else {}
                timing = counters.pop("server_timing", None)
                if timing is not None:
                    timings["server_timing"] = timing
                counters.update(delta)
                sample["counters"] = counters
                sample["timings"] = timings
                samples.append(sample)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    dataset = {"requests": args.repeats * len(operations), "auth_refusals": 1, "warmups": len(operations)}
    if scenario == "search":
        dataset.update({"records": args.records, "projects": 100, "sources": args.records})
    document = _document("miniflare", scenario, dataset, samples, tuple(name for name, _path in operations),
                         runtime={**_runtime(), "workerd": "2026-08-06", "miniflare": "4.20260730.0"})
    _write(document, Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out)
    print(json.dumps(document, indent=2, sort_keys=True))
    variance = document.get("counter_variance")
    if isinstance(variance, list) and variance:
        print("bench_mirror: deterministic counters varied across repeats: " + ", ".join(str(item) for item in variance),
              file=sys.stderr)
        return 1
    return 0


def _deployed_token(path):
    if not path.is_file():
        raise RuntimeError("deployed benchmark token file is missing: " + str(path))
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("deployed benchmark token file must have mode 0600")
    value = json.loads(path.read_text())
    client_id, client_secret = value.get("client_id"), value.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise RuntimeError("deployed benchmark token file has no client id and secret")
    return client_id, client_secret


_TLS_CONTEXT = None


def _tls_context():
    global _TLS_CONTEXT
    if _TLS_CONTEXT is None:
        _TLS_CONTEXT = ssl.create_default_context()
    return _TLS_CONTEXT


def _deployed_connection(base):
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise RuntimeError("deployed benchmark base must be an https origin")
    connection = http.client.HTTPSConnection(
        parts.hostname, parts.port or 443, timeout=120, context=_tls_context())
    started = time.perf_counter()
    connection.connect()
    connected = time.perf_counter()
    return connection, round((connected - started) * 1000, 3)


def _deployed_request(connection, path, token=None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["CF-Access-Client-Id"], headers["CF-Access-Client-Secret"] = token
    started = time.perf_counter()
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    headers_done = time.perf_counter()
    payload = response.read()
    done = time.perf_counter()
    results = None
    if b'"memories"' in payload:
        try:
            results = len(json.loads(payload)["memories"])
        except (ValueError, KeyError, TypeError):
            results = None
    return {
        "status": response.status, "headers": {key.lower(): value for key, value in response.getheaders()},
        "bytes": len(payload), "results": results, "timings": {
            "headers_ms": round((headers_done - started) * 1000, 3),
            "body_ms": round((done - headers_done) * 1000, 3),
        },
    }


def _server_timing(value) -> dict[str, Any]:
    parsed = {}
    for item in (value or "").split(","):
        parts = [entry.strip() for entry in item.split(";")]
        if not parts or not parts[0]:
            continue
        metric = {"dur": None}
        for entry in parts[1:]:
            name, _separator, raw = entry.partition("=")
            if name == "dur":
                try:
                    metric["dur"] = float(raw)
                except ValueError:
                    metric["dur"] = None
        parsed[parts[0]] = metric
    return parsed


def _deployed(args):
    scenario = args.scenario or "status"
    if scenario not in ("status", "search"):
        owner = DEFERRED.get(scenario, "a later slice")
        print("bench_mirror: deployed scenario %s lands in %s" % (scenario, owner), file=sys.stderr)
        return 2
    if args.repeats < 1:
        raise ValueError("repeats must be positive")
    base = os.environ.get("MEMORIES_BASE", "https://memories.17166969.xyz").rstrip("/")
    token_path = Path(os.environ.get("MEMORIES_TOKEN_FILE", ROOT / ".work" / "secrets" / "collector.token"))
    token = _deployed_token(token_path)
    refused_connection, _connect_ms = _deployed_connection(base)
    try:
        refused = _deployed_request(refused_connection, "/api/v1/status")
    finally:
        refused_connection.close()
    if refused["status"] == 302 and "cloudflareaccess.com" not in refused["headers"].get("location", ""):
        raise RuntimeError("deployed benchmark unauthenticated request did not redirect to Access")
    if refused["status"] not in (302, 403):
        raise RuntimeError("deployed benchmark unauthenticated request was not refused")
    probe_connection, _connect_ms = _deployed_connection(base)
    try:
        probe = _deployed_request(probe_connection, "/api/v1/status", token)
    finally:
        probe_connection.close()
    if probe["status"] != 200:
        raise RuntimeError("deployed benchmark authenticated status failed with %d" % probe["status"])
    if probe["headers"].get("cache-control") != "no-store":
        raise RuntimeError("deployed benchmark status response did not carry no-store")
    if "d1" not in probe["headers"].get("server-timing", ""):
        raise RuntimeError("deployed benchmark status response carried no d1 timing")
    if scenario == "search":
        query = os.environ.get("MEMORIES_QUERY", QUERY)
        operations = (
            ("search-page", "/api/v1/memories?limit=20&state=all"),
            ("search-fts", "/api/v1/memories?limit=20&state=all&q=" + quote(query, safe="")),
        )
    else:
        operations = (("status", "/api/v1/status"),)

    connections = {}
    connect_times = {}
    samples_seen = {}
    reconnects = {name: 0 for name, _path in operations}
    for name, _path in operations:
        connections[name], connect_times[name] = _deployed_connection(base)
        samples_seen[name] = 0

    def action(name, path):
        def run():
            try:
                result = _deployed_request(connections[name], path, token)
            except (http.client.HTTPException, OSError):
                connections[name].close()
                connections[name], connect_times[name] = _deployed_connection(base)
                reconnects[name] += 1
                result = _deployed_request(connections[name], path, token)
            if result["status"] != 200:
                raise RuntimeError("deployed benchmark request %s failed with %d" % (path, result["status"]))
            timing = _server_timing(result["headers"].get("server-timing"))
            if (timing.get("d1") or {}).get("dur") is None or (timing.get("r2") or {}).get("dur") is None:
                raise RuntimeError("deployed benchmark response has no valid Server-Timing")
            timings = {**result["timings"],
                       "connect_ms": connect_times[name] if samples_seen[name] == 0 else 0.0,
                       "d1_ms": (timing.get("d1") or {}).get("dur"),
                       "r2_ms": (timing.get("r2") or {}).get("dur"),
                       "reconnected": samples_seen[name] > 0 and reconnects[name] > 0}
            samples_seen[name] += 1
            counters = {"requests": 1, "bytes": result["bytes"], "status": result["status"],
                        "reconnects": reconnects[name]}
            if result["results"] is not None:
                counters["results"] = result["results"]
            return {**counters, "_timings": timings}
        return run

    try:
        for name, path in operations:
            _timed(action(name, path), -1, _gate)
        samples = []
        for repeat in range(args.repeats):
            for name, path in operations:
                sample = _timed(action(name, path), repeat, _gate)
                sample["operation"] = name
                samples.append(sample)
    finally:
        for connection in connections.values():
            connection.close()
    dataset = {"requests": args.repeats * len(operations), "auth_refusals": 1, "warmups": len(operations)}
    if scenario == "search":
        dataset.update({"query": os.environ.get("MEMORIES_QUERY", QUERY), "limit": 20})
    document = _document(
        "production", scenario, dataset, samples, tuple(name for name, _path in operations),
        runtime={**_runtime(), "endpoint": base, "token": "service-token"})
    document["measurement"] = "observational"
    document["perf_gate"] = "observational"
    document["host_gate"] = "passed" if all(sample["wall_ms"] is not None for sample in samples) else "deferred"
    _write(document, Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out)
    print(json.dumps(document, indent=2, sort_keys=True))
    variance = document.get("counter_variance")
    if isinstance(variance, list) and variance:
        print("bench_mirror: deterministic counters varied across repeats: " + ", ".join(str(item) for item in variance),
              file=sys.stderr)
        return 1
    return 0


def _worker_get(port, path):
    status, headers, body = _bench_http(port, "GET", path, {"x-bench-principal": "reader"})
    if status != 200:
        raise RuntimeError("worker request %s failed with %d" % (path, status))
    if headers.get("cache-control") != "no-store":
        raise RuntimeError("worker response did not carry no-store")
    return {"bytes": len(body), "server_timing": headers.get("server-timing")}


def _dispatch(environment, args):
    if environment == "worker":
        return _worker(args)
    if environment == "deployed":
        return _deployed(args)
    scenario = args.scenario or "capture"
    if scenario not in LOCAL_SCENARIOS:
        owner = DEFERRED.get(scenario, "a later slice")
        print("bench_mirror: local scenario %s lands in %s" % (scenario, owner), file=sys.stderr)
        return 2
    if args.repeats < 1:
        raise ValueError("repeats must be positive")
    if args.records < 1 or args.sessions < 1 or args.messages < 1 or args.projects < 1 or args.objects < 1:
        raise ValueError("records, sessions, messages, and projects must be positive")
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    with tempfile.TemporaryDirectory(dir=str(WORK_TMP), prefix="mirror-bench-") as temporary:
        root = Path(temporary)
        if scenario == "capture":
            dataset, samples, operations = _capture(scenario, args, root)
        elif scenario == "learning-import":
            dataset, samples, operations = _learning_import(scenario, args, root)
        else:
            dataset, samples, operations = _mirror_initial(scenario, args, root)
    document = _document("local", scenario, dataset, samples, operations)
    _write(document, out)
    print(json.dumps(document, indent=2, sort_keys=True))
    variance = document.get("counter_variance")
    if isinstance(variance, list) and variance:
        print("bench_mirror: deterministic counters varied across repeats: " + ", ".join(str(item) for item in variance),
              file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark the mirror's local paths and the deployed Worker.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for environment in ("local", "worker", "deployed"):
        command = subparsers.add_parser(environment)
        command.add_argument("--scenario")
        command.add_argument("--repeats", type=int, default=5)
        command.add_argument("--records", type=int, default=1000)
        command.add_argument("--objects", type=int, default=200)
        command.add_argument("--sessions", type=int, default=200)
        command.add_argument("--messages", type=int, default=10)
        command.add_argument("--projects", type=int, default=10)
        command.add_argument("--out", default=".work/perf")
    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--json", action="store_true")
    report = subparsers.add_parser("report")
    report.add_argument("--dir", default=".work/perf")
    args = parser.parse_args(argv)
    try:
        if args.command in ("local", "worker", "deployed"):
            return _dispatch(args.command, args)
        if args.command == "compare":
            verdict = _compare(_load(args.baseline), _load(args.candidate))
            if args.json:
                print(json.dumps(verdict, indent=2, sort_keys=True))
            else:
                _print_compare(verdict)
            return 0 if verdict["passed"] else 1
        return _report(args.dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print("bench_mirror: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
