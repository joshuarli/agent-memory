#!/bin/sh
# Read-only probe of the deployed Worker with a synthetic collector token.
set -eu

base=${MEMORIES_BASE:-https://memories.17166969.xyz}
token_file=${MEMORIES_TOKEN_FILE:-.work/secrets/collector.token}
root=$(CDPATH='' cd -P -- "$(dirname -- "$0")/../../.." && pwd)
work="$root/.work/tmp"
mkdir -p "$work"
headers="$work/smoke-headers.$$"
body="$work/smoke-body.$$"
trap 'rm -f "$headers" "$body"' EXIT INT TERM

fail () {
	>&2 printf 'smoke-deployed: %s\n' "$1"
	exit 1
}

ok () {
	printf 'ok: %s\n' "$1"
}

command -v curl >/dev/null 2>&1 || fail 'curl is not installed'
command -v jaq >/dev/null 2>&1 || fail 'jaq is not installed'
command -v rg >/dev/null 2>&1 || fail 'rg is not installed'
[ -r "$token_file" ] || fail "token file is missing: $token_file"

mode=$(stat -c '%a' "$token_file" 2>/dev/null || stat -f '%Lp' "$token_file")
[ "$mode" = "600" ] || fail 'token file must have mode 0600'

client_id=$(jaq -r '.client_id // empty' "$token_file")
client_secret=$(jaq -r '.client_secret // empty' "$token_file")
[ -n "$client_id" ] || fail 'token file has no client_id'
[ -n "$client_secret" ] || fail 'token file has no client_secret'
case "$client_id$client_secret" in
	*[!A-Za-z0-9._-]*) fail 'token file has unexpected characters' ;;
esac

code=$(curl --silent --show-error --max-time 30 --output "$body" --dump-header "$headers" \
	--header "CF-Access-Client-Id: $client_id" --header "CF-Access-Client-Secret: $client_secret" \
	--write-out '%{http_code}' "$base/api/v1/status") || fail 'token request failed'
[ "$code" = "200" ] || fail "token status returned $code"
ok 'token status 200'

rg -qi '^cache-control:.*no-store' "$headers" || fail 'token response lacks no-store'
ok 'cache-control no-store'

rg -qi '^server-timing:.*d1;dur=' "$headers" || fail 'token response lacks Server-Timing'
ok 'server-timing reports d1'

jaq -e '.store_id == "usr-memories" and .ready == true and .principal.kind == "collector"' "$body" >/dev/null \
	|| fail 'token response is not a bound collector status'
ok 'collector principal is bound'

code=$(curl --silent --show-error --max-time 30 --output /dev/null --dump-header "$headers" \
	--write-out '%{http_code}' "$base/api/v1/status") || fail 'unauthenticated request failed'
case "$code" in
	403)
		ok 'unauthenticated request refused by the Worker'
		;;
	401)
		rg -qi '^www-authenticate:.*Cloudflare-Access' "$headers" || fail 'unauthenticated 401 did not come from Access'
		ok 'unauthenticated request refused by Access'
		;;
	301|302|303|307|308)
		rg -qi '^location:.*cloudflareaccess\.com' "$headers" || fail "unauthenticated redirect went to an unexpected location"
		ok 'unauthenticated request redirected to Access'
		;;
	*)
		fail "unauthenticated request returned $code"
		;;
esac

ok 'read-only probe completed'
