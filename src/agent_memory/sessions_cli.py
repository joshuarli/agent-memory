#!/usr/bin/env python3
"""Search and inspect the local agent session index."""
import argparse
import datetime
import os
import json
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any

from agent_memory import archive as archive_module
from agent_memory import backup as backup_module
from agent_memory import learning_sync
from agent_memory import mirror as mirror_module
from agent_memory import transfer as transfer_module
from agent_memory.store import Store, StoreError, default_state_dir, project_identity, rebuild_index, redact
from agent_memory.transcripts import canonical_harness
from agent_memory.types import DATABASE_HARNESSES, HARNESSES, Harness


def _state_dir(args):
    return Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()


def _project(args):
    if args.all_projects:
        return None
    return project_identity(args.project if args.project else Path.cwd())


def _roots(args) -> dict[Harness, list[str]] | None:
    if not args.root:
        return None
    roots: dict[Harness, list[str]] = {}
    for value in args.root:
        name, separator, path = value.partition("=")
        if separator:
            try:
                roots.setdefault(canonical_harness(name), []).append(path)
            except ValueError:
                roots.setdefault("codex", []).append(value)
        else:
            roots.setdefault("codex", []).append(value)
    return roots


def _harnesses(args) -> tuple[Harness, ...]:
    selected = getattr(args, "harness", None)
    if selected:
        return tuple(canonical_harness(value) for value in selected)
    return HARNESSES


def _stats(stats):
    print(
        "sessions={sessions_indexed} events={events_added} malformed={malformed_lines} "
        "partial={partial_files} resets={resets} deleted={deleted} skipped_project={skipped_project} "
        "skipped_ephemeral={skipped_ephemeral} skipped_learning_workers={skipped_learning_workers} "
        "archived={archived} archive_errors={archive_errors}".format(**stats)
    )


def _since_value(value):
    if value is None:
        return None
    match = re.fullmatch(r"([0-9]+)([smhdw])", value, re.IGNORECASE)
    if match:
        seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2).lower()]
        return time.time() - seconds
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use an ISO date/time or a duration such as 30d") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _source_label(source, thread_source):
    if thread_source in ("user", "subagent"):
        return thread_source
    if not source:
        return "unknown"
    try:
        value = json.loads(source)
    except ValueError:
        return "unknown"
    if isinstance(value, dict) and "subagent" in value:
        return "subagent"
    return value if isinstance(value, str) and value in ("user", "cli", "child") else "unknown"


def _display_path(path):
    return json.dumps(str(path), ensure_ascii=False)


