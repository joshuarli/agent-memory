# Install

`agent-memory` is a Python 3.15 package with no runtime dependencies. Install it once into a virtual environment, then run its console scripts directly. Nothing resolves or downloads packages when a command runs, so hooks and scheduled jobs start fast and work offline.

## Create the environment

Create the environment at the path it will be used from. A virtual environment records absolute paths, so do not copy one to another host or directory.

```sh
cd ~/d/agent-memory
uv venv --python '>=3.15' .venv
uv pip install --python .venv/bin/python .
```

This is a regular (non-editable) install: later source edits take effect only after you reinstall. For development, `uv pip install --python .venv/bin/python -e .` installs it in editable mode instead.

To install a built wheel instead of the checkout, run `make dist` and install `.work/dist/agent_memory-*.whl`.

## Commands

| Command | Entry point |
| --- | --- |
| `agent-learn` | `agent_memory.learning:main` |
| `agent-sessions` | `agent_memory.sessions_cli:main` |
| `agent-sessions-serve` | `agent_memory.serve:main` |

The `usr` dotfiles ship launchers with these names. Each launcher checks the environment and runs `$AGENT_MEMORY_VENV/bin/<command>`, with `AGENT_MEMORY_VENV` defaulting to `~/d/agent-memory/.venv`. A missing environment is reported on stderr and the launcher exits nonzero. It never falls back to another copy.

## State

Installing or upgrading the package does not touch state. Every command uses `~/.agent-sessions` (or `AGENT_SESSIONS_STATE_DIR`, or `--state-dir`) for its index, archive, keys, grants, and locks, wherever the package is installed.

`agent-learn enable` records the `usr` checkout that may receive promoted skill edits. It defaults to `~/usr` and accepts `--usr-root`. Later runs use the recorded checkout, which must still be the tracked `usr` repository.

## Central receiver

A central host that only receives sync can run on an older Python: copy `src/agent_memory` from a checkout or the sdist and run `python3 -m agent_memory.serve ingest ...`, as described in the [README](../README.md).
