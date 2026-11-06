#!/bin/sh
set -eu

root=$(CDPATH='' cd -P -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"

node cloudflare/scripts/offline-probe.mjs
uv run --offline --no-project --python '>=3.15' python -I tests/test_mirror.py \
	MirrorIntegrationTests MirrorCliTests MemoryGateTests LoadRunTests
uv run --offline --no-project --python '>=3.15' python -I tests/test_local_stack.py \
	MirrorLifecycleTests SecurityTests ReliabilityTests
