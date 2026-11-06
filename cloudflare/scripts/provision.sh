#!/bin/sh
# Provision the usr-memories R2 bucket, D1 database, and Worker-level Access app.
set -eu

root=$(CDPATH='' cd -P -- "$(dirname -- "$0")/../../.." && pwd)
worker="$root/agent-memory/cloudflare"
cf=${CF_BIN:-cf}
dry_run=0
access=0
origin=synthetic-bench
label=synthetic
token_out="$root/.work/secrets/collector.token"

fail () {
	>&2 printf 'provision: %s\n' "$1"
	exit 1
}

say () {
	printf 'provision: %s\n' "$1"
}

while [ $# -gt 0 ]; do
	case "$1" in
		--dry-run) dry_run=1 ;;
		--access) access=1 ;;
		--origin) shift; origin=$1 ;;
		--label) shift; label=$1 ;;
		--token-out) shift; token_out=$1 ;;
		*) fail "unknown option: $1" ;;
	esac
	shift
done

case "$origin$label" in
	*[!A-Za-z0-9._-]*) fail 'origin and label must be alphanumeric, dot, dash, or underscore' ;;
esac

if [ "$dry_run" = 1 ]; then
	if [ "$access" = 0 ]; then
		say 'would create R2 bucket usr-memories when absent'
		say 'would create D1 database usr-memories when absent'
		say 'would write the D1 id to cloudflare.config.ts'
	else
		say 'would enroll service token usr-memories-collector when absent'
		say "would write its client id and secret to $token_out with mode 0600"
		say "would bind the token to origin $origin in D1"
		say "would create the Worker-level Access app usr-memories for ${OWNER_EMAIL:-the account owner} and the token"
		say "would set READERS to ${OWNER_EMAIL:-the account owner}"
	fi
	exit 0
fi

command -v "$cf" >/dev/null 2>&1 || fail "cf is not installed"
"$cf" auth whoami >/dev/null 2>&1 || fail 'cf is not authenticated; run cf auth login'
command -v node >/dev/null 2>&1 || fail 'node is not installed'
command -v jaq >/dev/null 2>&1 || fail 'jaq is not installed'

if [ "$access" = 0 ]; then
	buckets=$("$cf" r2 buckets list --name-contains usr-memories)
	if printf '%s' "$buckets" | jaq -e '.buckets[] | select(.name == "usr-memories")' >/dev/null 2>&1; then
		say 'R2 bucket usr-memories already exists'
	else
		say 'creating R2 bucket usr-memories'
		"$cf" r2 buckets create --name usr-memories >/dev/null
	fi
	databases=$("$cf" d1 list --name usr-memories)
	database_id=$(printf '%s' "$databases" | jaq -r '.[] | select(.name == "usr-memories") | .uuid' 2>/dev/null | head -1)
	if [ -z "$database_id" ]; then
		say 'creating D1 database usr-memories'
		created=$("$cf" d1 create --name usr-memories)
		database_id=$(printf '%s' "$created" | jaq -r '.uuid // .result.uuid // empty' 2>/dev/null)
		[ -n "$database_id" ] || fail 'could not read the new D1 database id'
	fi
	say "D1 database usr-memories is $database_id"
	[ -f "$worker/cloudflare.config.ts" ] || fail 'cloudflare.config.ts is missing; restore it from git'
	configured=$(node "$worker/scripts/config.mjs" database-id) || fail 'cannot read the D1 database id from cloudflare.config.ts'
	if [ "$configured" = "$database_id" ]; then
		say 'cloudflare.config.ts already has the D1 id'
	else
		say 'writing the D1 id to cloudflare.config.ts'
		node "$worker/scripts/config.mjs" set-database-id "$database_id" || fail 'could not update cloudflare.config.ts'
	fi
	exit 0
fi

[ -f "$worker/cloudflare.config.ts" ] || fail 'cloudflare.config.ts is missing; restore it from git'
workers=$("$cf" workers scripts search --name usr-memories)
worker_id=$(printf '%s' "$workers" | jaq -r '(.result? // .)[] | select(.script_name == "usr-memories" or .name == "usr-memories") | .id // .tag // empty' 2>/dev/null | head -1)
[ -n "$worker_id" ] || fail 'Worker usr-memories is not deployed; run make worker-deploy first'

