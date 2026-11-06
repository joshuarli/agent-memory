# Campaign: Cloudflare local parity for agent-memory

## Status

- **Goal:** The production bundle runs in local workerd from the release config, with real local D1 and R2. The real Python client is tested against it offline, the November 5 cost fixes are guarded by executable tests, stats telemetry contracts are checked, and one `make check` gates deploys. The campaign ends with one cost reduction that is deployed and verified by `statsctl`.
- **Base revision:** `4b2eaa6`. Baseline green (see **Baseline**).
- **Accepted:** S1 (`aa32669`), S2 (`8ae85a2`), S3 (`87f2af1`), S4 (`4e5667f`), S5 (`b19be38`), S6 (`9a3bf90`), S7 (`a559546`). **Red:** none. **Parked:** none.
- **S1 evidence:** `make worker-test` 138 pass; bundle digest `3cca3896…` unchanged across the Miniflare bump; `make lint` green.
- **S2 evidence:** 87 mirror tests pass; `make perf-worker REPEATS=1` exits 0; `make worker-test` 138 pass; `make lint` green.
- **S3 evidence:** migration and schema tests 4 pass, including runner equivalence and the 0007→0009 upgrade; `make migrations-check` passes and rejects an edited migration; `make worker-test` 140 pass; `make lint` green.
- **S4 evidence:** `DevModeTests` pass (sync, restart on the same state, unchanged second sync, `reset-local`); `make worker-test` 140 pass; search bench 100 records exits 0; `make lint` green.
- **S5 evidence:** 12 local-stack lifecycle and security tests pass with direct D1/R2 assertions; 87 mirror tests and 140 Worker tests pass; `make lint` green.
- **S6 evidence:** `make integration-local` passes: 16 mirror integration tests and 19 local-stack tests under the offline sandbox, with the cf shim exiting 99 and the probe proving loopback works while non-loopback is blocked.
- **S7 evidence:** `make cost-check` 5 pass with pinned `meta.rows_written` budgets and a pinned index set; `make worker-test` 145 pass; `make lint` green.
- **Next slice:** S8.

## Charter

- Decide and keep going. Record a decision in **Decisions** only when a later slice depends on it.
- Execution grant: everything under **Execution boundary**. Park any item that needs more.
- Hard stops, each of which parks the item: an action outside **Execution boundary**, a production write outside slices S11 and S12, and a loop that cannot check anything.
- **Pushing:**
  - agent-memory: commit locally. Push `master` to `origin` only immediately before each production deploy (S11, S12), so production always runs a pushed revision.
  - `~/d/stats`: push its commits when its slice is accepted.
- Fix egregious problems you meet, each in its own commit. Egregious means one of these:
  - a Worker test file over 30 s
  - a request that writes more than twice the D1 rows `docs/cost.md` states for it
  - a test that leaves a process, port, or state directory behind
  - any plaintext memory, transcript, key, Access token, or service-token secret in a log, response, telemetry event, or kept artifact

  Record anything else under **Status** and leave it.
- Parity and protection come before optimization. Product-performance numbers are measured and logged, never gating, until S12.
- One writer by default. The parallel pairs are named in **Sequence**. Lanes use worktrees at `.work/wt/<lane>` and follow the `orchestrate` worktree lifecycle.
- A failing item gets its cause before any retry (`root-cause`). Allow 3 attempts per slice. Then park it on branch `park/<id>`, with the cause and a retry condition in **Status**. A shared blocker has one owner: the parent.
- After each accepted slice, run the parent steering checkpoint from `~/.claude/skills/orchestrate/references/campaigns.md`.
- Commits:
  - Conventional Commits, with the workstream as scope: `stack`, `migrations`, `e2e`, `cost`, `observe`, `gate`, `docs`.
  - The item ID goes in the body.
  - Code comments never cite this doc.
  - No formatters.
- Scratch, ephemeral stacks, and failure artifacts go under `.work/`, never `/tmp`. Run tests with `TMPDIR="$PWD/.work/tmp"`.

## Decisions

### Owner (2026-11-06)

1. **Stats is in scope.** The campaign may change, push, and deploy `~/d/stats`, including `make observe-deploy`. New `usr-memories` operation names are added to stats' `MEMORIES_OPERATIONS` (`~/d/stats/cloudflare/src/observe/core/policies.ts:50`) and `stats-observe` is deployed before any agent-memory Worker that emits them.
2. **agent-memory production writes** are allowed only in S11 and S12:
   - `make deploy`, which runs `make check` first.
   - `make migrate-production CONFIRM_DATABASE=<id>` for any migration. A migration that drops or rewrites data first needs a local upgrade test that names exactly what it removes and asserts that everything else survives.
   - Rollback with `cf` to the previous version when a verdict is `regressed` or the smoke probe fails.
   - Nothing else: no `provision`, no `provision-access`, no `perf-deployed`.
