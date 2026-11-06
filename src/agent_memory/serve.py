#!/usr/bin/env python3
"""Fixed receiver entry point for the SSH transport.

Usage: agent-sessions-serve ingest [--protocol N] --origin ORIGIN [STATE_DIR]
The receiver is bound to configured origins; an unlisted peer is refused.
"""
import argparse
import sys

from agent_memory.store import Store, StoreError, default_state_dir
from agent_memory.transfer import PROTOCOL, receiver


def parser():
    result = argparse.ArgumentParser(prog="agent-sessions-serve")
    commands = result.add_subparsers(dest="command")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("--protocol", type=int, default=PROTOCOL)
    ingest.add_argument("--origin", action="append", required=True, help="origin id allowed to deliver; repeatable")
    ingest.add_argument("state_dir", nargs="?", default=str(default_state_dir()))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command != "ingest":
        print("agent-sessions-serve: expected the ingest command", file=sys.stderr)
        return 2
    try:
        with Store(args.state_dir) as store:
            receiver(store, sys.stdin.buffer, sys.stdout.buffer, allowed_origins=tuple(args.origin))
    except (StoreError, OSError) as exc:
        print("agent-sessions-serve: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
