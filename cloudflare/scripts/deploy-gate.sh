#!/bin/sh
# Refuse to build or deploy when auth, migrations, or the config disagree.
set -eu

root=$(CDPATH='' cd -P -- "$(dirname -- "$0")/../../.." && pwd)
worker="$root/agent-memory/cloudflare"
cd "$worker"

fail () {
	>&2 printf 'deploy-gate: %s\n' "$1"
	exit 1
}

cf=${CF_BIN:-cf}
command -v "$cf" >/dev/null 2>&1 || fail 'cf is not installed'
command -v node >/dev/null 2>&1 || fail 'node is not installed'
command -v jaq >/dev/null 2>&1 || fail 'jaq is not installed'
command -v rg >/dev/null 2>&1 || fail 'rg is not installed'

"$cf" auth whoami >/dev/null 2>&1 || fail 'cf is not authenticated; run cf auth login'

[ -f cloudflare.config.ts ] || fail 'cloudflare.config.ts is missing; restore it from git'
database_id=$(node scripts/config.mjs database-id) || fail 'cannot read the D1 database id from cloudflare.config.ts'
[ -n "$database_id" ] || fail 'cloudflare.config.ts has no D1 database id'

pending=$("$cf" d1 migrations list "$database_id" 2>&1) || fail "cannot list D1 migrations: $pending"
names=$(printf '%s' "$pending" | jaq -r '.[].Name' 2>/dev/null) || fail 'cannot parse the D1 migration list'
if [ -n "$names" ]; then
	printf '%s\n' "$names" | while IFS= read -r name; do
		>&2 printf 'deploy-gate: pending migration %s\n' "$name"
	done
	fail 'apply pending migrations with make migrate before deploying'
fi

check () {
	if ! rg -q --fixed-strings "$1" cloudflare.config.ts; then
		fail "cloudflare.config.ts lacks $2"
	fi
}

check 'name: "usr-memories"' 'the worker name'
check 'memories.17166969.xyz' 'the custom domain'
check 'ACCESS_TEAM_DOMAIN: bindings.text' 'the Access team domain'
check 'ACCESS_AUD: bindings.text' 'the Access audience'
check 'DB: bindings.d1' 'the D1 binding'
check 'BUCKET: bindings.r2' 'the R2 binding'
check 'headSamplingRate: 0.25' 'logs at 0.25'
check 'headSamplingRate: 0.1' 'traces at 0.10'
check 'workersDev: false' 'workersDev=false'
check 'previewUrls: false' 'previewUrls=false'

printf 'deploy-gate: ok\n'