3. **Pushing** agent-memory happens only immediately before a production deploy.
4. **Final slice:** ship one cost reduction to `usr-memories` and verify it with `statsctl verify`.

### Prescribed

| # | Decision | Reason | Reverse if |
| --- | --- | --- | --- |
| P1 | Pin `miniflare` to exactly `5.20261104.0-beta` (devDependency). Every `new Miniflare` call site moves to the v5 `workers: [{ config, dev }]` API. | Miniflare 4's workerd refuses the production compatibility date: "requires compatibility date 2026-10-30, but the newest date supported by this server binary is 2026-08-06". Every test therefore hardcodes `2026-08-06`. The 5 beta booted the real bundle from `worker.config.json` with `2026-10-30` and `nodejs_compat` (proven during preflight). The siblings' pin, `5.20261023.3-beta`, predates the compatibility date. | A non-beta Miniflare 5 that supports the date ships. Move to it. |
| P2 | `cloudflare/local/` is the single reader of the built `.cloudflare/output/v0/workers/default/worker.config.json`, and every test, bench, and dev mode goes through it. It inlines module `contents`, drops `manifest.type`, sets `modulesRoot`, and passes the config as `workers[0].config` (see the reference code at the end). | One interpretation of the config. Miniflare 5 accepts that config shape natively. | — |
| P3 | Unrepresentable configuration fails loudly. Only `text`, `d1`, and `r2` env bindings, `tailConsumers: [{worker:"stats-observe"}]`, `domains`, `observability`, `exports.default` (worker, cache disabled), `workersDev`, and `previewUrls` are accepted. Any other key or binding type throws, naming the key. Remote bindings are off, and the dev registry is off (`unsafeRegisterWorker: false`, no registry path). | The draft's requirement. Miniflare 5 defaults remote bindings and registry to on. | — |
| P4 | **Local Access.** A loopback HTTPS edge (Node, in `cloudflare/local/`) plays the Cloudflare Access edge, and production code is not changed for it. A single loopback HTTPS edge handles both kinds of caller.<br><br>**Collectors.** The edge reads the client's `CF-Access-Client-Id`/`-Secret`, checks them against the per-run token table, and forwards the request with a `Cf-Access-Jwt-Assertion` signed by a per-run RSA key.<br><br>**JWT verification.** `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` are overridden as text bindings (the only override allowed, and only for those two names). The Worker fetches `https://<team>/cdn-cgi/access/certs`, which the workerd `dev.outboundService` answers. Every other outbound request returns 599, is recorded, and fails the run at dispose.<br><br>**Readers.** Reader identity uses Miniflare 5 `accessBindingService` for `ctx.access.getIdentity()`. That service is fed from a reader session the edge establishes; requests without a session get no identity.<br><br>**TLS.** The edge uses a per-run certificate made with `openssl`. Clients trust it with `SSL_CERT_FILE`. | The collector path was proven in preflight: a signed assertion passed auth, and a tampered signature returned 403. The CLI accepts only `https` origins (`src/agent_memory/mirror.py:216`). | — |
| P5 | **Fault injection** lives only in the harness:<br>• At the edge: drop the connection mid-body, drop the response after forwarding, return 429, 500, or 503 with `retry-after`.<br>• Through direct `getD1Database`/`getR2Bucket` state edits: delete or corrupt an object, corrupt a page.<br>• In an optional fault entry module, `cloudflare/local/fault-entry.mjs`. It imports the bundle's unchanged main module and wraps `env.DB.batch`, `env.BUCKET.put`, and `env.BUCKET.get` on a control plan. It is loaded only when a test asks for faults; the default stack runs the bundle's `mainModule` itself. | Production gets no bypass. | — |
| P6 | **Modes and state:**<br>• Ephemeral: state in a fresh `.work/tmp/stack-<pid>-<rand>/`, deleted on success. On failure it is kept, and its path and the workerd log are printed.<br>• Persistent: `.work/local/dev/` on `127.0.0.1:8790` (stats uses 8789). If the port is busy it fails.<br>Dev mode writes `.work/local/dev/endpoint.json` (`url`, `ca_file`, `token_files` per origin, `home`) and an `agent-sessions` config under `home`, so `HOME=<home> SSL_CERT_FILE=<ca_file> agent-sessions mirror sync` works unchanged. Test credentials exist only in the state directory, with mode 0600. | No new client code paths. | — |
| P7 | **Migrations.** Persistent dev state is migrated with Cloudflare's runner (`cf d1 migrations apply --local --persist-to`), the production path. Ephemeral tests keep `cloudflare/bench/seed.mjs`. A test proves the two produce an identical normalized `sqlite_master`. An applied migration is immutable, enforced by a check that fails when a tracked `cloudflare/migrations/*.sql` differs from the blob of the commit that added it. | `cf d1 migrations apply --local` works offline (preflight: rc 0 with network denied). The Cloudflare runner tracks only names, so git is the only place an alteration can be detected. | Miniflare 5 cannot open `cf`'s persisted state. Then dev mode applies `seed.mjs` plus a `d1_migrations` record, and the equivalence test is the guard. |
| P8 | **Telemetry contract.** The `stats.observe` reader rules are in `~/d/stats/cloudflare/src/observe/cloudflare/tail.ts:212-330`:<br>• At most one `kind:"operation"` message per invocation, with `traceId`, `spanId`, `operation` from the closed set, `synthetic: boolean`, and optional `rows: {read, written, statements}` as non-negative safe integers.<br>• Errors use the existing `reportError` shape.<br>A message that breaks any rule drops the whole invocation in stats. The Worker builds rows from each D1 result's `meta.rows_read`/`rows_written` and statement count, in the per-request accumulator next to `timing`. Publishing is wrapped so a throw cannot fail the request. | — | — |
| P9 | **Operation names.** Add at most 16 names, each matching `^[a-z]+\.[a-z]+$`, one per route family in `cloudflare/src/routes/*.ts`, to the existing five. Starting set: `sync.hello`, `sync.missing`, `sync.upload`, `snapshots.page`, `snapshots.verify`, `snapshots.gc`, `tombstones.commit`, `learning.state`, `learning.delta`, `learning.reconcile`, `memories.search`, `status.read`, `mcp.request`. Rename freely before the stats deploy (S8). They are frozen after it. | — | — |
| P10 | **Offline gate.** `cloudflare/scripts/offline-run.sh`:<br>• Unsets every `CF_*` and `CLOUDFLARE_*`.<br>• Puts a `cf` shim that exits 99 on `PATH` when `OFFLINE_FORBID_CF=1`.<br>• macOS: `sandbox-exec` with `(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))(allow network-outbound (remote unix-socket))`.<br>• Linux: `unshare -rn` if it works, else exit 2 naming the limitation.<br>A negative probe test proves that a non-loopback connect fails under it and loopback works. | Preflight: under that profile `curl https://1.1.1.1` failed (rc 7) and loopback HTTP returned 200. | — |
| P11 | **`make check`** is the predeploy gate:<br>• `lint`, `test-python`, `worker-test`, `migrations-check`, `integration-local` (under `offline-run.sh`), `cost-check`, and `config-check`.<br>• It never needs `cf` login.<br>• `make deploy` = `deploy-gate.sh` (remote reads), then `make check`, then the digest of the bundle `check` built, deploy `--prebuilt`, digest compare, smoke.<br>• `config-check` replaces `deploy-gate.sh`'s `rg` assertions with assertions on the parsed config from `scripts/config.mjs`.<br>• Slow benchmarks (`perf-*`, `perf-cost`) stay out. | One interpretation of the config. A Worker-only pass cannot hide a broken client. | — |
| P12 | No CI changes. | Owner's global rule. | — |
| P13 | **Python E2E.** It moves from `cloudflare/test/integration-server.mjs` (the Worker in Node, header identity) to the workerd stack. The `start_integration_server` contract becomes `node cloudflare/local/cli.mjs serve --ephemeral --origins <json>` printing `{"port":N,"ca_file":...,"token_files":{...}}`. Delete `integration-server.mjs`, and port `test/serve.mjs`'s counting endpoints (used by `eval/bench_mirror.py:556`) onto the stack. Bindings-only unit tests (`harness.mjs` `environment()` fakes) stay in Node as the fast layer. | Fidelity, without losing the fast tests. | — |

