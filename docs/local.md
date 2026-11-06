# Local stack

The Worker tests and the Python mirror integration tests run the production bundle in workerd, not a stand-in. `cloudflare/local/` reads the built `worker.config.json` once, inlines the released module contents, and boots them through Miniflare 5 with local D1 and R2. Runtime settings come from that config, including the compatibility date and flags. The `local-stack` test asserts that the bundle the stack ran has the same SHA-256 as the one `cloudflare/scripts/bundle-digest.sh` would publish.

The stack runs as two workers backed by one D1 database and one R2 bucket:

- The **reader** worker carries the Access identity binding and is reached through the loopback edge. Its entry module also counts D1/R2 work and answers the test-only bench endpoints.
- The **collector** worker carries no static identity. The edge signs a short-lived RS256 assertion for a per-run service token; the Worker verifies it against the JWKS the edge serves through the outbound service.

Every other outbound request receives a 599, is recorded, and fails the stack at dispose. The edge itself binds only `127.0.0.1` and uses a per-run certificate made with `openssl`; clients trust it with `SSL_CERT_FILE`. Per-run token files and the CA live in the stack's state directory, mode 0600.

## The loopback Access edge

`cloudflare/local/edge.mjs` plays the Access edge for tests and local development. It validates `CF-Access-Client-Id` and `CF-Access-Client-Secret` against a per-run token table, forwards the request with a signed `Cf-Access-Jwt-Assertion`, and routes reader sessions through the reader worker. A request with neither credential nor a reader session is refused. The `--test-state` flag adds the test control API described below. The edge is not a general-purpose proxy; its only client is the mirror client or a test.

## Modes

| Mode | State | Use |
| --- | --- | --- |
| Ephemeral | A fresh `.work/tmp/stack-<pid>-<random>/`, deleted after a clean dispose | Worker tests and `make integration-local` |
| Persistent | `.work/local/dev/` on `127.0.0.1:8790` | `make dev-local`; survives a restart; `make reset-local` removes it |

`make dev-local` writes `.work/local/dev/endpoint.json` with the URL, CA file, per-origin token files, and a home directory. It also writes an `agent-sessions` config under that home, so:

```sh
HOME=.work/local/dev/home SSL_CERT_FILE=.work/local/dev/edge-cert.pem agent-sessions mirror sync
```

works without further configuration. The persistent D1 state is migrated with Cloudflare's local runner before the stack opens it. A busy port fails the target instead of choosing another.

## Faults

The edge can return 429/500/503 with `retry-after: 0`, drop a request before its body is read, or drop a response after the Worker processed it. The fault entry can make a D1 batch or an R2 `put`/`get` fail on a count plan. The test control API, available only with `--test-state`, exposes `/__test/d1` for queries and state edits, `/__test/r2` for object listing, reads, writes, and deletes, `/__test/tail` for the captured `stats.observe` events, and `/__test/faults` for the request/drop counters. Every control route is loopback-only.

## Checks

| Effect | Command |
| --- | --- |
| Stack and Worker tests | `make worker-test` |
| Offline mirror integration, including the Python client | `make integration-local` |
| Cost budgets and the schema guard | `make cost-check` |
| The 80,000-object cost workload and 30-day projection | `make perf-cost` |
| Persistent dev mode | `make dev-local`, then `make reset-local` |
| Full predeploy gate | `make check` |

`make integration-local` runs the Python integration classes under `cloudflare/scripts/offline-run.sh`: it removes `CF_*` and `CLOUDFLARE_*`, shadows `cf` with an exit-99 shim, and denies non-loopback network on macOS with `sandbox-exec` (Linux uses `unshare -rn`). A negative probe proves that loopback still works and a non-loopback connection does not.

## Limits

The local stack cannot reproduce every production property. D1 billing of rows from a rolled-back batch is a Cloudflare accounting rule; local `meta.rows_written` reflects the statements that ran, not the invoice. The Access edge implements the contract the Worker relies on, not Cloudflare's whole edge. The cost workload uses identity objects rather than age ciphertext so the measurement is the Worker's D1/R2 work; the restore checks the object digests without decrypting. Anything that depends on Cloudflare's network, its dashboard, or its billing must be checked against production.
