#!/usr/bin/env python3
"""Score recall on a private corpus of real queries and propose candidates for labeling.

Neither subcommand prints prompt or result text: `candidates` reports counts, and
`validate` and `score` report identifiers and per-category counts only.
"""
import argparse
import dataclasses
import importlib.util
import json
import os
import random
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
MODULES = {}

BACK_REFERENCE = re.compile(
    r"\b(?:again|last time|before|previously|we tried|earlier|remember|"
    r"same (?:error|issue|problem)|why did we|what did we decide)\b",
    re.IGNORECASE,
)
QUERY_CHARS = 400
SEED = 13
ABSTENTION_SAMPLE = 40
DEFAULT_LIMIT = 200
REPEATED_PROMPT_SESSIONS = 3
VALID_EXPECTATIONS = ("match", "abstain")


def _load(name, path):
    if name not in MODULES:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        MODULES[name] = module
    return MODULES[name]


def _replay():
    return _load("replay_recall", ROOT / "eval" / "replay_recall.py")


def _bench():
    return _load("bench_recall", ROOT / "eval" / "bench_recall.py")


def _read_index(state_dir):
    path = Path(_replay().Store.database_path(Path(state_dir)))
    if not path.is_file():
        raise SystemExit("no session index at " + str(path))
    database = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(str(path)), uri=True, timeout=5)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA busy_timeout = 5000")
    return database


@dataclasses.dataclass(frozen=True)
class _Index:
    db: sqlite3.Connection


def _private_output(path):
    path = Path(path).expanduser()
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
        os.chmod(parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")


def _read_corpus(path):
    entries = []
    for number, raw in enumerate(Path(path).expanduser().read_text(encoding="utf-8").splitlines(), 1):
        if raw.strip():
            entries.append((number, raw))
    return entries


def _prompt_text(raw):
    text = str(raw).strip()
    hook = _replay().recall_hook
    if not text or text.startswith((*hook.INJECTED_PROMPT_PREFIXES, hook.HEADER)):
        return None
    return text


def _prompt_rows(database):
    replay = _replay()
    rows = database.execute(
        "SELECT e.session AS session, e.position AS position, e.text AS text, e.timestamp AS timestamp, "
        "s.project AS project, s.parent AS parent, s.thread_source AS thread_source "
        "FROM events e JOIN sessions s ON s.id = e.session "
        "WHERE e.kind = 'user' AND e.text != '' AND s.project IS NOT NULL "
        "ORDER BY s.updated, e.session, e.position").fetchall()
    back, ordinary, opened = [], [], set()
    sessions_by_text = {}
    dropped_non_user = 0
    for row in rows:
        key = " ".join(row["text"].split())
        sessions_by_text.setdefault(key, set()).add(row["session"])
        if row["parent"] is not None or row["thread_source"] not in ("user", None):
            dropped_non_user += 1
            continue
        text = _prompt_text(row["text"])
        stamp = replay.epoch(row["timestamp"])
        if text is None or stamp is None:
            continue
        opening = row["session"] not in opened
        opened.add(row["session"])
        if BACK_REFERENCE.search(text):
            back.append((row, text, stamp, key))
        elif opening:
            ordinary.append((row, text, stamp, key))
    dropped_repeated = 0
    kept_back, kept_ordinary = [], []
    for source, target in ((back, kept_back), (ordinary, kept_ordinary)):
        for row, text, stamp, key in source:
            if len(sessions_by_text[key]) >= REPEATED_PROMPT_SESSIONS:
                dropped_repeated += 1
            else:
                target.append((row, text, stamp))
    return kept_back, kept_ordinary, dropped_non_user, dropped_repeated


def _candidate(row, text, stamp):
    return {
        "id": "%s@%d" % (row["session"], row["position"]), "project": row["project"], "session": row["session"],
        "position": int(row["position"]), "cutoff": stamp, "query": text[:QUERY_CHARS],
        "category": "", "expect": "", "answer": [], "relevant_sessions": [],
    }


def candidates(state_dir, out, limit):
    database = _read_index(state_dir)
    try:
        back, ordinary, dropped_non_user, dropped_repeated = _prompt_rows(database)
    finally:
        database.close()
    budget = max(0, int(limit))
    chosen = back[:budget]
    room = budget - len(chosen)
    sampled = random.Random(SEED).sample(ordinary, min(len(ordinary), ABSTENTION_SAMPLE, room)) if room > 0 else []
    with _private_output(out) as stream:
        for row, text, stamp in chosen + sampled:
            stream.write(json.dumps(_candidate(row, text, stamp), ensure_ascii=False) + "\n")
    print("candidates: back_reference=%d abstain_sample=%d written=%d filtered_non_user=%d filtered_repeated=%d"
          % (len(chosen), len(sampled), len(chosen) + len(sampled), dropped_non_user, dropped_repeated))
    return 0


def validate(path):
    labeled = unlabeled = 0
    problems = []
    for number, raw in _read_corpus(path):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            problems.append("line %d: not JSON" % number)
            continue
        if not isinstance(record, dict):
            problems.append("line %d: not an object" % number)
            continue
        identifier = record.get("id")
        name = identifier if isinstance(identifier, str) and identifier else "line %d" % number
        expect = record.get("expect")
        if expect in ("", None):
            unlabeled += 1
            continue
        labeled += 1
        if expect not in VALID_EXPECTATIONS:
            problems.append("%s: expect must be match or abstain" % name)
        category = record.get("category")
        if not isinstance(category, str) or not category.strip():
            problems.append("%s: category is empty" % name)
        cutoff = record.get("cutoff")
        if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)):
            problems.append("%s: cutoff is not a number" % name)
        answer = record.get("answer")
        if not isinstance(answer, list):
            problems.append("%s: answer must be a list" % name)
        elif expect == "match" and (
            not answer or any(not isinstance(item, str) or not item.strip() for item in answer)
        ):
            problems.append("%s: match needs a non-empty answer" % name)
        elif expect == "abstain" and answer:
            problems.append("%s: abstain needs an empty answer" % name)
    for problem in problems:
        print(problem)
    print("validate: labeled=%d unlabeled=%d problems=%d" % (labeled, unlabeled, len(problems)))
    return 1 if problems else 0