### Delegated (decide inside the bound, then log it in the commit body)

- **Stack modules.** File layout inside `cloudflare/local/`, in plain `.mjs` like the existing tests, with no new runtime dependency. Bound: under 900 lines in total.
- **Mapping existing tests onto the stack.** Bound: no assertion weakened. Where a Node-hosted test asserted against fakes that workerd makes unnecessary, move the assertion to real state.
- **Optimization candidate (S12).** Choose by the S10 report.
- **Real `stats-observe` in the loop (S9).** Attempt it by extending stats' `test/observe-e2e` harness to load agent-memory's built bundle from `AGENT_MEMORY_DIR`. Allow two attempts. If it fails, stats' unit tests on a recorded agent-memory event fixture are the proof.

## Baseline (2026-11-06, `4b2eaa6`, macOS, Node 26.12.1, uv 0.13.7, cf 1.0.0-beta.27)

| Command | Result | Wall |
| --- | --- | --- |
| `make worker-test` (build + 132 tests) | pass | 63 s |
| `TMPDIR=… python -I tests/test_mirror.py` (87 tests) | pass | 49 s |
| `make test-python` (all suites) | pass | 216 s |
| `make lint` (warm caches) | pass | 1 s |
| `make migrate-local` (fresh state, also under the network-deny sandbox) | pass | 2 s |
| `cd ~/d/stats/cloudflare && pnpm -s run test:observe` (155 tests) | pass | 6 s |
| Real bundle boot in Miniflare 5 + 3 requests (preflight probe) | pass | 0.7 s |

