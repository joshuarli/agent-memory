#!/usr/bin/env python3
"""Opt-in memory-use evaluation: baseline, retrieved-memory treatment, and unrelated-task control against native Codex runs."""
import argparse
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
SRC = REPOSITORY / "agent-memory/src"
SHIM = REPOSITORY / "dotfiles/base/.local/bin/agent-learn"
sys.path.insert(0, str(SRC))
SPEC = importlib.util.spec_from_file_location("agent_learning_live", REPOSITORY / "agent-memory/eval/live_learning.py")
assert SPEC is not None
LIVE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(LIVE)
BENCH_SPEC = importlib.util.spec_from_file_location("agent_recall_bench", REPOSITORY / "agent-memory/eval/bench_recall.py")
assert BENCH_SPEC is not None
BENCH = importlib.util.module_from_spec(BENCH_SPEC)
assert BENCH_SPEC.loader is not None
sys.modules[BENCH_SPEC.name] = BENCH
BENCH_SPEC.loader.exec_module(BENCH)

from agent_memory import learning as agent_learning  # noqa: E402
from agent_memory import store as agent_session_store  # noqa: E402

TMP_ROOT = REPOSITORY / ".work" / "tmp"
RUNS_PER_ARM = 3
ARMS = ("baseline", "treatment", "control")
TASK_PROMPT = (
    "The cache test suite was reported as OOM-killed on CI. Find out whether the cache tests pass here, using tools/run_cache_tests.py. "
    "Do not modify repository files. Report the outcome."
)
CONTROL_PROMPT = "What version is declared in VERSION? Read it and report it. Do not run the test suite or modify files."
LESSON_ENV = "TIDEPOOL_SHARDS"
FIXTURE_FILES = {
    "README.md": "# Tidepool\n\nA sharded cache. Run `python3 tools/run_cache_tests.py` for the cache suite.\n",
    "VERSION": "3.7.2\n",
    "src/cache.py": "import os\n\n\ndef shard_count():\n    return int(os.environ.get('TIDEPOOL_SHARDS') or os.cpu_count() or 1)\n",
    "tools/run_cache_tests.py": (
        "import os\n"
        "import sys\n"
        "if not os.environ.get('TIDEPOOL_SHARDS'):\n"
        "    print('OOM_KILLED: shard count followed cpu_count (64); container limit 2g')\n"
        "    sys.exit(137)\n"
        "print('CACHE_TESTS_PASS shards=' + os.environ['TIDEPOOL_SHARDS'])\n"
    ),
}
LESSON_TURNS = [
    {"user": "The nightly cache job OOMs on Linux CI but never on my laptop. Find out why."},
    {"tool": {"cmd": "python3 tools/run_cache_tests.py", "output": "OOM_KILLED: shard count followed cpu_count (64); container limit 2g", "exit": 137}},
    {"assistant": "ShardCache sizes itself from os.cpu_count(); the CI runner reports 64 CPUs so it allocates 64 shards inside a 2g container."},
    {"tool": {"cmd": "TIDEPOOL_SHARDS=4 python3 tools/run_cache_tests.py", "output": "CACHE_TESTS_PASS shards=4", "exit": 0}},
]
GUIDANCE_START = "- **Recall prior experience before expensive rediscovery.**"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def recall_guidance():
    text = (REPOSITORY / "dotfiles/base/.agents/skills/do/SKILL.md").read_text(encoding="utf-8")
    line = next(row for row in text.splitlines() if row.startswith(GUIDANCE_START))
    return line.replace("~/usr/agent-memory/README.md", "the project documentation")