def score(state_dir, path, as_json):
    database = _read_index(state_dir)
    replay = _replay()
    normalize = _bench().normalize
    limit = replay.recall_hook.CANDIDATE_LIMIT
    per_case = []
    try:
        for _number, raw in _read_corpus(path):
            record = json.loads(raw)
            expect = record.get("expect")
            if expect in ("", None):
                continue
            response = replay.AsOfRetriever(
                _Index(database), record["project"], None, float(record["cutoff"])).retrieve(
                    record["query"], limit, verify=False)
            top = response["results"][:limit]
            cited = {support["session"] for item in top for support in item["support"]}
            returned = normalize(" ".join(
                " ".join(str(item[field] or "") for field in ("summary", "applies_when", "action")) for item in top))
            metrics = {}
            if expect == "match":
                relevant = set(record.get("relevant_sessions") or ())
                if relevant:
                    metrics["recall5"] = int(bool(relevant & cited))
                if record.get("answer"):
                    metrics["span5"] = int(any(normalize(answer) in returned for answer in record["answer"]))
            else:
                metrics["abstain_correct"] = int(not response["results"])
            per_case.append((record, metrics))
    finally:
        database.close()
    counts = _bench().category_counts(per_case)
    if as_json:
        print(json.dumps(counts))
    else:
        for category, row in counts.items():
            print("%s: %s" % (category, " ".join("%s=%d" % entry for entry in row.items())))
    return 0


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", metavar="command")
    propose = commands.add_parser("candidates", help="propose candidate queries for the owner to label")
    propose.add_argument("--state-dir", type=Path, required=True)
    propose.add_argument("--out", type=Path, required=True)
    propose.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="maximum lines to write")
    check = commands.add_parser("validate", help="check a labeled corpus")
    check.add_argument("--corpus", type=Path, required=True)
    measure = commands.add_parser("score", help="score a labeled corpus as of each cutoff")
    measure.add_argument("--state-dir", type=Path, required=True)
    measure.add_argument("--corpus", type=Path, required=True)
    measure.add_argument("--json", action="store_true")
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 2
    if args.command == "candidates":
        return candidates(args.state_dir, args.out, args.limit)
    if args.command == "validate":
        return validate(args.corpus)
    return score(args.state_dir, args.corpus, args.json)


if __name__ == "__main__":
    sys.exit(main())