Known red: none.

Known gaps the campaign closes:

- `cloudflare/test/schema.test.mjs` applies only `0001_init.sql` and asserts its stale table list, a schema production can no longer reach.
- Every Worker test runs the bundle in Node except `runtime.test.mjs`.
- Identity in the E2E server is an unauthenticated `cf-access-client-id` header.

Already covered and reused:

- the 24 adversarial cases (`cloudflare/test/adversarial.test.mjs:223-695`)
- the rows-written bounds (`cloudflare/test/snapshots.test.mjs:650`, `cloudflare/test/generations.test.mjs:406`)
- the Python E2E against the bundle, with 10 collectors and injected faults (`tests/test_mirror.py:1629-2693`)

## Sequence

Each slice ends on its acceptance command plus the narrowest affected suite, run on the integrated tree. `make check` runs at milestones M1 (after S6), M2 (after S10), and before each deploy.

| ID | Slice | Owns | Prereq | Acceptance |
| --- | --- | --- | --- | --- |
| S1 | Pin Miniflare 5 (P1). Build `cloudflare/local/` topology, ephemeral mode, P3 rejection, outbound deny, tail capture worker named `stats-observe`. Move every `new Miniflare` site (the 13 files under `cloudflare/test/`) onto it. `runtime.test.mjs` boots the bundle at the config's compatibility date. New `cloudflare/test/local-stack.test.mjs`: an unknown binding type throws, the compatibility date and flags equal `worker.config.json`'s, an unexpected outbound fetch fails dispose, two stacks are isolated, and dispose leaves no state directory or process. **Also:** the bundle digest is unchanged by the Miniflare bump. | `cloudflare/package.json`, `package-lock.json`, `cloudflare/local/**`, `cloudflare/test/**` | — | `make worker-test`, then `sh cloudflare/scripts/bundle-digest.sh` equal before and after the bump |
| S2 | Local Access edge (P4), fault hooks (P5), `cli.mjs serve --ephemeral`. Python suites move onto it (P13). Delete `test/integration-server.mjs` and port `serve.mjs`. | `cloudflare/local/**`, `tests/test_mirror.py`, `eval/bench_mirror.py`, `cloudflare/test/serve.mjs`, `cloudflare/test/integration-server.mjs` | S1 | `TMPDIR=… python -I tests/test_mirror.py` and `make perf-worker REPEATS=1` |
| S3 | Migrations (P7). New `cloudflare/test/migrations.test.mjs`:<br>• fresh `0001..N` in order<br>• runner equivalence<br>• upgrade from a database persisted at `0007` that holds rows, with the restart between applies<br>• re-running finds nothing pending<br>• `0008` destroys `snapshot`, `snapshot_chunk`, and `snapshot_object` rows exactly as intended and keeps `remote_object`, `generation*`, `tombstone`, `collector`, and learning rows<br>• `0009` drops only `last_referenced_at`<br>Rewrite `schema.test.mjs` against the final schema. Add `cloudflare/scripts/check-migrations.sh` and `make migrations-check`. | `cloudflare/test/migrations.test.mjs`, `cloudflare/test/schema.test.mjs`, `cloudflare/scripts/check-migrations.sh`, `Makefile` (target only) | S1 | `cd cloudflare && node --test test/migrations.test.mjs test/schema.test.mjs && cd .. && make migrations-check` |
| S4 | Persistent dev mode: `make dev-local`, `make reset-local`, and `endpoint.json` (P6). Two origins configured. | `cloudflare/local/**`, `Makefile` | S2 | New `tests/test_local_stack.py DevModeTests`. It starts `dev-local`, the CLI syncs from origin A, the stack stops and restarts on the same state, a second sync uploads nothing new, and `reset-local` empties state. |
| S5 | E2E lifecycle and security in new `tests/test_local_stack.py`. Each scenario asserts D1 rows and R2 keys directly, not only the HTTP status. See the **Acceptance** map. | `tests/test_local_stack.py`, `tests/testkit.py` | S2 | `TMPDIR=… python -I tests/test_local_stack.py` |
| S6 | E2E reliability: the fault scenarios in the **Acceptance** map. `make integration-local` runs `test_mirror.py`'s integration classes and `test_local_stack.py` under `offline-run.sh` (P10) with `OFFLINE_FORBID_CF=1`, plus the negative probe. | `tests/test_local_stack.py`, `cloudflare/scripts/offline-run.sh`, `Makefile` | S5 | `make integration-local` |
| S7 | Cost invariants (fast) in new `cloudflare/test/cost.test.mjs` on the workerd stack, using real `meta.rows_written` per statement. See **Acceptance** A6. Add `make cost-check`. | `cloudflare/test/cost.test.mjs`, `Makefile` | S1 | `make cost-check` |
| S8 | **Stats side, in `~/d/stats`:**<br>• Add the P9 names to `MEMORIES_OPERATIONS`.<br>• Add tests in `test/observe/` that usr-memories `operation` messages with rows are accepted per name, and that an unknown name is rejected.<br>• Run `make cloudflare-test lint`, commit, push.<br>• `make observe-deploy` (authorized).<br>Record the deployed `stats-observe` version under **Status**. | `~/d/stats/cloudflare/src/observe/core/policies.ts`, `~/d/stats/cloudflare/test/observe/**` | — | `cd ~/d/stats && make cloudflare-test lint` and the deploy prints a version ID |
| S9 | **Worker side:**<br>• Emit operation messages (P8, P9). Rows are accumulated from D1 meta.<br>• Contract test `cloudflare/test/observe-contract.test.mjs`: through the stack's capture tail, each instrumented route emits at most one operation message, with a name from a list asserted equal to stats' `MEMORIES_OPERATIONS`. It reads `~/d/stats` when `STATS_DIR` is set, and otherwise uses the recorded fixture `tests/fixtures/stats_observe_events.json`. Rows equal the D1 meta sums, and no event field contains a token, assertion, digest-bearing URL query, or plaintext.<br>• A publisher that throws leaves the response unchanged.<br>• Then the delegated real-`stats-observe` attempt. | `cloudflare/src/observe-channel.ts`, `cloudflare/src/index.ts`, `cloudflare/src/routes/*.ts`, `cloudflare/src/db.ts`, `cloudflare/test/observe-contract.test.mjs`, `tests/fixtures/stats_observe_events.json` | S1, S8 | `cd cloudflare && STATS_DIR=~/d/stats node --test test/observe-contract.test.mjs test/observe.test.mjs test/observe-channel.test.mjs` |
| S10 | **Cost workload (measurement).** `make perf-cost` drives the workerd stack with a synthetic mirror of 80,000 objects:<br>• initial backfill<br>• 24 hourly incremental syncs (2 new generations each)<br>• 1 daily snapshot<br>• GC mark and sweep<br>• a tombstone<br>• a learning delta<br>• a full restore into a second host<br>Per operation it records exact runtime counts: statements, batches, D1 `rows_read`/`rows_written`, R2 ops by method (Class A: put, list, multipart. Class B: get, head), R2 bytes in and out, duration, retries, and failures. Then a 30-day projection (estimate, labeled) and prices from `~/d/stats/cloudflare/src/cost/pricing.ts` (`PRICING_REVISION`, cited). Rolled-back batch rows are counted as billed (`docs/cost.md`). Output: `.work/perf/cost.json` and `.work/perf/cost.md`. | `eval/bench_cost.py` (or extend `eval/bench_mirror.py`), `cloudflare/local/**` counters, `Makefile` | S2, S7 | `make perf-cost` completes in under 15 min, and `jaq '.operations | length' .work/perf/cost.json` is at least 8 |
| S11 | **Hardening and the instrumented deploy.**<br>• `make check` (P11), `config-check`, `deploy` rewired.<br>• Docs: `docs/operations.md` (commands, failure recovery, deploy order "stats-observe first"), new `docs/local.md` (local architecture and testing), the `AGENTS.md` checks table, the `docs/cost.md` "Watching it" section.<br>• Then: push, `make deploy`, record the version under **Status**. | `Makefile`, `cloudflare/scripts/deploy-gate.sh`, `cloudflare/scripts/config.mjs`, `docs/**`, `AGENTS.md`, `README.md` | S3–S10 | `make check` green. `make deploy` exits 0, including `smoke-deployed`. |
| S12 | **Verified cost reduction (optimization).** Pick the operation with the largest projected 30-day D1 rows written (or cost) in `.work/perf/cost.json`. Implement a change that cuts its local measured rows written by ≥20% (accept ≥10% if nothing reaches 20%). It may include a migration (Owner 2). It needs a `cost.test.mjs` assertion that locks the new bound. If no candidate reaches 10%, record "no material candidate" with the numbers and skip the deploy. Otherwise:<br>1. Write `bench/policies/usr-memories-<id>.json` in `~/d/stats`: `statsctl-policy/1`, project `usr-memories`, operation `global` (or a per-operation metric, if S9 plus a stats change made usr-memories operation summaries verifiable), metric `d1.write` (or `cost`), direction `decrease`, `minChangePercent` 10, window `24h`, `maxWaitMs` 172800000. Commit it in stats before the deploy, then push.<br>2. Wait until at least 24 h have passed since the S11 deploy.<br>3. `make check`, push, `make migrate-production` if needed, `make deploy`.<br>4. Inside the warm-up: `statsctl check ingest --json > .work/statsctl/check.json`.<br>5. `statsctl verify --project usr-memories --deployment <id> --policy … --safety-check-file … --wait --timeout 48h --json`. | the chosen source files, `cloudflare/test/cost.test.mjs`, a new migration if needed, `~/d/stats/bench/policies/` | S11 | Local: `make check`, plus the `cost.test.mjs` bound. Production: `statsctl verify` exit 0 (`improved`). |
| S13 | **Close.**<br>• `dead-code-archaeology`: confirm no remaining reference to `integration-server.mjs`, `serve.mjs`'s old Miniflare, `deploy-gate.sh`'s `rg` assertions, or the `2026-08-06` literal (`rg -n '2026-08-06\|integration-server' -g '!node_modules'` returns nothing).<br>• `ultrareview` of the campaign diff; fix P1 and P2 findings.<br>• Delete `.work/local/dev` and ephemeral state.<br>• Final report. | — | S12 | `make check` green, and `rg` returns nothing |