def write_repository(root, with_guidance):
    root.mkdir(parents=True)
    for relative, content in FIXTURE_FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    instructions = "# Project instructions\n\nWork carefully and report evidence.\n"
    if with_guidance:
        instructions += "\n## Prior experience\n\n`agent-learn` is on PATH.\n\n" + recall_guidance() + "\n"
    (root / "AGENTS.md").write_text(instructions, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Live Evaluation")
    _git(root, "config", "user.email", "live-evaluation@example.invalid")
    (root / ".git/info/exclude").write_text(".agent/\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "chore(live): create synthetic fixture")


def install_memory(repo):
    state = repo / ".agent/state"
    roots = repo / ".agent/codex/sessions"
    start = time.time() - 20 * 86400
    BENCH.write_episode({"codex": roots, "claude": roots, "muse": roots, "grok": roots},
                        {"id": "lesson-1", "harness": "codex", "turns": LESSON_TURNS}, repo, start)
    store = agent_session_store.Store(state)
    learner = agent_learning.Learner(store, None)
    store.refresh(roots={"codex": [roots]})
    identity = agent_session_store.project_identity(repo)
    totals = learner.observe_project(identity, repo, time.time(), 0)
    store.close()
    return totals


def write_shim(repo):
    shim_dir = repo / ".agent/bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "agent-learn"
    shim.write_text(
        "#!/bin/sh\nCODEX_HOME=\"%s\" exec %s %s --state-dir \"%s\" \"$@\"\n"
        % (repo / ".agent/codex", sys.executable, SHIM, repo / ".agent/state"),
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim_dir


def executes(command, script):
    return re.search(r"(?:^|[\s;&|'\"(])(?:\S+=\S+\s+)*python3?\s[^;&|]*?" + re.escape(script), command) is not None


def run_metrics(arm, run):
    commands = run.get("commands", [])
    suite = [record for record in commands if executes(record["command"], "tools/run_cache_tests.py")]
    unset = [record for record in suite if record["exit_code"] == 137]
    passed = any(record["exit_code"] == 0 and "CACHE_TESTS_PASS" in record["output"] for record in suite)
    recalled = [record for record in commands if re.search(r"\bagent-learn\s+recall\b", record["command"])]
    inspected = [record for record in commands if re.search(r"\bagent-learn\s+inspect\b", record["command"])]
    lesson_used = any(LESSON_ENV + "=" in record["command"] for record in suite) or any(LESSON_ENV in record["command"] for record in commands if record not in recalled)
    return {
        "run_status": run.get("status"),
        "completed_correctly": passed if arm != "control" else not suite,
        "repeated_mistake_ran_without_lesson": bool(unset),
        "attempts_at_suite": len(suite),
        "tool_calls": len(commands),
        "recall_invoked": bool(recalled),
        "inspect_invoked": bool(inspected),
        "lesson_applied": lesson_used and any(record["exit_code"] == 0 for record in suite),
        "unwanted_memory_effect": arm == "control" and (bool(recalled) or lesson_used or bool(suite)),
        "usage": run.get("usage", {}),
        "elapsed_seconds": run.get("elapsed_seconds"),
    }


def _rate(rows):
    return lambda key: sum(1 for row in rows if row[key]) / len(rows)


def summarize(per_run):
    summary = {}
    for arm in ARMS:
        rows = [row for row in per_run.get(arm, []) if row["run_status"] == "complete"]
        if not rows:
            summary[arm] = {"complete_runs": 0}
            continue
        rate = _rate(rows)
        tokens = [sum(value for key, value in row["usage"].items() if key in ("input_tokens", "output_tokens")) for row in rows]
        summary[arm] = {
            "complete_runs": len(rows),
            "completed_correctly": rate("completed_correctly"),
            "repeated_mistake": rate("repeated_mistake_ran_without_lesson"),
            "recall_invoked": rate("recall_invoked"),
            "lesson_applied": rate("lesson_applied"),
            "unwanted_memory_effect": rate("unwanted_memory_effect"),
            "mean_tool_calls": sum(row["tool_calls"] for row in rows) / len(rows),
            "mean_attempts_at_suite": sum(row["attempts_at_suite"] for row in rows) / len(rows),
            "mean_tokens": sum(tokens) / len(tokens) if tokens else None,
            "mean_elapsed_seconds": sum(row["elapsed_seconds"] or 0 for row in rows) / len(rows),
        }
    return summary


def verdict(summary, runs):
    if any(summary[arm]["complete_runs"] == 0 for arm in ARMS):
        return {"status": "incomplete", "evidence_level": "none"}
    base, treated, control = summary["baseline"], summary["treatment"], summary["control"]
    improved = treated["repeated_mistake"] < base["repeated_mistake"] and treated["completed_correctly"] >= base["completed_correctly"]
    clean_control = control["unwanted_memory_effect"] == 0 and control["completed_correctly"] == 1
    return {
        "status": "evaluated",
        "treatment_better_than_baseline": improved,
        "control_clean": clean_control,
        "evidence_level": "smoke validation" if runs < 5 else "small measured sample",
        "claim": "no causal or general claim; one task family, %d runs per arm, one model" % runs,
    }


def run_arm(codex, reviewer, scratch, arm, index, prompt_schema):
    repo = scratch / ("%s-%d" % (arm, index))
    write_repository(repo, with_guidance=arm != "baseline")
    if arm != "baseline":
        install_memory(repo)
    shim_dir = write_shim(repo)
    env = reviewer._runtime_environment(scratch, {"PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", os.defpath)})
    env.pop("AGENT_LEARN_WORKER", None)
    prompt = CONTROL_PROMPT if arm == "control" else TASK_PROMPT
    model, effort = reviewer._tier("worker")
    started = time.time()
    result = LIVE._run_events(codex, repo, prompt_schema, env, prompt, model, effort, sandbox="workspace-write")
    result["elapsed_seconds"] = time.time() - started
    return run_metrics(arm, result)


def live(runs):
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    reviewer = LIVE.REVIEW.CodexReviewer(REPOSITORY, TMP_ROOT)
    capabilities = reviewer.capabilities()
    if capabilities.get("status") != "ok":
        return {"status": "unavailable", "reason": capabilities.get("reason", "codex_unavailable")}
    with tempfile.TemporaryDirectory(prefix="agent-recall-live-", dir=str(TMP_ROOT)) as temp:
        scratch = Path(temp)
        schema = scratch / "response-schema.json"
        schema.write_text(json.dumps(LIVE.TASK_SCHEMA, separators=(",", ":")), encoding="utf-8")
        per_run = {arm: [] for arm in ARMS}
        for index in range(runs):
            for arm in ARMS:
                per_run[arm].append(run_arm(reviewer._codex_path, reviewer, scratch, arm, index, schema))
        summary = summarize(per_run)
        return {"status": "done", "runs_per_arm": runs, "summary": summary, "verdict": verdict(summary, runs)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make paid model calls: three arms, --runs agent runs each")
    parser.add_argument("--runs", type=int, default=RUNS_PER_ARM)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("pass --live to opt in to model calls")
    if not 1 <= args.runs <= 10:
        parser.error("--runs must be between 1 and 10")
    result = live(args.runs)
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