def _timestamp_epoch(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _snippet(text, query, regex):
    flat = " ".join(text.split())
    if not flat:
        return ""
    if regex:
        match = re.search(query, flat, re.IGNORECASE)
        index = match.start() if match else 0
    else:
        terms = [term.strip('"') for term in re.findall(r"[\w-]+", query) if term.upper() not in ("AND", "OR", "NOT", "NEAR")]
        positions = [flat.lower().find(term.lower()) for term in terms if term and flat.lower().find(term.lower()) >= 0]
        index = min(positions) if positions else 0
    start = max(0, index - 72)
    end = min(len(flat), index + 160)
    snippet = ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")
    return redact(snippet)




def config_path():
    canonical = Path.home() / ".agent-sessions" / "config.json"
    if canonical.is_file():
        return canonical
    base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
    return base.expanduser() / "agent-sessions" / "config.json"


def load_config():
    path = config_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except ValueError as exc:
        raise StoreError("agent-sessions config is not valid JSON: " + str(path)) from exc
    if not isinstance(value, dict):
        raise StoreError("agent-sessions config must be an object: " + str(path))
    return value


def central_settings(args):
    settings = dict(load_config().get("central") or {})
    if getattr(args, "central_state", None):
        settings.update({"transport": "local", "state_dir": args.central_state})
    if not settings:
        raise StoreError("no central store is configured; set ~/.agent-sessions/config.json or pass --central-state")
    return settings


def open_central(args):
    settings = central_settings(args)
    if settings.get("transport", "local") != "local":
        raise StoreError("central transport %r cannot be opened directly" % settings.get("transport"))
    return Store(Path(os.path.expanduser(str(settings["state_dir"]))))


def central_rows(args, op, payload) -> list[dict[str, Any]]:
    settings = central_settings(args)
    if settings.get("transport", "local") == "local":
        with Store(Path(os.path.expanduser(str(settings["state_dir"])))) as store:
            return transfer_module.query(store, op, payload)
    host = settings.get("host")
    command = settings.get("command")
    if not host or not command:
        raise StoreError("ssh central settings need host and command")
    with Store(_state_dir(args)) as local:
        origin = archive_module.origin_for(local.db)
    return transfer_module.query_over_ssh(host, command, op, payload, origin, "agent-sessions")


def _print_central_row(row, query=None, regex=False):
    evidence = row["evidence"]
    timestamp = evidence.get("timestamp") or datetime.datetime.fromtimestamp(row["updated"], datetime.timezone.utc).isoformat(timespec="seconds")
    print(
        f"session={row['session']} byte={row['position']} kind={evidence['kind']} time={timestamp[:19]} "
        f"source={_source_label(row.get('source'), row.get('thread_source'))} project={_display_path(row.get('cwd') or '')} "
        f"harness={row['harness']} origin={row.get('origin_host') or 'unknown'} generation={row.get('archive_generation')}"
    )
    if row.get("parent"):
        print("parent=" + str(row["parent"]))
    text = _snippet(evidence["text"], query, regex) if query is not None else redact(evidence["text"])
    print("  " + text)


def _memory(args):
    try:
        rows = central_rows(args, "memory", {"limit": args.limit})
        for raw in rows:
            row = dict(raw)
            observed = datetime.datetime.fromtimestamp(float(row["observed_at"]), datetime.timezone.utc)
            print("record=%s origin=%s project=%s kind=%s observed=%s" % (
                row["record"], row.get("origin_host") or "central", row["project"], row["kind"],
                observed.isoformat(timespec="seconds")))
            print("  claim=" + redact(row["claim"]))
            if row.get("applies_when"):
                print("  applies_when=" + redact(row["applies_when"]))
            for item in row.get("sources") or []:
                source = dict(item)
                print("  evidence=%s byte=%s digest=%s role=%s" % (
                    source["session"], source["position"], str(source["digest"])[:16], source["role"]))
            print("  authority=" + str(row["authority"]))
        print("records=%d scope=central" % len(rows))
        return 0
    except (OSError, StoreError, transfer_module.TransferError) as exc:
        print("agent-sessions memory: " + str(exc), file=sys.stderr)
        return 1


def _identity(args):
    try:
        with Store(_state_dir(args)) as store:
            regenerate = args.regenerate or args.action == "regenerate"
            origin, label = archive_module.ensure_identity(store.db, label=args.label, regenerate=regenerate)
            print("origin_host=" + origin)
            print("label=" + str(label or ""))
            for row in store.db.execute("SELECT id, display_label, created_at FROM origin_host ORDER BY created_at"):
                print("known_origin=%s label=%s created=%s" % (row[0], row[1], datetime.datetime.fromtimestamp(row[2], datetime.timezone.utc).isoformat(timespec="seconds")))
        return 0
    except (OSError, StoreError) as exc:
        print("agent-sessions identity: " + str(exc), file=sys.stderr)
        return 1


def _project_command(args):
    try:
        with Store(_state_dir(args)) as store:
            if args.action == "list":
                for row in store.db.execute("SELECT origin_host, name, checkout_path FROM project_map ORDER BY name"):
                    print("project=%s origin=%s checkout=%s" % (row[1], row[0], _display_path(row[2] or "")))
                return 0
            if not args.name or not args.checkout:
                raise StoreError("project map needs --name and --checkout")
            origin = args.origin or archive_module.origin_for(store.db)
            with store.db:
                store.db.execute(
                    "INSERT OR REPLACE INTO project_map (id, origin_host, name, checkout_path, created_at) VALUES (?, ?, ?, ?, ?)",
                    (origin + ":" + args.name, origin, args.name, argparse_checkout(args.checkout), time.time()))
            print("mapped=%s origin=%s" % (args.name, origin))
        return 0
    except (OSError, StoreError) as exc:
        print("agent-sessions project: " + str(exc), file=sys.stderr)
        return 1


def argparse_checkout(value):
    return str(Path(value).expanduser().resolve())


def _sync(args):
    import fcntl

    state = _state_dir(args)
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = open(state / "sync.lock", "a+")
    except OSError as exc:
        print("agent-sessions sync: " + str(exc), file=sys.stderr)
        return 1
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("overlapped=1 pending=skipped")
            return 0
        with Store(state) as store:
            settings = dict(load_config().get("central") or {})
            if args.central_state:
                settings.update({"transport": "local", "state_dir": args.central_state})
            if not settings:
                raise StoreError("no central store is configured; set ~/.agent-sessions/config.json or pass --central-state")
            transport = settings.get("transport", "local")
            if transport != "cloudflare":
                store.refresh(roots=_roots(args), project=None, harnesses=_harnesses(args))
            origin = archive_module.origin_for(store.db)
            ack_remote = args.to
            if transport == "cloudflare":
                pinned = store.mirror_remote_state(args.to)
                if pinned is not None and pinned["store_id"]:
                    ack_remote = pinned["store_id"]
            pending = store.pending_transfers(ack_remote)
            if args.dry_run:
                print("pending=%d origin=%s remote=%s" % (pending, origin, args.to))
                return 0
            def progress(count):
                if count % 25 == 0:
                    print("delivered=%d" % count, file=sys.stderr, flush=True)

            if transport == "local":
                result = transfer_module.local_sync(
                    store, Path(os.path.expanduser(str(settings["state_dir"]))), args.to, origin,
                    args.label or "collector", progress=progress)
            elif transport == "cloudflare":
                config = mirror_module.decode_config(settings)
                result = mirror_module.sync(
                    store, config, args.to,
                    refresh=lambda: store.refresh(roots=_roots(args), project=None, harnesses=_harnesses(args)),
                    progress=progress)
                print("committed=%d duplicate=%d tombstoned=%d objects_stored=%d manifests=%d "
                      "bytes_plaintext=%d bytes_compressed=%d tombstones_fetched=%d "
                      "tombstones_pushed=%d endpoint=%s store=%s revision=%d" % (
                          result["generations_committed"], result["generations_duplicate"],
                          result["generations_tombstoned"], result["objects_stored"],
                          result["manifests_uploaded"],
                          result["bytes_plaintext"], result["bytes_compressed"],
                          result["tombstones_fetched"], result["tombstones_pushed"],
                          config.url, result["store_id"], result["revision"]))
                return 0
            else:
                host = settings.get("host")
                command = settings.get("command")
                if not host or not command:
                    raise StoreError("ssh central settings need host and command")
                result = transfer_module.ssh_sender(store, args.to, host, command, origin,
                                                    args.label or "collector", progress=progress)
            print("delivered=%d pending=%d sent_bytes=%d raw_bytes=%d compression=%s endpoint=%s" % (
                len(result["delivered"]), store.pending_transfers(result["endpoint"]),
                result["sent_bytes"], result["raw_bytes"], result["compression"] or "none", result["endpoint"]))
        return 0
    except (OSError, StoreError, transfer_module.TransferError, mirror_module.MirrorError,
            mirror_module.mirror_crypto.CryptoError, archive_module.ArchiveError) as exc:
        print("agent-sessions sync: " + str(exc), file=sys.stderr)
        return 1
    finally:
        lock.close()


def _write_config(document):
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _mirror_configure(args):
    if not args.url or not args.token_file or not args.recipients or not args.identity:
        print("agent-sessions mirror: configure needs --url, --token-file, --recipients, and --identity",
              file=sys.stderr)
        return 2
    document = load_config()
    central = document.get("central")
    previous = central if isinstance(central, dict) else {}
    block = {
        "transport": "cloudflare", "url": args.url,
        "service_token_file": str(Path(args.token_file).expanduser().resolve()),
        "age_recipients_file": str(Path(args.recipients).expanduser().resolve()),
        "age_identity_file": str(Path(args.identity).expanduser().resolve()),
        "learning_projection": bool(previous.get("learning_projection", False)),
        "enabled": bool(previous.get("enabled", True)),
    }
    try:
        config = mirror_module.decode_config(block)
        mirror_module.load_service_token(config.service_token_file)
        mirror_module.mirror_crypto.load_recipients(config.age_recipients_file)
        mirror_module.mirror_crypto.recipient(config.age_identity_file)
    except (mirror_module.MirrorError, mirror_module.mirror_crypto.CryptoError, OSError) as exc:
        print("agent-sessions mirror: " + str(exc), file=sys.stderr)
        return 1
    document["central"] = block
    _write_config(document)
    print("configured=1 url=%s token=%s recipients=%s identity=%s projection=%d" % (
        config.url, config.service_token_file, config.age_recipients_file,
        config.age_identity_file, int(config.learning_projection)))
    return 0


def _mirror_keygen(args):
    if not args.identity:
        print("agent-sessions mirror: keygen needs --identity", file=sys.stderr)
        return 2
    identity = Path(args.identity).expanduser()
    recipients = Path(args.recipients).expanduser() if args.recipients \
        else identity.with_name(identity.name + ".recipients")
    generated = [(identity, mirror_module.mirror_crypto.keygen(identity))]
    if args.recovery:
        recovery = Path(args.recovery).expanduser()
        generated.append((recovery, mirror_module.mirror_crypto.keygen(recovery)))
    recipients.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = recipients.with_name(recipients.name + ".tmp")
    temporary.write_text("".join(recipient + "\n" for _path, recipient in generated))
    os.chmod(temporary, 0o600)
    os.replace(temporary, recipients)
    print("recipients=%s" % recipients)
    for path, recipient in generated:
        print("identity=%s recipient=%s" % (path, recipient))
    return 0


def _mirror_toggle(args, enabled):
    document = load_config()
    block = document.get("central")
    if not isinstance(block, dict):
        print("agent-sessions mirror: configure the mirror first", file=sys.stderr)
        return 1
    block = dict(block)
    block["enabled"] = enabled
    if args.searchable_learning:
        block["learning_projection"] = enabled
        if enabled:
            print("searchable-learning=1 disclosure=claims and applicability text will be stored in D1 and indexed by FTS5; "
                  "records still travel inside the encrypted recovery snapshot when this is off")
    try:
        mirror_module.decode_config(block)
    except mirror_module.MirrorError as exc:
        print("agent-sessions mirror: " + str(exc), file=sys.stderr)
        return 1
    document["central"] = block
    _write_config(document)
    queued = 0
    if enabled and args.searchable_learning:
        state = _state_dir(args)
        if (state / "sessions.sqlite3").is_file():
            with Store(state) as store:
                queued = learning_sync.requeue(store.db)
    print("enabled=%d searchable_learning=%d requeued=%d" % (
        int(enabled), int(bool(block.get("learning_projection"))), queued))
    return 0


def _mirror_status(args, store):
    report = mirror_module.pending_report(store, args.remote)
    document = dict(report)
    document.update({"config": False, "enabled": False, "token_mode": None, "remote_ok": False,
                     "remote_error": None, "last_verified_snapshot": None, "learning_revision": None,
                     "remote_revision": None})
    config = None
    try:
        config = mirror_module.load_config(config_path())
    except (mirror_module.MirrorError, StoreError) as exc:
        document["remote_error"] = str(exc)
    if config is not None:
        document["config"] = True
        document["enabled"] = config.enabled
        try:
            info = Path(config.service_token_file).stat()
            document["token_mode"] = "%04o" % stat.S_IMODE(info.st_mode)
        except OSError:
            pass
        if not args.offline:
            try:
                with mirror_module.MirrorClient(config) as client:
                    hello = client.hello(report["origin"])
                    document["learning_revision"] = hello["learning"]["revision"]
                    document["remote_revision"] = hello["revision"]
                    rows = mirror_module.snapshots(config, client=client, limit=100)
                verified = [row["verified_at"] for row in rows if row["verified_at"] is not None]
                document["last_verified_snapshot"] = max(verified) if verified else None
                document["remote_ok"] = True
            except (mirror_module.MirrorError, mirror_module.mirror_crypto.CryptoError) as exc:
                document["remote_error"] = str(exc)
    if args.json:
        print(json.dumps(document, sort_keys=True))
        return 0
    for key in ("remote", "enabled", "pinned", "origin", "store_id", "revision", "watermark", "learning_seq",
                "learning_revision", "remote_revision", "last_verified_snapshot", "last_sync_at", "last_error",
                "generations", "objects", "bytes", "tombstones", "learning_changes", "requests",
                "token_mode", "remote_ok", "remote_error"):
        print("%s=%s" % (key, document.get(key)))
    return 0


def _mirror_sync(args, store):
    import fcntl
    state = _state_dir(args)
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = open(state / "sync.lock", "a+")
    except OSError as exc:
        print("agent-sessions mirror: " + str(exc), file=sys.stderr)
        return 1
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if args.json:
                print(json.dumps({"overlapped": True, "pending": "skipped"}, sort_keys=True))
            else:
                print("overlapped=1 pending=skipped")
            return 0
        if args.dry_run:
            report = mirror_module.pending_report(store, args.remote)
            report["dry_run"] = True
            if args.json:
                print(json.dumps(report, sort_keys=True))
            else:
                print("dry_run=1 remote=%s pending_generations=%d pending_objects=%d pending_bytes=%d "
                      "learning_changes=%d tombstones=%d estimated_requests=%d" % (
                          args.remote, report["generations"], report["objects"], report["bytes"],
                          report["learning_changes"], report["tombstones"], report["requests"]))
            return 0
        config = mirror_module.load_config(config_path())
        if not config.enabled:
            if args.json:
                print(json.dumps({"enabled": False, "skipped": True}, sort_keys=True))
            else:
                print("enabled=0 skipped=1")
            return 0
        if args.json:
            def progress(count):
                return None
        else:
            def progress(count):
                if count % 25 == 0:
                    print("delivered=%d" % count, file=sys.stderr, flush=True)
        if args.reconcile:
            result = mirror_module.reconcile(store, config, args.remote, progress=progress)
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                print("reconciled=1 current=%d records=%d missing=%d extra=%d changed=%d chunks=%d "
                      "applied=%d revision=%d epoch=%d" % (
                          int(result["current"]), result["records"], result["missing"], result["extra"],
                          result["changed"], result["chunks"], result["applied"], result["revision"],
                          result["epoch"]))
            return 0
        def refresh():
            store.refresh(roots=_roots(args), project=None)
        result = mirror_module.sync(
            store, config, args.remote, refresh=refresh, progress=progress)
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print("committed=%d duplicate=%d tombstoned=%d objects_stored=%d manifests=%d "
                  "bytes_plaintext=%d bytes_compressed=%d bytes_ciphertext=%d tombstones_fetched=%d "
                  "tombstones_pushed=%d learning_changes=%d learning_deletes=%d "
                  "snapshots_created=%d snapshots_deferred=%d unchanged=%d endpoint=%s store=%s revision=%d" % (
                      result["generations_committed"], result["generations_duplicate"],
                      result["generations_tombstoned"], result["objects_stored"], result["manifests_uploaded"],
                      result["bytes_plaintext"], result["bytes_compressed"], result["bytes_ciphertext"],
                      result["tombstones_fetched"], result["tombstones_pushed"],
                      result["learning_changes"], result["learning_deletes"],
                      result["snapshots_created"], result["snapshots_deferred"], int(result["unchanged"]),
                      config.url, result["store_id"], result["revision"]))
        return 0
    finally:
        lock.close()


def _mirror_doctor(args):
    ok = True

    def report(name, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print("%s=%s%s" % (name, "ok" if passed else "fail", (":" + detail) if detail else ""))

    config = None
    try:
        config = mirror_module.load_config(config_path())
        report("config", True)
    except (mirror_module.MirrorError, StoreError) as exc:
        report("config", False, str(exc))
    if config is not None:
        report("enabled", True, "%d" % int(config.enabled))
        try:
            mirror_module.mirror_crypto.binary()
            report("age", True)
        except mirror_module.mirror_crypto.CryptoError as exc:
            report("age", False, str(exc))
        try:
            info = Path(config.service_token_file).stat()
            mode = stat.S_IMODE(info.st_mode)
            report("token", stat.S_ISREG(info.st_mode) and not mode & 0o077, "%04o" % mode)
        except OSError as exc:
            report("token", False, str(exc))
        try:
            mirror_module.mirror_crypto.recipient(config.age_identity_file)
            report("identity", True)
        except (mirror_module.mirror_crypto.CryptoError, OSError) as exc:
            report("identity", False, str(exc))
        try:
            recipients = mirror_module.mirror_crypto.load_recipients(config.age_recipients_file)
            report("recipients", len(recipients) > 0, "%d" % len(recipients))
        except (mirror_module.mirror_crypto.CryptoError, OSError) as exc:
            report("recipients", False, str(exc))
    try:
        with Store(_state_dir(args)) as store:
            report("state", True)
            pending = mirror_module.pending_report(store, args.remote)
            print("origin=%s" % pending["origin"])
            print("pinned=%s store_id=%s revision=%s" % (
                pending["pinned"], pending["store_id"], pending["revision"]))
            print("pending_generations=%d pending_objects=%d pending_bytes=%d learning_changes=%d tombstones=%d" % (
                pending["generations"], pending["objects"], pending["bytes"],
                pending["learning_changes"], pending["tombstones"]))
            print("last_error=%s" % pending["last_error"])
            if config is not None and not args.offline:
                try:
                    with mirror_module.MirrorClient(config) as client:
                        client.hello(pending["origin"])
                    report("remote", True)
                except (mirror_module.MirrorError, mirror_module.mirror_crypto.CryptoError) as exc:
                    report("remote", False, str(exc))
    except (OSError, StoreError, sqlite3.DatabaseError) as exc:
        report("state", False, str(exc))
    print("ok=%d" % int(ok))
    return 0 if ok else 1


def _mirror(args):
    try:
        if args.action in ("status", "adopt"):
            return _mirror_local(args)
        if args.action == "configure":
            return _mirror_configure(args)
        if args.action == "keygen":
            return _mirror_keygen(args)
        if args.action == "enable":
            return _mirror_toggle(args, True)
        if args.action == "disable":
            return _mirror_toggle(args, False)
        if args.action == "doctor":
            return _mirror_doctor(args)
        if args.action == "label":
            if len(args.label_args) != 2:
                print("agent-sessions mirror: label needs PROJECT_KEY NAME", file=sys.stderr)
                return 2
            with Store(_state_dir(args)) as store:
                learning_sync.ensure_schema(store.db)
                learning_sync.set_project_label(store.db, args.label_args[0], args.label_args[1])
            print("project=%s label=%s" % (args.label_args[0], args.label_args[1]))
            return 0
        if args.action == "sync":
            with Store(_state_dir(args)) as store:
                return _mirror_sync(args, store)
        config = mirror_module.load_config(config_path())
        with mirror_module.MirrorClient(config) as client:
            if args.action == "snapshots":
                rows = mirror_module.snapshots(config, client=client, limit=args.limit)
                for row in rows:
                    print("snapshot=%s created_at=%.0f db_size=%d objects=%d object_bytes=%d verified_at=%s revision=%d" % (
                        row["id"], row["created_at"], row["db_size"], row["objects"], row["object_bytes"],
                        row["verified_at"], row["revision"]))
                if not rows:
                    print("snapshots=0")
                return 0
            if args.action == "verify":
                result = mirror_module.verify(
                    config, objects=args.objects, sample=args.sample)
                print("generations=%d snapshots=%d objects=%d chunks=%d sampled=%d attested=%d bytes=%d" % (
                    result["generations"], result["snapshots"], result["objects"], result["chunks"],
                    result["sampled"], result["attested"], result["bytes"]))
                return 0
            if args.action == "gc":
                result = mirror_module.gc(config, client=client, apply=args.apply)
                print("apply=%d snapshots_retained=%d snapshots_pruned=%d objects_marked=%d "
                      "bytes_marked=%d objects_swept=%d bytes_swept=%d" % (
                          int(result["apply"]), result["snapshots_retained"], result["snapshots_pruned"],
                          result["objects_marked"], result["bytes_marked"], result["objects_swept"],
                          result["bytes_swept"]))
                return 0
            if args.action == "restore":
                if not args.snapshot or not args.to:
                    print("agent-sessions mirror: restore needs --snapshot and --to", file=sys.stderr)
                    return 2
                result = mirror_module.restore(
                    config, args.remote, args.snapshot, args.to, client=client,
                    identity_path=args.identity)
                print("snapshot=%s db_size=%d chunks=%d objects=%d replayed=%d tombstones=%d" % (
                    result["snapshot"], result["db_size"], result["chunks"], result["objects"],
                    result["replayed"], result["tombstones"]))
                return 0
            if args.action == "rotate":
                if not args.recipients or not args.identity:
                    print("agent-sessions mirror: rotate needs --recipients and --identity", file=sys.stderr)
                    return 2
                result = mirror_module.rotate(
                    config, recipients_file=args.recipients, identity_file=args.identity,
                    old_identity_file=args.old_identity, client=client)
                print("objects=%d rotated=%d" % (result["objects"], result["rotated"]))
                return 0
        print("agent-sessions mirror: unknown action " + args.action, file=sys.stderr)
        return 2
    except (OSError, sqlite3.DatabaseError, StoreError, mirror_module.MirrorError,
            mirror_module.mirror_crypto.CryptoError, archive_module.ArchiveError,
            backup_module.BackupError) as exc:
        print("agent-sessions mirror: " + str(exc), file=sys.stderr)
        return 1


def _mirror_local(args):
    with Store(_state_dir(args)) as store:
        if args.action == "status":
            return _mirror_status(args, store)
        config = mirror_module.load_config(config_path())
        origin = archive_module.origin_for(store.db)
        with mirror_module.MirrorClient(config) as client:
            hello = client.hello(origin)
        previous = store.mirror_remote_state(args.remote)
        cleared = 0
        if previous is not None and previous["store_id"]:
            cleared = store.remote_ack_count(previous["store_id"])
            store.clear_remote_acks(previous["store_id"])
        store.forget_mirror_remote(args.remote)
        store.write_mirror_remote(args.remote, hello["store_id"], hello["revision"], 0)
        print("adopted remote=%s store=%s revision=%d cleared_acks=%d" % (
            args.remote, hello["store_id"], hello["revision"], cleared))
    return 0


def _archive(args):
    try:
        with Store(_state_dir(args)) as store:
            if args.action == "verify":
                bad = 0
                for row in store.db.execute("SELECT id FROM archive_generation ORDER BY id"):
                    try:
                        archive_module.verify_generation(store.db, store.archive_dir, row["id"])
                    except (archive_module.ArchiveError, OSError) as exc:
                        bad += 1
                        print("generation=%s error=%s" % (row["id"], exc))
                print("generations_checked=%d failures=%d" % (store.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], bad))
                return 1 if bad else 0
            if args.action == "reindex":
                count = store.reindex_generations()
                print("generations_reindexed=%d sessions=%d" % (count, store.db.execute("SELECT count(*) FROM sessions").fetchone()[0]))
                return 0
            if args.action == "prune-legacy":
                total = files = redundant_files = redundant_bytes = retained_files = retained_bytes = 0
                for path, size, redundant in archive_module.legacy_copies(store.db, store.archive_dir):
                    files += 1
                    total += size
                    if not redundant:
                        retained_files += 1
                        retained_bytes += size
                        continue
                    redundant_files += 1
                    redundant_bytes += size
                    if args.apply:
                        path.unlink()
                if args.apply:
                    for root in sorted(store.archive_dir.iterdir()):
                        if root.name not in HARNESSES or root.name in DATABASE_HARNESSES:
                            continue
                        for directory, _dirnames, _filenames in os.walk(root, topdown=False):
                            try:
                                Path(directory).rmdir()
                            except OSError:
                                pass
                print("files=%d bytes=%d redundant_files=%d redundant_bytes=%d retained_files=%d retained_bytes=%d apply=%d"
                      % (files, total, redundant_files, redundant_bytes, retained_files, retained_bytes,
                         int(args.apply)))
                return 0
            report = archive_module.retention_report(store.db)
            print("generations=%d objects=%d bytes=%d tombstoned=%d" % (
                report["generations"], report["objects"], report["bytes"], report["tombstoned"]))
            print("pending_local_generations=%d" % store.db.execute(
                "SELECT count(*) FROM archive_generation WHERE indexed_at IS NULL AND harness NOT IN ('opencode')").fetchone()[0])
        return 0
    except (OSError, StoreError) as exc:
        print("agent-sessions archive: " + str(exc), file=sys.stderr)
        return 1


def _backup(args):
    try:
        if args.action == "create":
            with Store(_state_dir(args)) as store:
                manifest = backup_module.create(store, args.destination)
            raw_objects = manifest.get("objects")
            count = len(raw_objects) if isinstance(raw_objects, list) else 0
            print("backup_objects=%d generations=%s destination=%s" % (
                count, manifest.get("generations"), args.destination))
            return 0
        if not args.to:
            raise StoreError("backup restore needs --to <new state directory>")
        tombstones = args.tombstones_from or (str(_state_dir(args)) if (_state_dir(args) / "sessions.sqlite3").is_file() else None)
        manifest = backup_module.restore(Path(args.destination), Path(args.to), tombstones)
        raw_objects = manifest.get("objects")
        count = len(raw_objects) if isinstance(raw_objects, list) else 0
        print("restored_generations=%s objects=%d destination=%s" % (
            manifest.get("generations"), count, args.to))
        return 0
    except (OSError, StoreError, backup_module.BackupError, ValueError) as exc:
        print("agent-sessions backup: " + str(exc), file=sys.stderr)
        return 1


def _search(args):
    state_dir = _state_dir(args)
    if args.central:
        try:
            rows = central_rows(args, "search", {
                "query": args.query, "project": args.central_project, "limit": args.limit,
                "regex": args.regex, "kinds": args.kind, "since": args.since})
            for row in rows:
                _print_central_row(row, args.query, args.regex)
            print("matches=%d scope=central" % len(rows))
            return 0
        except (OSError, StoreError, transfer_module.TransferError) as exc:
            print("agent-sessions search: " + str(exc), file=sys.stderr)
            return 1
    try:
        with Store(state_dir) as store:
            project = _project(args)
            _stats(store.refresh(roots=_roots(args), project=project, harnesses=_harnesses(args)))
            rows = store.search(args.query, project, limit=args.limit, regex=args.regex, kinds=args.kind, since=args.since)
            for row in rows:
                evidence = store.resolve_evidence(row["session"], row["position"], row["digest"])
                timestamp = evidence.get("timestamp") or datetime.datetime.fromtimestamp(row["updated"], datetime.timezone.utc).isoformat(timespec="seconds")
                source = _source_label(row.get("source"), row.get("thread_source"))
                parent = row.get("parent")
                print(
                    f"session={row['session']} byte={row['position']} kind={evidence['kind']} "
                    f"time={timestamp[:19]} source={source} project={_display_path(row['cwd'])} file={_display_path(row['source_path'])} harness={row['harness']}"
                )
                if parent:
                    print("parent=" + parent)
                print("  " + _snippet(evidence["text"], args.query, args.regex))
            print("matches=" + str(len(rows)))
    except (OSError, StoreError) as exc:
        print("agent-sessions search: " + str(exc), file=sys.stderr)
        return 1
    return 0


def _print_event(store, row, max_chars, query=None, regex=False):
    evidence = store.resolve_evidence(row["session"], row["position"], row["digest"])
    timestamp = evidence.get("timestamp") or datetime.datetime.fromtimestamp(row["updated"], datetime.timezone.utc).isoformat(timespec="seconds")
    source = _source_label(row.get("source"), row.get("thread_source"))
    print(
        f"session={row['session']} byte={row['position']} kind={evidence['kind']} "
        f"time={timestamp[:19]} source={source} project={_display_path(row['cwd'])} file={_display_path(row['source_path'])} harness={row['harness']}"
    )
    if row.get("parent"):
        print("parent=" + row["parent"])
    text = _snippet(evidence["text"], query, regex) if query is not None else redact(evidence["text"])
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "…"
    print("  " + text)


def _recent(args):
    if args.central:
        try:
            rows = central_rows(args, "recent", {"project": getattr(args, "central_project", None), "limit": args.limit,
                                                 "kinds": args.kind, "since": args.since})
            for row in rows:
                _print_central_row(row)
            print("sessions=%d scope=central" % len(rows))
            return 0
        except (OSError, StoreError, transfer_module.TransferError) as exc:
            print("agent-sessions recent: " + str(exc), file=sys.stderr)
            return 1
    try:
        with Store(_state_dir(args)) as store:
            project = _project(args)
            _stats(store.refresh(roots=_roots(args), project=project, harnesses=_harnesses(args)))
            rows = store.recent(project, limit=args.limit, kinds=args.kind, since=args.since)
            for row in rows:
                _print_event(store, row, 240)
            print("sessions=" + str(len(rows)))
        return 0
    except (OSError, StoreError) as exc:
        print("agent-sessions recent: " + str(exc), file=sys.stderr)
        return 1


def _show(args):
    if args.before < 0 or args.after < 0:
        parser().error("--before and --after must not be negative")
    if args.central:
        try:
            rows = central_rows(args, "show", {"session": args.session, "project": getattr(args, "central_project", None),
                                               "position": args.position, "before": args.before, "after": args.after})
            if not rows:
                raise StoreError("no events in the requested window")
            for row in rows:
                _print_central_row(row)
            return 0
        except (OSError, StoreError, transfer_module.TransferError) as exc:
            print("agent-sessions show: " + str(exc), file=sys.stderr)
            return 1
    try:
        with Store(_state_dir(args)) as store:
            project = _project(args)
            _stats(store.refresh(roots=_roots(args), project=project, harnesses=_harnesses(args)))
            rows = store.session_events(args.session, project, position=args.position, before=args.before, after=args.after)
            if args.kind:
                rows = [row for row in rows if row["kind"] in args.kind]
            if args.since is not None:
                rows = [row for row in rows if (_timestamp_epoch(row["timestamp"]) or row["updated"]) >= args.since]
            if not rows:
                raise StoreError("no events in the requested kind/time window")
            for row in rows:
                _print_event(store, row, args.max_chars)
        return 0
    except (OSError, StoreError, ValueError) as exc:
        print("agent-sessions show: " + str(exc), file=sys.stderr)
        return 1


def status_document(store):
    coverage = store.coverage()
    capture_failures = store.db.execute("SELECT count(*) FROM capture_failure").fetchone()[0]
    unindexed = store.db.execute(
        "SELECT count(*) FROM archive_generation WHERE indexed_at IS NULL AND harness NOT IN ('opencode')").fetchone()[0]
    pending = store.db.execute(
        "SELECT coalesce(remote, 'unattempted') AS remote, count(*) AS pending, max(durable_at) AS last_ack "
        "FROM archive_generation g LEFT JOIN transfer_generation t ON t.generation = g.id "
        "WHERE t.durable_at IS NULL GROUP BY coalesce(remote, 'unattempted')").fetchall()
    tombstones = store.db.execute("SELECT count(*) FROM tombstone").fetchone()[0]
    unmapped = store.db.execute(
        "SELECT count(DISTINCT cwd) FROM sessions WHERE project IS NULL AND cwd != ''").fetchone()[0]
    artifacts = store.db.execute("SELECT count(*), coalesce(sum(size), 0) FROM archive_artifact").fetchone()
    return {
        "harness": [dict(row) for row in coverage],
        "capture_failures": capture_failures,
        "unindexed_generations": unindexed,
        "pending_remote": [dict(row) for row in pending],
        "unmapped_workspaces": unmapped,
        "pending_deletions": tombstones,
        "deletions": store.tombstone_states(),
        "artifacts": {"count": artifacts[0], "bytes": artifacts[1]},
        "archive": archive_module.retention_report(store.db),
        "learning_synced": [dict(row) for row in store.db.execute("SELECT remote, digest, durable_at FROM transfer_learning")]
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'transfer_learning'").fetchone() else [],
        "identity": archive_module.origin_for(store.db),
        "backup": {"builtin": True},
        "integrity": store.db.execute("PRAGMA quick_check").fetchone()[0],
    }


def _doctor(args):
    state_dir = _state_dir(args)
    try:
        if args.rebuild_index:
            rebuild_index(state_dir)
        with Store(state_dir) as store:
            if args.json:
                document = status_document(store)
                print(json.dumps(document, sort_keys=True))
                return 0 if document["integrity"] == "ok" else 1
            check = store.db.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise StoreError("SQLite integrity check failed: " + str(check))
            try:
                store.db.execute("INSERT INTO events_fts(events_fts, rank) VALUES('integrity-check', 1)")
            except sqlite3.DatabaseError as exc:
                raise StoreError("FTS content is inconsistent; run `agent-sessions doctor --rebuild-index`: " + str(exc)) from exc
            sessions = store.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
            events = store.db.execute("SELECT count(*) FROM events").fetchone()[0]
            malformed = store.db.execute("SELECT coalesce(sum(malformed_lines), 0) FROM sessions").fetchone()[0]
            partial = store.db.execute("SELECT count(*) FROM sessions WHERE partial_bytes > 0").fetchone()[0]
            print("ok sqlite=" + store.db.execute("SELECT sqlite_version()").fetchone()[0])
            print("database=" + str(store.path))
            print("sessions=" + str(sessions) + " events=" + str(events) + " malformed=" + str(malformed) + " partial=" + str(partial))
            for row in store.coverage():
                print("harness=%(harness)s sessions=%(sessions)d events=%(events)d complete=%(complete)d degraded=%(degraded)d unsupported=%(unsupported)d unrecognized_events=%(unrecognized)d unknown_ancestry=%(unknown_ancestry)d" % row)
            for row in store.unrecognized_kinds():
                print("unrecognized harness=%(harness)s kind=%(kind)s events=%(events)d" % row)
            return 0
    except (OSError, StoreError) as exc:
        print("agent-sessions doctor: " + str(exc), file=sys.stderr)
        print("For a valid but stale index, run `agent-sessions doctor --rebuild-index`; this preserves learning tables.", file=sys.stderr)
        return 1


def _refresh(args):
    try:
        with Store(_state_dir(args)) as store:
            _stats(store.refresh(roots=_roots(args), project=_project(args), harnesses=_harnesses(args)))
        return 0
    except (OSError, StoreError) as exc:
        print("agent-sessions refresh: " + str(exc), file=sys.stderr)
        return 1


def _readme_path():
    return Path(__file__).resolve().parents[2] / "README.md"


def parser():
    result = argparse.ArgumentParser(
        prog="agent-sessions",
        description="Search and inspect the local agent session index: every Codex, Claude, Muse, Grok, OpenCode, and Pi transcript on this machine.",
        epilog="Examples:\n"
        "  agent-sessions search 'codegen-units' --all-projects --kind user\n"
        "  agent-sessions search 'thin lto / codegen-units' --all-projects --kind user --since 1d\n"
        "  agent-sessions recent --kind user --since 2026-10-25\n"
        "  agent-sessions show opencode:ses_abc123 --position 3553000\n"
        "\n"
        "Hyphenated identifiers and slashes are autoquoted, so codegen-units works\n"
        "unquoted; quoting still forces an exact phrase and AND/OR still combine terms.\n"
        "Use --kind user to find your own prompts, --all-projects to leave the current\n"
        "repository, and --regex only for pattern matching.\n"
        f"Full docs: {_readme_path()}.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = result.add_subparsers(dest="command", metavar="command")

    refresh = commands.add_parser("refresh", help="incrementally index transcripts")
    refresh.add_argument("--state-dir")
    refresh.add_argument("--root", action="append", metavar="[HARNESS=]PATH", help="transcript root, repeatable; HARNESS is codex, claude, muse, grok, opencode, pi, cmd or agy (default codex)")
    _add_scope(refresh)
    refresh.set_defaults(run=_refresh)

    search = commands.add_parser("search", help="search indexed transcript text")
    search.add_argument("query", help="words, hyphenated identifiers, or quoted phrases; AND/OR combine terms")
    search.add_argument("--state-dir", help="session index state directory")
    search.add_argument("--root", action="append", metavar="[HARNESS=]PATH", help="transcript root, repeatable; HARNESS is codex, claude, muse, grok, opencode, pi, cmd or agy (default codex)")
    _add_scope(search)
    search.add_argument("--regex", action="store_true", help="scan indexed text using a Python regular expression")
    search.add_argument("--kind", action="append", help="only return this event kind, e.g. user, assistant, tool-call, tool-output; repeatable")
    search.add_argument("--since", type=_since_value, help="only include event time on or after an ISO date/time or duration such as 30d")
    search.add_argument("--limit", type=int, default=20, help="max matches (default %(default)s)")
    search.set_defaults(run=_search)

    recent = commands.add_parser("recent", help="show the latest indexed event for each recent session")
    recent.add_argument("--state-dir")
    recent.add_argument("--root", action="append", metavar="[HARNESS=]PATH", help="transcript root, repeatable; HARNESS is codex, claude, muse, grok, opencode, pi, cmd or agy (default codex)")
    _add_scope(recent)
    recent.add_argument("--kind", action="append", help="only include sessions whose latest matching event has this kind")
    recent.add_argument("--since", type=_since_value, help="only include events on or after an ISO date/time or duration such as 30d")
    recent.add_argument("--limit", type=int, default=20)
    recent.set_defaults(run=_recent)

    show = commands.add_parser("show", help="show source-verified events around a session byte offset")
    show.add_argument("session", help="session id as printed by search, e.g. opencode:ses_abc123")
    show.add_argument("--state-dir", help="session index state directory")
    show.add_argument("--root", action="append", metavar="[HARNESS=]PATH", help="transcript root, repeatable; HARNESS is codex, claude, muse, grok, opencode, pi, cmd or agy (default codex)")
    _add_scope(show)
    show.add_argument("--position", type=int, help="anchor the window at this byte offset; default is latest event")
    show.add_argument("--before", type=int, default=3, help="events before the anchor (default %(default)s)")
    show.add_argument("--after", type=int, default=3, help="events after the anchor (default %(default)s)")
    show.add_argument("--kind", action="append", help="only display this event kind; repeatable")
    show.add_argument("--since", type=_since_value, help="only display events on or after an ISO date/time or duration such as 30d")
    show.add_argument("--max-chars", type=int, default=2000, help="max event text chars (default %(default)s)")
    show.set_defaults(run=_show)

    doctor = commands.add_parser("doctor", help="check the local index and SQLite FTS5")
    doctor.add_argument("--state-dir")
    doctor.add_argument("--json", action="store_true", help="print a machine-readable status document")
    doctor.add_argument("--rebuild-index", action="store_true", help="rebuild derived transcript tables while preserving learning tables")
    doctor.set_defaults(run=_doctor)

    memory = commands.add_parser("memory", help="list retained experience records exposed by the central store")
    memory.add_argument("--state-dir")
    memory.add_argument("--central-state")
    memory.add_argument("--limit", type=int, default=20)
    memory.set_defaults(run=_memory)

    identity = commands.add_parser("identity", help="show or regenerate this collector identity")
    identity.add_argument("action", nargs="?", choices=("show", "regenerate"), default="show")
    identity.add_argument("--state-dir")
    identity.add_argument("--label")
    identity.add_argument("--regenerate", action="store_true", help="mint a new collector id; recorded origins are unchanged")
    identity.set_defaults(run=_identity)

    project = commands.add_parser("project", help="map a logical project to a checkout on an origin host")
    project.add_argument("action", choices=("map", "list"))
    project.add_argument("--state-dir")
    project.add_argument("--origin", help="origin host id; defaults to this collector")
    project.add_argument("--name")
    project.add_argument("--checkout")
    project.set_defaults(run=_project_command)

    sync = commands.add_parser("sync", help="capture and deliver pending generations to the central store")
    sync.add_argument("--state-dir")
    sync.add_argument("--central-state", help="local central store directory (offline/local transport)")
    sync.add_argument("--to", default="central", help="configured remote name (default %(default)s)")
    sync.add_argument("--label", help="display label recorded with this collector")
    sync.add_argument("--dry-run", action="store_true", help="report pending work without transferring")
    sync.add_argument("--root", action="append", metavar="[HARNESS=]PATH")
    sync.set_defaults(run=_sync)

    mirror = commands.add_parser("mirror", help="inspect, verify, restore, or maintain the Cloudflare mirror")
    mirror.add_argument("action", choices=(
        "configure", "enable", "disable", "keygen", "status", "adopt", "sync", "verify",
        "restore", "snapshots", "gc", "rotate", "label", "doctor"))
    mirror.add_argument("label_args", nargs="*", help="PROJECT_KEY NAME for label")
    mirror.add_argument("--state-dir")
    mirror.add_argument("--remote", default="cloudflare", help="configured remote name (default %(default)s)")
    mirror.add_argument("--limit", type=int, default=100, help="snapshot rows to list (default %(default)s)")
    mirror.add_argument("--snapshot", help="snapshot id for restore")
    mirror.add_argument("--to", help="destination state directory for restore")
    mirror.add_argument("--objects", action="store_true", help="download and verify sampled objects")
    mirror.add_argument("--sample", type=int, help="objects to sample with --objects (default 5%% or 100)")
    mirror.add_argument("--apply", action="store_true", help="apply snapshot pruning and object sweep")
    mirror.add_argument("--recipients", help="age recipients file for rotate")
    mirror.add_argument("--identity", help="new age identity file for keygen, rotate, or restore")
    mirror.add_argument("--recovery", help="offline recovery age identity file for keygen")
    mirror.add_argument("--old-identity", help="old age identity file for rotate")
    mirror.add_argument("--url", help="cloudflare worker origin for configure")
    mirror.add_argument("--token-file", help="service token JSON file for configure")
    mirror.add_argument("--searchable-learning", action="store_true",
                        help="opt in to the searchable D1 projection (prints the disclosure)")
    mirror.add_argument("--json", action="store_true", help="print JSON for status, sync, or dry-run")
    mirror.add_argument("--offline", action="store_true", help="skip remote checks in status or doctor")
    mirror.add_argument("--dry-run", action="store_true", help="report the backlog without a network write")
    mirror.add_argument("--reconcile", action="store_true", help="replace the projection with the local live set")
    mirror.add_argument("--root", action="append", metavar="[HARNESS=]PATH",
                        help="capture sessions from this root during sync")
    mirror.set_defaults(run=_mirror)

    archive = commands.add_parser("archive", help="verify, reindex, or report the local archive")
    archive.add_argument("action", choices=("status", "verify", "reindex", "prune-legacy"))
    archive.add_argument("--apply", action="store_true", help="delete redundant legacy copies")
    archive.add_argument("--state-dir")
    archive.set_defaults(run=_archive)

    backup = commands.add_parser("backup", help="create or restore a consistent backup")
    backup.add_argument("action", choices=("create", "restore"))
    backup.add_argument("--state-dir")
    backup.add_argument("destination", help="backup directory to create, or an existing backup to restore")
    backup.add_argument("--to", help="restore destination for `backup restore`")
    backup.add_argument("--tombstones-from", help="merge newer tombstones from this state directory (defaults to --state-dir)")
    backup.set_defaults(run=_backup)
    return result


def _add_scope(command):
    command.add_argument("--harness", action="append", metavar="NAME", help="only index and search this harness; repeatable")
    command.add_argument("--project", metavar="PATH", help="scope to this project path; defaults to the current repository")
    command.add_argument("--all-projects", action="store_true", help="search every indexed project")
    command.add_argument("--central", action="store_true", help="query the configured central store instead of this machine")
    command.add_argument("--central-state", help="central store directory for the local transport")
    command.add_argument("--central-project", help="limit a central query to this mapped project name")


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 2
    if hasattr(args, "limit") and args.limit < 1:
        cli.error("--limit must be positive")
    if hasattr(args, "max_chars") and args.max_chars < 1:
        cli.error("--max-chars must be positive")
    if getattr(args, "all_projects", False) and getattr(args, "project", None):
        cli.error("--project and --all-projects cannot be used together")
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