**Parallel lanes:**

- S3 may run beside S2 or S4. S3 owns only its files and the `migrations-check` Makefile target, so add that target in S3's merge.
- S8, in another repo, may run beside anything.
- S7 may run beside S4–S6.

Everything else is serial.

**S12 verdict handling:**

- Exit 0 (`improved`): done.
- Exit 6 (`unchanged` or `confounded`): record the verdict and evidence. Allow one rerun on the next 24 h window, if the workload was stable. Otherwise close with the verdict recorded.
- Exit 7 (`regressed`): roll back the Worker with `cf` to the S11 version, smoke it, and park S12 with the evidence. A schema migration is not rolled back; it must have been compatible with the S11 code, and the S12 local tests prove that before deploying.
- Exit 5 after the timeout: put the printed resume command under **Status**. Closing with S12 pending is allowed.
- Exit 3 (not signed in to stats): park, and add it to **Needs environment**.

## Acceptance

Every deliverable, scenario, and invariant from the source draft, mapped to proof.

**A1. Fresh offline checkout.** `make worker-deps && make check` passes with network denied: `make integration-local` runs under `offline-run.sh`, and `make check` contains no remote call (the `cf` shim proves it in integration). On macOS, `cloudflare/scripts/offline-run.sh make check` passes after `worker-deps`. On Linux this goes to **Needs environment**.

