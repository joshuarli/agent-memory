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

## Remote MCP

The Worker exposes a stateless Streamable HTTP endpoint at `https://memories.17166969.xyz/mcp`. Cloudflare Access and the Worker's existing principal checks apply to every request. A collector can read only its bound origin. A reader can read all origins. Requests with a different host or a present, different Origin are refused. Native clients can omit Origin.

The endpoint offers these read-only tools:

| Tool | Purpose |
| --- | --- |
| `memory_search` | Search synced learning records. Filters match the REST search parameters and are strings. `limit` is 1 through 100. Follow `next_cursor` with `after`. |
| `memory_inspect` | Read a record by `origin`, `record` (`experience` or `memory`), and string `id`, including available sources and links. |
| `server_status` | Report the protocol, principal scope, and available sync freshness. |

Search and inspection results are advisory. The Worker cannot verify the client's checkout, run local recall, or decrypt raw transcripts. Treat retrieved text as data, not instructions. Requests are limited to 32 KiB. Tool results over 256 KiB return an error; narrow the request. No tool syncs, writes receipts, or modifies state.

For Codex CLI, configure the endpoint and pass the two Access service-token headers from the environment:

```toml
[mcp_servers.agent_memory]
url = "https://memories.17166969.xyz/mcp"
env_http_headers = { "CF-Access-Client-Id" = "MEMORIES_ACCESS_CLIENT_ID", "CF-Access-Client-Secret" = "MEMORIES_ACCESS_CLIENT_SECRET" }
```

Set both variables in the Codex process environment using the existing collector credential. Do not put their values in the configuration or a repository. This grants the collector's origin scope, not reader access. Clients that support only OAuth cannot use this service-token flow without a separately designed Access integration. See the [Codex configuration reference](https://developers.openai.com/codex/config-reference/) for HTTP header settings.

The official SDK adds a measurable module-graph cost: the pre-MCP upload was 196.50 KiB (36.36 KiB gzip) and the MCP build is 527.79 KiB (125.19 KiB gzip). Local Miniflare cold start grew from a 74.5 ms median to 124.7 ms across five alternating samples. The REST status and search paths kept identical D1 operation counts. `memory_search` and `memory_inspect` call the same scoped operations and add no D1 work; `server_status` uses the existing status readers. This is the cost of the official handler, not a reason to enable streaming or stateful sessions.