owner_email=${OWNER_EMAIL:-$("$cf" auth whoami | jaq -r '.email // empty')}
[ -n "$owner_email" ] || fail 'OWNER_EMAIL is required'

tokens=$("$cf" zero-trust access service-tokens list --name usr-memories-collector 2>/dev/null || true)
token_id=$(printf '%s' "$tokens" | jaq -r '(.result? // .)[] | select(.name == "usr-memories-collector") | .id // empty' 2>/dev/null | head -1)

mkdir -p "$(dirname "$token_out")"
chmod 700 "$(dirname "$token_out")"
if [ -z "$token_id" ]; then
	say 'creating service token usr-memories-collector'
	created=$("$cf" zero-trust access service-tokens create --name usr-memories-collector)
	token_id=$(printf '%s' "$created" | jaq -r '(.result? // .) | .id // empty')
	client_id=$(printf '%s' "$created" | jaq -r '(.result? // .) | .client_id // empty')
	client_secret=$(printf '%s' "$created" | jaq -r '(.result? // .) | .client_secret // empty')
	[ -n "$token_id" ] && [ -n "$client_id" ] && [ -n "$client_secret" ] || fail 'could not read the new service token'
	(umask 077 && printf '{"client_id":"%s","client_secret":"%s"}\n' "$client_id" "$client_secret" > "$token_out")
else
	say 'service token usr-memories-collector already exists'
	[ -f "$token_out" ] || fail "token file is missing: $token_out"
	client_id=$(jaq -r '.client_id // empty' "$token_out")
	[ -n "$client_id" ] || fail "token file has no client_id: $token_out"
fi
chmod 600 "$token_out"

database_id=$(node "$worker/scripts/config.mjs" database-id) || fail 'cannot read the D1 database id from cloudflare.config.ts'
now=$(date +%s)
"$cf" d1 query "$database_id" --sql "INSERT OR REPLACE INTO collector(service_token_id, origin_host, label, created_at, disabled) VALUES ('$client_id', '$origin', '$label', $now, 0)" >/dev/null
say "bound service token client $client_id to origin $origin"

owner_body=$(jaq -n \
	--arg name 'usr-memories' \
	--arg worker "$worker_id" \
	--arg owner "$owner_email" <<'JQ'
{type:"self_hosted", name:$name,
 destinations:[{type:"worker", worker_id:$worker}],
 policies:[{name:"owner", decision:"allow", include:[{email:{email:$owner}}]}]}
JQ
)
collector_body=$(jaq -n \
	--arg name 'usr-memories' \
	--arg worker "$worker_id" \
	--arg owner "$owner_email" \
	--arg token "$token_id" <<'JQ'
{type:"self_hosted", name:$name,
 destinations:[{type:"worker", worker_id:$worker}],
 policies:[
   {name:"owner", decision:"allow", include:[{email:{email:$owner}}]},
   {name:"collector", decision:"non_identity", include:[{service_token:{token_id:$token}}]}
 ]}
JQ
)

apps=$("$cf" zero-trust access applications list --name usr-memories)
app_id=$(printf '%s' "$apps" | jaq -r '(.result? // .)[] | select(.name == "usr-memories") | .id // empty' 2>/dev/null | head -1)
if [ -n "$app_id" ]; then
	say 'Access application usr-memories already exists'
else
	say 'creating the Worker-level Access application usr-memories'
	created=$("$cf" zero-trust access applications create --body "$owner_body")
	app_id=$(printf '%s' "$created" | jaq -r '(.result? // .) | .id // empty')
	[ -n "$app_id" ] || fail 'could not read the new Access application id'
fi
say 'checking the Access policies'
current=$("$cf" zero-trust access applications get "$app_id")
bound=$(
	printf '%s' "$current" | jaq -r \
		'(.result? // .) | [.policies[]? | select(.decision == "non_identity") | .include[]? | .service_token.token_id // empty] | first // empty' \
		2>/dev/null
)
if [ "$bound" = "$token_id" ]; then
	say 'Access application already binds the owner and collector policies'
else
	say 'ensuring the owner and collector policies'
	"$cf" zero-trust access applications update "$app_id" --body "$collector_body" >/dev/null
fi

say "setting READERS to $owner_email"
"$cf" workers secrets update READERS --worker usr-memories --text "$owner_email" --type secret_text >/dev/null
say 'Access provisioning complete'