**A2. Production-equivalent Worker in workerd.** S1: `local-stack.test.mjs` asserts the stack ran the exact modules listed in `worker.config.json`'s manifest, and that the bundle digest equals `bundle-digest.sh`'s, which is what `make deploy` publishes. It also asserts:
- `nodejs_compat` works, through a route that uses `node:diagnostics_channel`
- an unrepresentable binding fails
- no remote binding or registry is used
- each test gets isolated state and deterministic cleanup
- persistent mode survives a restart (S4)

**A3. Runtime settings come only from the release config.** `rg -n 'compatibilityDate|compatibilityFlags' cloudflare/test cloudflare/local cloudflare/bench eval tests` shows only reads of the parsed config. The only overrides are `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD`, and the stack enforces that.

**A4. Python collector E2E** (S5, S6). The real client runs over HTTPS against the workerd stack, with real local D1 and R2 and synthetic data. Each scenario asserts D1 and R2 state directly. Reuse an existing test where it already exists by moving it onto the stack; otherwise add it to `tests/test_local_stack.py`.

*Mirror lifecycle:*
- fresh initial sync
- encrypted upload, with the digest verified against the stored R2 object
- incremental sync with unchanged objects: zero uploads and zero new `generation_object` rows
- a modified session making a new generation
- snapshot pages and commit
- snapshot verification
- tombstone propagation
- GC mark, retention, and sweep
- full recovery into a second host
- learning projection sync, with recall correctness on the recovered host

