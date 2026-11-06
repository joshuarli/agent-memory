# agent-memory

Local agent session memory (`src/agent_memory`) and its Cloudflare Worker (`cloudflare/`). The [README](README.md) describes behavior, [install](docs/install.md) the runtime environment, [operations](docs/operations.md) the Worker, and [cost](docs/cost.md) what it spends.

## Rules

- The Python package uses only the standard library and requires Python 3.15. Build-only tooling stays in `[build-system]`.
- Retrieval uses no models: no embeddings, rerankers, or model calls in `recall`, the prompt hook, or their benchmarks. Model review under `agent-learn enable` and the opt-in live evaluations are outside this rule.
- Shell scripts are POSIX `sh` and resolve paths from their own location.
- Scratch goes under `.work/` (ignored). Point `TMPDIR` at `.work/tmp` for tests.
- Never run a production write (`make deploy`, `migrate-production`, `provision`, `provision-access`, `perf-deployed`) unless the owner asked for it. Keep `cloudflare/cloudflare.config.ts` the single Worker configuration.
- The `usr` dotfiles repository consumes this package: its launchers run the installed commands, `skill-usage` imports `agent_memory.transcripts` and `agent_memory.types`, and promotion edits its skills. Treat those as public contracts.
- Tests never read `usr`. A synthetic promotion target lives in `tests/fixtures/usr_target`; update it when `usr`'s skill checker, launchd template, or tier table changes.

## Checks

| Change | Command |
| --- | --- |
| Any Worker or local-stack change | `make check` (the predeploy gate; no `cf` login) |
| Local stack or Access edge | `make worker-test`, `make integration-local`, `make lint` |
| Persistent dev mode | `make dev-local`, then `make reset-local` |
| Deploy configuration | `make config-check`, `make check` |
| Cost budgets | `make cost-check`; `make perf-cost` for the full workload |
| Any Python | `make test-python` (every `tests/test_*.py` and `eval/test_*.py`) and `make lint` |
| One suite | `TMPDIR="$PWD/.work/tmp" uv run --no-project --python '>=3.15' python -I tests/test_NAME.py` |
| Worker source, tests, or config | `make worker-deps` once, then `make worker-test`, `make lint`, and `make worker-check` |
| Migrations | `make migrate-local` |
| Packaging | `make dist`, then run the suites against an installed wheel: `AGENT_MEMORY_SRC=<site-packages> AGENT_MEMORY_BIN=<venv>/bin` |
| Mirror protocol | `tests/test_mirror.py` and `cd cloudflare && npm run test:protocol`; both read `tests/fixtures/mirror_protocol.json` |
| Benchmarks | `make perf-local REPEATS=1` and `make perf-report` |
