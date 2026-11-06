#!/usr/bin/env python3
"""Replay archived opening prompts through the prompt hook's recall as of each session's start, to measure how often it would have injected something."""
import argparse
import datetime
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_memory import recall_hook  # noqa: E402
from agent_memory.recall import Retriever  # noqa: E402
from agent_memory.store import Store  # noqa: E402


class AsOfRetriever(Retriever):
    def _load_record(self, kind, ref):
        candidate = super()._load_record(kind, ref)
        if candidate is None or candidate.observed_at >= self.now:
            return None
        return candidate


def epoch(stamp):
    if not stamp:
        return None
    try:
        return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def opening_prompts(db):
    rows = db.execute(
        "SELECT s.id AS session, s.project AS project, s.harness AS harness FROM sessions s "
        "WHERE s.parent IS NULL AND s.project IS NOT NULL ORDER BY s.updated").fetchall()
    for row in rows:
        first = db.execute("SELECT min(timestamp) FROM events WHERE session = ? AND timestamp IS NOT NULL", (row["session"],)).fetchone()[0]
        start = epoch(first)
        prompt = None
        for event in db.execute("SELECT text FROM events WHERE session = ? AND kind = 'user' ORDER BY position LIMIT 5", (row["session"],)):
            if recall_hook.is_task_prompt(event["text"]):
                prompt = event["text"][:recall_hook.MAX_PROMPT_CHARS]
                break
        called = db.execute(
            "SELECT 1 FROM events WHERE session = ? AND kind = 'tool-call' AND text LIKE '%agent-learn%' AND text LIKE '%recall%' LIMIT 1",
            (row["session"],)).fetchone() is not None
        yield {"session": row["session"], "project": row["project"], "harness": row["harness"], "start": start, "prompt": prompt, "called_recall": called}


def replay(state_dir, sample_size, seed):
    store = Store(state_dir)
    totals = {}
    hits, timings = [], []
    try:
        for entry in opening_prompts(store.db):
            bucket = totals.setdefault(entry["harness"], {"sessions": 0, "substantive": 0, "hits": 0, "echo_only": 0, "called_recall": 0, "hit_and_called": 0})
            bucket["sessions"] += 1
            bucket["called_recall"] += int(entry["called_recall"])
            if entry["prompt"] is None or entry["start"] is None:
                continue
            bucket["substantive"] += 1
            started = time.perf_counter()
            response = AsOfRetriever(store, entry["project"], None, entry["start"]).retrieve(entry["prompt"], recall_hook.CANDIDATE_LIMIT, verify=False)
            timings.append(time.perf_counter() - started)
            shown, context = recall_hook.render(entry["prompt"], response["results"])
            if context is None:
                bucket["echo_only"] += int(not response["abstained"])
                continue
            bucket["hits"] += 1
            bucket["hit_and_called"] += int(entry["called_recall"])
            hits.append({**entry, "results": [{"ref": item["ref"], "tier": item["tier"], "kind": item["kind"], "summary": item["summary"]} for item in shown]})
    finally:
        store.close()
    overall = {key: sum(bucket[key] for bucket in totals.values()) for key in ("sessions", "substantive", "hits", "echo_only", "called_recall", "hit_and_called")}
    timings.sort()
    sample = random.Random(seed).sample(hits, min(sample_size, len(hits)))
    return {
        "overall": overall, "by_harness": totals,
        "latency_ms": {"median": 1000 * timings[len(timings) // 2], "p95": 1000 * timings[int(0.95 * (len(timings) - 1))]} if timings else None,
        "hit_kinds": {kind: sum(1 for hit in hits for item in hit["results"] if item["ref"].startswith(kind + ":")) for kind in ("memory", "experience", "event")},
        "sample": sample,
    }


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--state-dir", type=Path, required=True, help="a copy of the state directory; recall creates temporary tables")
    cli.add_argument("--sample", type=int, default=60)
    cli.add_argument("--seed", type=int, default=13)
    args = cli.parse_args(argv)
    print(json.dumps(replay(args.state_dir, args.sample, args.seed), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
