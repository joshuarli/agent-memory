# Cloudflare operations

The `cloudflare/` project is the `usr-memories` Worker that receives the encrypted mirror and serves the learning projection. This repository builds and deploys it. Moving the source here did not create, rename, or migrate any Cloudflare resource.

## Production identity

| Item | Value |
| --- | --- |
| Worker | `usr-memories` |
| Custom domain | `memories.17166969.xyz` |
| D1 | binding `DB`, database `usr-memories`, ID `9870c4da-3758-4f0c-ab6d-2372eb34009b` |
| R2 | binding `BUCKET`, bucket `usr-memories` |
| Exposure | `workersDev: false`, `previewUrls: false` |
| Compatibility | date `2026-10-30`, `nodejs_compat` |
| Telemetry | tail consumer `stats-observe` |

`cloudflare/cloudflare.config.ts` is the only configuration. `make deploy` checks the values above against it before it builds anything.

## Commands

Run every command from the repository root. Scripts locate the project from their own path, so the checkout can be anywhere.

| Effect | Command |
| --- | --- |
| Local only | `make test`, `make lint`, `make worker-test`, `make migrate-local`, `make perf-local`, `make perf-worker` |
| Local, reads the account config | `make worker-check` (a production dry run that uploads nothing) |
| Remote, read-only | `make migrations-production`, `make observe`, `make smoke-deployed` |
| Production write | `make deploy`, `make perf-deployed`, `make migrate-production CONFIRM_DATABASE=<id>`, `make provision`, `make provision-access` |

Production writes run only when you invoke them. `test`, `sync`, and installation never deploy, migrate, or provision.

`make deploy` runs `cloudflare/scripts/deploy-gate.sh`, which refuses if `cf` is not authenticated, a production migration is pending, or the configuration lost a binding, the domain, or the exposure settings. It then builds the bundle once, runs every Worker suite against it, records its digest, deploys it with `cf deploy --prebuilt --mode production`, and fails if the digest changed in between. It finishes with the read-only smoke probe.

Apply pending migrations with `make migrate-production` before deploying. It lists the pending migrations and applies them only when `CONFIRM_DATABASE` matches the configured D1 ID. Migrations are numbered SQL files under `cloudflare/migrations/`; never edit or squash an applied one.

`make smoke-deployed` and `make perf-deployed` read the collector service token from `MEMORIES_TOKEN_FILE` (default `.work/secrets/collector.token` in this checkout, mode 0600).

## Rollback

Rolling back the Worker (`cf` or the dashboard) restores code only. D1 rows, R2 objects, and applied migrations stay as they are, so a schema change needs its own recovery plan before it ships. Never use provisioning, GC, or an archive restore as a rollback.
