#!/bin/sh
# Drive the mirror benchmark harness for one environment.
set -eu

mode="${1:-}"
case "$mode" in
	local|worker|deployed) ;;
	*) >&2 printf 'usage: perf.sh local|worker|deployed\n'; exit 2 ;;
esac

root=$(CDPATH='' cd -P -- "$(dirname -- "$0")/../.." && pwd)
repeats="${REPEATS:-5}"
records="${RECORDS:-1000}"
objects="${OBJECTS:-200}"
out="${OUT:-$root/.work/perf}"
tmp="${TMPDIR:-$root/.work/tmp}"
mkdir -p "$tmp" "$out"
if [ -z "${GIT_TEMPLATE_DIR:-}" ] && [ -d /opt/homebrew/share/git-core/templates ]; then
	GIT_TEMPLATE_DIR=/opt/homebrew/share/git-core/templates
	export GIT_TEMPLATE_DIR
fi

set -- "$mode" --repeats "$repeats" --records "$records" --objects "$objects" --out "$out"
if [ -n "${SCENARIO:-}" ]; then
	set -- "$@" --scenario "$SCENARIO"
fi
cd "$root"
TMPDIR="$tmp" exec uv run --no-project --python '>=3.15' python -I \
	agent-memory/eval/bench_mirror.py "$@"
