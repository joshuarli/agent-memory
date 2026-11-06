#!/usr/bin/env python3
"""Measure the mirror's D1 and R2 cost on the local workerd stack."""
import argparse
import http.client
import json
import os
import re
import select
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORK_TMP = ROOT / ".work" / "tmp"
WORKER = ROOT / "cloudflare"
STATS = Path(os.environ.get("STATS_DIR") or (Path.home() / "d" / "stats"))
ORIGIN = "origin-a"
CLIENT_ID = "collector-client.access"
CONCURRENCY = 32
GENERATION_ARTIFACTS = 200
HOURLY_ARTIFACTS = 10
sys.path.insert(0, str(ROOT / "src"))

from agent_memory import archive as archive_module  # noqa: E402
from agent_memory import experience as experience_module  # noqa: E402
from agent_memory import learning as learning_module  # noqa: E402
from agent_memory import mirror  # noqa: E402
from agent_memory import mirror_crypto  # noqa: E402
from agent_memory.store import Store  # noqa: E402


def ensure_age():
    try:
        mirror_crypto.binary()
        return
    except mirror_crypto.CryptoError:
        result = subprocess.run(
            ["sh", str(WORKER / "dev-age.sh")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise RuntimeError("cannot build rage: " + result.stderr.strip()) from None
        os.environ["AGE_BIN"] = result.stdout.strip()


def start_stack():
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is required for the cost workload")
    process = subprocess.Popen(
        [node, "local/cli.mjs", "serve", "--ephemeral", "--test-state",
         "--origins", json.dumps([{"client_id": CLIENT_ID, "origin": ORIGIN}])],
        cwd=str(WORKER), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("local stack has no output stream")
    ready, _, _ = select.select([stdout], [], [], 60)
    if not ready:
        process.kill()
        _, stderr = process.communicate(timeout=10)
        raise RuntimeError("local stack did not start: " + (stderr.strip() or "timeout"))
    line = stdout.readline()
    try:
        info = json.loads(line)
    except (ValueError, KeyError, TypeError):
        process.kill()
        _, stderr = process.communicate(timeout=10)
        raise RuntimeError("local stack did not start: " + (stderr.strip() or line.strip())) from None
    return process, info


def stop_stack(process):
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def same_size(raw, _recipients):
    digest = archive_module.sha256(raw)
    return mirror_crypto.PreparedObject(
        plaintext_digest=digest, plaintext_size=len(raw), encoding="identity",
        compressed_size=len(raw), ciphertext=raw, ciphertext_digest=digest, ciphertext_size=len(raw),
        compress_cpu_ms=0.0, encrypt_cpu_ms=0.0)


def skip_snapshot(_store, _config, **kwargs):
    return {
        "id": "d" * 64, "revision": kwargs["revision"], "duplicate": True, "db_size": 0,
        "chunks": 0, "chunks_uploaded": 0, "objects": 0, "objects_uploaded": 0, "object_bytes": 0,
        "bytes_plaintext": 0, "bytes_ciphertext": 0,
    }


class Workload:
    def __init__(self, info, root, objects, hours):
        self.info = info
        self.root = root
        self.objects = objects
        self.hours = hours
        self.port = info["port"]
        self.tls = ssl.create_default_context(cafile=info["ca_file"])
        self.identity = root / "age.key"
        self.recipient = mirror_crypto.keygen(self.identity)
        self.recipients_file = root / "recipients.txt"
        self.recipients_file.write_text(self.recipient + "\n")
        self.recipients = mirror_crypto.load_recipients(self.recipients_file)
        self.token = Path(info["token_files"][ORIGIN])
        self.config = mirror.MirrorConfig(
            url=info["url"], service_token_file=self.token, age_recipients_file=self.recipients_file,
            age_identity_file=self.identity, learning_projection=False)
        self.store = Store(root / "state")
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO collector_identity(singleton, id, origin_host, display_label, created_at) "
                "VALUES(1, ?, ?, 'cost', 0)", (ORIGIN, ORIGIN))
        self.source = root / "source.jsonl"
        self.source.write_text(json.dumps({"type": "message", "text": "synthetic cost source"}) + "\n")
        self.client = mirror.MirrorClient(self.config, factory=self.factory, sleep=lambda _delay: None)

    def factory(self):
        return http.client.HTTPSConnection("127.0.0.1", self.port, timeout=300, context=self.tls)

    def request(self, method, path, body=None):
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=60, context=self.tls)
        try:
            headers = {"content-type": "application/json"} if body is not None else {}
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, payload
        finally:
            connection.close()

    def control(self, path, body=None):
        status, payload = self.request("POST" if body is not None else "GET", path, body)
        if status != 200:
            raise RuntimeError("%s failed: %s" % (path, payload.decode("utf-8", "replace")))
        return json.loads(payload)

    def reset(self):
        self.control("/__bench/reset?worker=collectors")

    def state(self):
        return self.control("/__bench/status?worker=collectors")

    def d1(self, sql, params):
        return self.control("/__test/d1", {"sql": sql, "params": params, "mode": "run"})

    def capture(self, native, artifacts):
        return archive_module.capture(
            self.store.db, self.store.archive_dir, "codex", native, self.source,
            artifacts=[("%s-%d.txt" % (native, index), data) for index, data in enumerate(artifacts)])

    def sync(self):
        with mock.patch.object(mirror, "upload_snapshot", side_effect=skip_snapshot):
            return mirror.sync(
                self.store, self.config, "cloudflare", client=self.client,
                upload_client_factory=lambda: mirror.MirrorClient(self.config, factory=self.factory, sleep=lambda _d: None),
                recipients=self.recipients, concurrency=CONCURRENCY, prepare=same_size)

    def measure(self, name, action):
        self.reset()
        started = time.perf_counter()
        details = action()
        duration = (time.perf_counter() - started) * 1000
        state = self.state()
        methods = state["r2"]
        return {
            "name": name,
            "statements": state["rows"]["statements"],
            "batches": state["counters"].get("d1.batch", 0),
            "rows_read": state["rows"]["read"],
            "rows_written": state["rows"]["written"],
            "r2": {
                "class_a": methods["put"] + methods["list"] + methods["multipart"],
                "class_b": methods["get"] + methods["head"],
                "methods": methods,
                "bytes_in": methods["bytesIn"],
                "bytes_out": methods["bytesOut"],
            },
            "duration_ms": round(duration, 1),
            "retries": 0,
            "failures": 0,
            "details": details,
        }

    def run(self):
        generations = max(1, self.objects // GENERATION_ARTIFACTS)
        initial = []
        for generation in range(generations):
            artifacts = [("artifact %d %d" % (generation, index)).encode() for index in range(GENERATION_ARTIFACTS)]
            self.capture("initial-%d" % generation, artifacts)
            initial.append(generation)
        operations = [self.measure("initial-backfill", self.sync)]

        def hourly():
            details = None
            for hour in range(self.hours):
                for number in range(2):
                    artifacts = [("hourly %d %d %d" % (hour, number, index)).encode()
                                 for index in range(HOURLY_ARTIFACTS)]
                    self.capture("hourly-%d-%d" % (hour, number), artifacts)
                details = self.sync()
            return details
        operations.append(self.measure("hourly-incremental", hourly))

        revision = self.client.hello(ORIGIN)["revision"]
        snapshot = self.measure(
            "daily-snapshot",
            lambda: mirror.upload_snapshot(
                self.store, self.config, client=self.client, origin=ORIGIN, revision=revision,
                recipients=self.recipients, concurrency=CONCURRENCY, prepare=same_size,
                client_factory=lambda: mirror.MirrorClient(self.config, factory=self.factory, sleep=lambda _d: None)))

        operations.append(snapshot)
        operations.append(self.measure("gc-mark", lambda: mirror.gc(self.config, client=self.client, apply=False)))

        def sweep():
            self.d1(
                "UPDATE remote_object SET gc_pending_at = ? WHERE gc_pending_at IS NOT NULL",
                [time.time() - 3601])
            return mirror.gc(self.config, client=self.client, apply=True)
        operations.append(self.measure("gc-sweep", sweep))

        def tombstone():
            self.store.forget_session("codex:hourly-0-0", time.time())
            return self.sync()
        operations.append(self.measure("tombstone", tombstone))

        def learning():
            learning_module.initialize(self.store.db)
            draft = experience_module.Draft(
                kind="method", claim="Synthetic cost projection claim.",
                applies_when="Measuring the mirror cost workload.", detector="synthetic",
                subject="cost", observed_at=100.0,
                sources=[{"session": "codex:initial-0", "position": 0, "digest": "a" * 64, "role": "trigger"}])
            with self.store.db:
                experience_module.add_experience(
                    self.store.db, "git:" + str(self.root), draft, "codex:initial-0", 100.0)
            projected = mirror.MirrorConfig(
                url=self.config.url, service_token_file=self.config.service_token_file,
                age_recipients_file=self.config.age_recipients_file,
                age_identity_file=self.config.age_identity_file, learning_projection=True)
            with mock.patch.object(mirror, "upload_snapshot", side_effect=skip_snapshot):
                return mirror.sync(
                    self.store, projected, "cloudflare", client=self.client,
                    upload_client_factory=lambda: mirror.MirrorClient(
                        projected, factory=self.factory, sleep=lambda _d: None),
                    recipients=self.recipients, concurrency=CONCURRENCY, prepare=same_size)
        operations.append(self.measure("learning-delta", learning))

        def restore():
            def identity_decode(ciphertext, _identity, _encoding, plaintext_digest, plaintext_size):
                if len(ciphertext) != plaintext_size or archive_module.sha256(ciphertext) != plaintext_digest:
                    raise mirror_crypto.CryptoError("object does not match")
                return ciphertext
            with mock.patch.object(mirror_crypto, "decode_object", side_effect=identity_decode):
                return mirror.restore(
                    self.config, "cloudflare", snapshot["details"]["id"], self.root / "restored",
                    client=self.client, identity_path=self.identity)
        operations.append(self.measure("restore", restore))
        return operations


def pricing():
    path = STATS / "cloudflare/src/cost/pricing.ts"
    source = path.read_text()
    def number(pattern):
        match = re.search(pattern, source)
        if match is None:
            raise RuntimeError("cannot read %s from %s" % (pattern, path))
        return float(match.group(1))
    return {
        "revision": re.search(r'PRICING_REVISION = "([^"]+)"', source).group(1),
        "per_million": {
            "d1_rows_written": number(r"d1RowsWritten: ([0-9.]+)"),
            "d1_rows_read": number(r"d1RowsRead: ([0-9.]+)"),
        },
        "per_gb_month": {"r2_storage": number(r"r2Storage: ([0-9.]+)")},
        "source": str(path),
    }


def project(operations, prices, days=30):
    by_name = {operation["name"]: operation for operation in operations}
    frequencies = {
        "initial-backfill": 1,
        "hourly-incremental": 24 * days,
        "daily-snapshot": days,
        "gc-mark": days,
        "gc-sweep": days,
        "tombstone": 1,
        "learning-delta": 1,
        "restore": 1,
    }
    totals = {
        "rows_read": 0, "rows_written": 0, "statements": 0, "batches": 0,
        "r2_class_a": 0, "r2_class_b": 0, "r2_bytes_in": 0, "r2_bytes_out": 0,
    }
    for name, count in frequencies.items():
        operation = by_name[name]
        totals["rows_read"] += operation["rows_read"] * count
        totals["rows_written"] += operation["rows_written"] * count
        totals["statements"] += operation["statements"] * count
        totals["batches"] += operation["batches"] * count
        totals["r2_class_a"] += operation["r2"]["class_a"] * count
        totals["r2_class_b"] += operation["r2"]["class_b"] * count
        totals["r2_bytes_in"] += operation["r2"]["bytes_in"] * count
        totals["r2_bytes_out"] += operation["r2"]["bytes_out"] * count
    snapshot = by_name["daily-snapshot"]["details"]
    stored = snapshot.get("object_bytes", 0) + snapshot.get("db_size", 0)
    cost = {
        "d1_rows_written": totals["rows_written"] / 1_000_000 * prices["per_million"]["d1_rows_written"],
        "d1_rows_read": totals["rows_read"] / 1_000_000 * prices["per_million"]["d1_rows_read"],
        "r2_storage": stored / 1e9 * prices["per_gb_month"]["r2_storage"],
    }
    cost["total"] = sum(cost.values())
    return {
        "days": days,
        "frequencies": frequencies,
        "totals": totals,
        "r2_storage_bytes": stored,
        "cost": {key: round(value, 6) for key, value in cost.items()},
        "notes": [
            "R2 Class A/B operation prices are not present in pricing.ts; the counts are reported without a price.",
            "R2 storage is the measured snapshot object and database bytes held for the projection window.",
            "The one-time backfill, tombstone, learning change, and restore are counted once.",
        ],
    }


def write_report(document, out):
    out.mkdir(parents=True, exist_ok=True)
    (out / "cost.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Mirror cost workload",
        "",
        "Objects: %d. Hours: %d. Pricing: %s." % (
            document["objects"], document["hours"], document["pricing"]["revision"]),
        "",
        "| Operation | Statements | Batches | Rows read | Rows written | R2 A | R2 B | Bytes in | Bytes out | ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for operation in document["operations"]:
        lines.append("| %s | %d | %d | %d | %d | %d | %d | %d | %d | %.1f |" % (
            operation["name"], operation["statements"], operation["batches"], operation["rows_read"],
            operation["rows_written"], operation["r2"]["class_a"], operation["r2"]["class_b"],
            operation["r2"]["bytes_in"], operation["r2"]["bytes_out"], operation["duration_ms"]))
    projection = document["projection"]
    lines.extend([
        "",
        "## 30-day projection (estimate)",
        "",
        "| Cost | Value |",
        "| --- | ---: |",
        "| D1 rows written | %d |" % projection["totals"]["rows_written"],
        "| D1 rows read | %d |" % projection["totals"]["rows_read"],
        "| R2 Class A | %d |" % projection["totals"]["r2_class_a"],
        "| R2 Class B | %d |" % projection["totals"]["r2_class_b"],
        "| D1 written cost | %.4f |" % projection["cost"]["d1_rows_written"],
        "| D1 read cost | %.4f |" % projection["cost"]["d1_rows_read"],
        "| R2 storage cost | %.4f |" % projection["cost"]["r2_storage"],
        "| Total | %.4f |" % projection["cost"]["total"],
        "",
    ])
    lines.extend("- " + note for note in projection["notes"])
    (out / "cost.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--objects", type=int, default=80_000)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--out", default=".work/perf")
    args = parser.parse_args()
    ensure_age()
    WORK_TMP.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="cost-", dir=str(WORK_TMP)))
    process = None
    try:
        process, info = start_stack()
        workload = Workload(info, root, args.objects, args.hours)
        try:
            operations = workload.run()
        finally:
            shutil.rmtree(root / "restored", ignore_errors=True)
            workload.store.close()
        prices = pricing()
        document = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "objects": args.objects,
            "hours": args.hours,
            "operations": operations,
            "pricing": prices,
            "projection": project(operations, prices),
        }
        write_report(document, Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out)
        print(json.dumps({"operations": len(operations), "out": str(args.out)}, sort_keys=True))
    finally:
        if process is not None:
            stop_stack(process)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
