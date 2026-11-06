#!/bin/sh
set -eu

worker=$(CDPATH='' cd -P -- "$(dirname -- "$0")/.." && pwd)
output="$worker/.cloudflare/output"
[ -d "$output" ] || { >&2 printf 'bundle-digest: %s is missing; run make worker-bundle\n' "$output"; exit 1; }
cd "$output"
find . -type f | LC_ALL=C sort | while IFS= read -r file; do
	shasum -a 256 "$file"
done | shasum -a 256 | cut -d' ' -f1
