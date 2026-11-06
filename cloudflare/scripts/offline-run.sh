#!/bin/sh
# Run a command with no outbound network except loopback and no Cloudflare CLI.
set -eu

if [ "$#" -eq 0 ]; then
	>&2 printf 'offline-run: usage: offline-run.sh COMMAND [ARG...]\n'
	exit 2
fi

root=$(CDPATH='' cd -P -- "$(dirname -- "$0")/.." && pwd)
repo=$(dirname "$root")

for name in $(env | sed -n 's/^\(CF_[A-Za-z0-9_]*\|CLOUDFLARE_[A-Za-z0-9_]*\)=.*/\1/p'); do
	unset "$name"
done

if [ "${OFFLINE_FORBID_CF:-0}" = "1" ]; then
	shim="$repo/.work/tmp/offline-bin"
	mkdir -p "$shim"
	printf '#!/bin/sh\nexit 99\n' >"$shim/cf"
	chmod +x "$shim/cf"
	PATH="$shim:$PATH"
	export PATH
fi

case "$(uname -s)" in
	Darwin)
		profile='(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))(allow network-outbound (remote unix-socket))'
		exec sandbox-exec -p "$profile" "$@"
		;;
	Linux)
		if unshare -rn true 2>/dev/null; then
			exec unshare -rn sh -c 'ip link set lo up 2>/dev/null || ifconfig lo up 2>/dev/null || true; exec "$@"' sh "$@"
		fi
		>&2 printf 'offline-run: unshare -rn is unavailable; cannot isolate the network\n'
		exit 2
		;;
	*)
		>&2 printf 'offline-run: unsupported platform %s\n' "$(uname -s)"
		exit 2
		;;
esac