*Reliability:*
- an interrupted object upload
- a response lost after a successful commit, where the retry re-acks with no extra rows
- a failed D1 batch (fault entry), leaving no partial state and a clean retry
- a failed or missing R2 object, detected by the client
- an invalid or corrupted snapshot page, refused
- duplicate requests and retries
- a client restart mid-sync
- a Worker restart on the same persisted D1 and R2 (persistent mode)
- backoff under repeated 429, 500, and 503, with the timing bounded and asserted
- recovery after an interrupted snapshot or generation
- two collectors trying to read each other's objects, which is refused

*Security* (through the edge):
- Access assertions that are missing, expired, wrongly signed, or have the wrong audience are refused, each with a 403 and no state change
- reader versus collector permissions
- cross-origin denial
- no unintended public endpoint: every route returns 403 unauthenticated, from the route list in `cloudflare/src/routes/*.ts`
- no sensitive data: a scan of telemetry captured from the tail, response bodies, and kept failure artifacts finds none of the token secret, JWT, age identity, or plaintext fixture strings

*Limits that cannot be emulated:* document each in `docs/local.md` and test the closest enforceable contract. These include Access edge behavior beyond the service-token and JWT contract, and D1 billing of rolled-back rows, which local D1 does not report.

**A5. Migrations** (S3): every bullet in S3's acceptance. No test uses a schema that production cannot reach by migrations: `schema.test.mjs` uses the full chain.

**A6. Cost invariants** (S7). Fast and deterministic; each asserts real D1 `rows_written`:
1. An unchanged mirror sync writes no `remote_object` or `generation_object` rows and fewer than 10 rows in total.
2. A snapshot commit writes no per-object D1 rows. Its rows are bounded by chunks plus pending objects plus a constant. The existing `snapshots.test.mjs:650` moves onto the stack.
3. A generation commit writes `generation_object` rows only for objects new to the session.
4. With 10× retained objects and no new objects, generation commit rows grow by under 1.5×.
5. Retrying a failed commit, or re-sending a committed one, does no second expensive write: the re-ack writes 0 rows.
6. GC never deletes an object reachable from a retained generation or snapshot page.
7. Schema guard: a test pins every table's index set and the rows per write for each hot statement. A new index or schema change fails until the test is updated with measured rows.

**A7. Stats telemetry** (S8, S9): stats accepts every name, the agent-memory contract test passes, and an instrumentation failure cannot break a request. Requests stay within the existing span-attribute allowlist (`cloudflare/src/observe.ts:1`), with no new tracing dependency. Overhead is negligible: the S10 report shows median request duration within 5% of the S2 `perf-worker` baseline (logged, not gating).

**A8. The single gate** (S11):
- `make check` passes with no production service and no `cf` login.
- `make deploy` runs it before deploying, then applies the digest compare.
- Production migration safeguards (`CONFIRM_DATABASE`) are unchanged.
- `make help` lists `check`, `dev-local`, `integration-local`, `reset-local`, `migrations-check`, `cost-check`, `config-check`, and `perf-cost`.

**A9. Compatibility:**
- `make test-python` passes.
- `make dist`, then the suites against the installed wheel, per the Packaging row in `AGENTS.md`.
- `tests/fixtures/mirror_protocol.json` parity passes (`tests/test_mirror.py` and `npm run test:protocol`).
- CLI entry points are unchanged.
- The `agent_memory.transcripts` and `agent_memory.types` imports are unchanged.

