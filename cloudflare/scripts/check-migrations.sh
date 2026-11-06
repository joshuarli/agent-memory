#!/bin/sh
# Fail when an applied migration differs from the commit that added it.
set -eu

worker=$(CDPATH='' cd -P -- "$(dirname -- "$0")/.." && pwd)
cd "$worker"

fail () {
	>&2 printf 'migrations-check: %s\n' "$1"
	exit 1
}

command -v git >/dev/null 2>&1 || fail 'git is not installed'
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail 'not inside a git worktree'

files=$(git ls-files 'migrations/*.sql')
[ -n "$files" ] || fail 'no tracked migrations found'
prefix=$(git rev-parse --show-prefix)

for path in $files; do
	added=$(git log --diff-filter=A --format=%H -- "$path" | tail -n 1)
	[ -n "$added" ] || fail "$path has no adding commit"
	expected=$(git rev-parse "$added:$prefix$path")
	actual=$(git hash-object "$path")
	[ "$expected" = "$actual" ] || fail "$path changed since it was added in $added"
done

printf 'migrations-check: ok\n'
