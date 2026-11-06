#!/bin/sh
set -eu

root=$(CDPATH='' cd -P -- "$(dirname -- "$0")/../.." && pwd)
if command -v rage >/dev/null 2>&1; then
	command -v rage
	exit 0
fi
if [ -x "${HOME}/.local/bin/rage" ]; then
	printf '%s\n' "${HOME}/.local/bin/rage"
	exit 0
fi
bin="$root/.work/tools/rage/bin/rage"
if [ -x "$bin" ]; then
	printf '%s\n' "$bin"
	exit 0
fi
if sh "$root/dl" rage >/dev/null 2>&1 && [ -x "${HOME}/.local/bin/rage" ]; then
	printf '%s\n' "${HOME}/.local/bin/rage"
	exit 0
fi
if ! command -v cargo >/dev/null 2>&1; then
	>&2 printf 'cargo is required to build rage into %s\n' "$root/.work/tools/rage"
	exit 1
fi
mkdir -p "$root/.work/tools" "$root/.work/tmp"
cd "$root"
CARGO_TARGET_DIR="$root/.work/tools/rage-target" cargo install rage --version 0.12.1 \
	--root "$root/.work/tools/rage" --locked
if [ ! -x "$bin" ]; then
	>&2 printf 'rage build did not produce %s\n' "$bin"
	exit 1
fi
printf '%s\n' "$bin"