**A10. No unneeded infrastructure:**
- `cloudflare/local/` is under 900 lines.
- No Docker and no new services.
- Exactly one new devDependency change (the Miniflare pin) and no Python dependency.

**A11. Verified cost reduction:** S12's verdict and evidence are under **Status**.

**Final report** (S13):
- the architecture changes and why
- the commands that ran, with results
- the S10 measurements before and after S12
- remaining limitations
- the commit SHAs (agent-memory and stats)
- the deployed versions
- the `statsctl` verdict
- deferred work

## Needs environment

| Proof | Why it is not local | Owner command |
| --- | --- | --- |
| Linux offline sandbox (`unshare -rn`) | The executor runs on macOS | `cloudflare/scripts/offline-run.sh make check` on a Linux host |
| `statsctl` reads (S12) | Needs a signed-in Access session for `stats.17166969.xyz` | `! cf access login https://stats.17166969.xyz` before S12 |

## Execution boundary and non-goals

**Authorized:**

- Local builds and tests; writes under `.work/`.
- The `miniflare` pin change in `cloudflare/package.json` and its lockfile.
- Commits in this repo, and pushes only before S11 and S12 deploys.
- In `~/d/stats`: edits, commits, pushes, `make observe-deploy`, and read-only `statsctl` commands.
- For agent-memory in S11 and S12: `make deploy`, `make migrate-production CONFIRM_DATABASE=<id>`, and `cf` rollback of `usr-memories`.

**Forbidden:**

- `make provision`, `make provision-access`, `make perf-deployed`.
- Any synthetic write to production D1 or R2.
- Deploying the stats Worker (`make stats-deploy`) unless an S8 change requires it.
- Editing an applied migration.
- Changing the Worker name, domain, D1 identity, R2 bucket, Access settings, or the `stats-observe` tail consumer.
- Logging plaintext, keys, Access tokens, or bodies.
- CI changes.
- Docker or external databases.
- Changing the D1 storage engine. A Durable Object SQLite comparison is out of scope unless the S10 numbers show D1 is the dominant cost after S12. In that case record the numbers and stop.

**Contract changes, for the owner:**

1. `make deploy` now runs all of `make check`, so it is slower; S11 records the time.
2. New make targets (A8). `worker-test` runs the bundle in workerd.
3. The `start_integration_server` test contract, and the bench server endpoints (P13).
4. The Worker emits `operation` messages with D1 rows under new names (P9), and stats' accepted set grows to match.
5. `deploy-gate.sh` config assertions move to the parsed config.
6. The S12 optimization's behavior or schema change, written up in its commit and in `docs/cost.md`.

## Reference

Miniflare 5 boot, proven on `4b2eaa6` with `5.20261104.0-beta`:

```js
const config = JSON.parse(readFileSync(join(out, "worker.config.json"), "utf8"));
const { type: _type, ...manifest } = config.manifest;
for (const [name, module] of Object.entries(manifest.modules)) module.contents = readFileSync(join(out, "bundle", name), "utf8");
manifest.modulesRoot = join(out, "bundle");
new Miniflare({ workers: [{ config: { ...config, manifest, env: { ...config.env, ACCESS_TEAM_DOMAIN: { type: "text", value: team }, ACCESS_AUD: { type: "text", value: aud } } },
  dev: { outboundService: { type: "fetcher", handler } } }] });
```

D1 results expose `meta.rows_read` and `meta.rows_written` locally. `tailConsumers` was dropped in the probe; S1 must represent it with the capture worker, not drop it.

Code map:

| Concern | Location |
| --- | --- |
| Config parse and build | `cloudflare/scripts/config.mjs`, `cloudflare/scripts/build.mjs` |
| Deploy gates | `cloudflare/scripts/deploy-gate.sh`, `cloudflare/scripts/bundle-digest.sh` |
| JWT verification and JWKS fetch | `cloudflare/src/assertion.ts:67-124` |
| Reader identity | `cloudflare/src/auth.ts:29-43` |
| Channel | `cloudflare/src/observe-channel.ts` |
| Migration runner for tests | `cloudflare/bench/seed.mjs` |
| Python client config | `src/agent_memory/mirror.py:200-262` (https only, token file mode 0600) |
| Stats reference stack | `~/d/stats/cloudflare/local/topology.ts`, `~/d/stats/scripts/offline-run.sh` |
| Stats verify | `cd ~/d/stats && pnpm --dir cloudflare -s statsctl help verify` |
