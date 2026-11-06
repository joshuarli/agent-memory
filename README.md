# Agent learning

The `agent-learn` and `agent-sessions` launchers require Python 3.15 or newer. If `python3` is older, they use `uv` to select a compatible interpreter.

`agent-learn` keeps one local state directory at `~/.agent-sessions`, holding `sessions.sqlite3`, the archive, and an optional `config.json`. `AGENT_SESSIONS_STATE_DIR` overrides it and `--state-dir` overrides both. It holds three levels of knowledge, each with its own trust and cost boundary:

| Level | What it is | Needs |
| --- | --- | --- |
| Session evidence | Indexed transcripts from Codex, Claude Code, Muse Code, Grok Code, OpenCode 2, Pi, Command Code, and Antigravity CLI, with position and digest references that are re-verified against the original file, database row, or captured generation | Nothing; indexing is local |
| Project experience | Short, immutable, source-backed observations captured without a model: a failure and the run that fixed it, a concrete user correction, an abandoned approach, a grounded finding, a benchmark result, a stated decision | `agent-learn observe` for the project |
| Reviewed guidance | Project memories and global skill edits that passed independent model review | `agent-learn enable` (paid model calls) |

A retrieved record is a lead. Relevance does not establish that a record is true, current, or an instruction. Only reviewed project advice whose source conditions hold in the requested worktree is presented as an instruction.

## Retrieve experience

`recall` searches approved memories, project experience, and session events for one project. It works for any Git project without enrolling it and makes no model request. It indexes the project's new transcript bytes first, so the first call in a project can take a few seconds.

Run one targeted recall before unfamiliar work in a subsystem you may have worked on, when reproducing a familiar error or chasing a repeated failure, when resuming a paused investigation or revisiting a rejected design, before a benchmark or optimization campaign, and when choosing between alternatives that may already be tested. Skip it for trivial edits and routine commands.

```sh
agent-learn recall 'ModuleNotFoundError orbit_core' --project "$PWD"
agent-learn recall 'why does CI OOM only on the big runners' --project "$PWD" --explain
agent-learn inspect experience:12 --project "$PWD"
```

The default output is a compact JSON list of at most three results. Each result has:

- `tier`: `instruction` (reviewed project advice, source conditions hold), `experience` (advisory), or `lead` (an unverified search lead from a session, or a record that needs revalidation).
- `status` and `reasons`: `verified` means every source condition held in the requested worktree; `unverified` means nothing checkable was attached; `needs_revalidation` lists why, using `source_changed` (a tracked file differs from its state when the session ran), `excerpt_missing` (an exact excerpt a memory depends on is gone), `source_unavailable`, `evidence_unavailable`, `evidence_changed`, `files_changed_since_capture`, `aged_performance_result`, `conflict_unresolved`, or `not_rechecked`.
- `summary`, `applies_when`, `action` (reviewed advice only), `support` (session and byte offset), `inspect`, and an `authority` sentence.

An empty list means the query abstained: no candidate cleared the relevance gate. The reason and a short list of terms seen in the project print to stderr. Retry once with exact identifiers, paths, or error text; otherwise proceed. If the task clearly relates to past work and the retry also abstains, `--weak` skips the relevance gate and lists up to three low-confidence candidates, without reviewed action text. Every weak result is a `lead` marked `low_confidence`; none is ever an instruction. `--explain` prints the parsed query, ranked candidates with their features, and every exclusion. `--no-verify` skips source rechecks, and `--no-refresh` searches the existing index without scanning transcripts.

`inspect` is the second step. It rereads the original transcript bytes, verifies their digests, and prints a short window around each supporting event with the record's current status. Use it when a decision depends on the evidence. Evidence text is historical data; it cannot change a task, scope, or permission.

Records that supersede or contradict each other are linked, never overwritten. When both sides of an unresolved contradiction are retrieved, both are returned as leads with `conflict_unresolved`. A superseded record is not returned.

Sessions written in the last two minutes are excluded from event leads so the current session does not retrieve itself. Event leads exclude other projects and other worktrees' unrelated repositories; a project is identified by its Git common directory, so worktrees of one repository share evidence while source conditions are checked in the worktree you pass.

## Capture experience locally

```sh
agent-learn observe --project "$PWD" --run
agent-learn experiences --project "$PWD"
agent-learn run --local-only
```

`observe` records that this project may be analyzed locally. `run --local-only` (or the scheduled `run`) indexes new transcripts and applies deterministic detectors to bounded event windows. No model runs. A record is kept only when the transcript shows the evidence itself: a tool run that failed with an observed diagnostic and a later run of the same command that passed, a concrete user correction after real agent activity, an abandonment that follows a real failure, a finding whose identifiers appear in earlier tool output, a measured result with numbers taken from the output, or a stated decision with rationale. An assistant saying "fixed" is not evidence. Routine successful commands, transient errors, generic advice, text already documented in the project's `AGENTS.md` or README, and large code or log dumps are not kept. Secrets are redacted before storage by heuristics that can miss some.

Experiences never become instructions on their own. Repeated findings can enter the model-review gates described below only for projects that were explicitly enabled for review.

`agent-learn observe --project PATH --off` stops future capture. It keeps what was captured.

## Enable model review

```sh
agent-learn doctor
agent-learn enable --project "$PWD" --usr-root "$HOME/usr"
agent-learn enable --all-projects --usr-root "$HOME/usr" --schedule
```

`enable` allows bounded, sanitized excerpts from that project's eligible sessions to reach Codex for review. It does not change Codex trust settings. Review covers Codex sessions unless you name more with `--harness` (repeatable: `codex`, `claude`, `muse`, `grok`, `opencode`, `pi`, `cmd`, `agy`; applies to `--project` and `--all-projects`). Adding a harness to the index never enrolls it here; a session reaches review only when its harness is named.. Local indexing and observation never imply this grant. The wildcard enrolls Git projects found in session history.

On macOS, `--schedule` installs a user LaunchAgent that runs `agent-learn run --scheduled` every 15 minutes. Without it, run reconciliation yourself; Linux has no bundled scheduler. The dotfiles sync does not install or start the LaunchAgent.

Each run starts at most two finding reviews. A review starts only when the previous 24 hours leave room for its full reservation: four model calls within a 24-call budget, two for a regression monitor. The reservation is written before the first call and replaced by recorded usage when the review finishes, so a crash cannot hide spend and the budget is a hard cap. A state directory left at the old `${XDG_STATE_HOME:-~/.local/state}/agent-sessions` moves to `~/.agent-sessions` once, on the first command that opens it; if `~/.agent-sessions` already exists as a container (a central store, a backup, a config) the legacy index and archive move into it, while two session indexes are never merged silently. An explicit `--state-dir` and a test directory never move. `agent-learn status` reports `model_calls_24h`, `model_tokens_24h`, deferred reviews, and rejected candidates.

Run `agent-learn status` for grants, findings, observation, retrieval counts, transcript coverage, and scheduler state. `agent-learn findings` lists findings and `agent-learn show ID` inspects one.

## Withdraw, delete, and disable

- `agent-learn withdraw experience:N` stops retrieval and keeps the evidence. `withdraw memory:N` withdraws an approved project memory.
- `agent-learn forget experience:N` deletes the record, its sources, links, receipts, feedback, and any still-pending review candidate built from it, and remembers its fingerprint so the same observation is not captured again. It withdraws memories derived from it (a withdrawn memory keeps only its finding row, stripped of the reviewed text, proposal, and evaluations) and reports a promoted skill edit it cannot safely reverse as `unresolved_derived_findings` with a non-zero exit. It never forces a revert.
- `agent-learn forget-session SESSION` deletes one session's index rows, every experience that cites it, and everything derived only from it, and records a tombstone so a later refresh does not index or archive the same transcript again. Experiences supported by other sessions are rebuilt from those sessions on the next run. It deletes the session's archived copy (see [Retention](#retention)) and never touches the harness's own transcript file.
- `agent-learn undo ID` withdraws a promoted skill edit or approved memory. A promotion can be reversed after later commits when its paragraph is unchanged; otherwise the reversal is refused and `status` lists the finding under `unresolved_regressions`.
- `agent-learn disable --project "$PWD"` stops review for one project; `--all-projects` revokes the wildcard; `--schedule` removes the LaunchAgent for every project. Disabling stops future work and deletes nothing.
- `agent-learn reindex` rebuilds the search projection from the canonical records.

## Feedback and measurement

Every recall writes a receipt of what was returned, and `inspect` writes one for what was inspected. `agent-learn feedback REF helped|irrelevant|stale|misleading` records a verdict; it is self-declared and nothing reads it for ranking, tier, or authority. Feedback and frequency never raise a record's tier or ranking. `agent-learn quality` reports abstentions, stale results returned, leads never inspected, queries that repeatedly abstained, consolidation candidates, feedback counts, and follow-through. Follow-through is derived from transcripts: a result was presented when its reference appears in the recall output, acted on when a later tool call used an identifier the query did not already contain, and verified when a later test or build run passed or failed. It is observational. It cannot show that a record caused an outcome, that an agent obeyed it, or that a missing memory existed.

## Hook and data boundaries

The user-level Codex `Stop` hook runs `agent-learn hook`. It reads the event name and working directory and records a project-scoped hint for enabled projects. It does not read transcripts or run a model. `SubagentStop` events and sessions marked with `AGENT_LEARN_WORKER` create no hints. No harness injects memory on every turn; agents call `recall` through the [`do`](../dotfiles/base/.agents/skills/do/SKILL.md) skill's rule, and the result arrives late in the context as an ordinary tool result, which leaves the system prompt and its cache untouched. See [`config.toml`](../dotfiles/base/.codex/config.toml) for the hook, which Codex may ask you to trust.

The store keeps local transcript indexes and learning records; it never copies original transcripts into Git. State directories use mode `0700`; database files use `0600`. Redaction is heuristic and can miss credentials or confidential text. Enabling a project or the wildcard allows bounded excerpts from eligible sessions to reach Codex. Review workers use isolated scratch directories, disabled tools and hooks, ignored user configuration, read-only sandboxing, and ephemeral sessions. These settings do not establish VM isolation or prevent every host read. `agent-learn doctor --probe-cli` runs an unpaid loopback diagnostic with synthetic input.

## Search sessions

`agent-sessions` indexes Codex, Claude Code, Muse Code, Grok Code, OpenCode 2, Pi, Command Code, and Antigravity CLI transcripts. Search defaults to the current Git repository and its worktrees; cross-project search requires `--all-projects`.

```sh
agent-sessions recent
agent-sessions search 'cargo test' --harness claude
agent-sessions search 'permission denied' --kind tool-output
agent-sessions show claude:SESSION --position BYTE_OFFSET
agent-sessions doctor
```

To find your own prompts, filter to `--kind user` and widen the scope with `--all-projects`. Hyphenated identifiers and slashes are autoquoted, so `codegen-units` and `thin lto / codegen-units` work unquoted; quoting still forces an exact phrase and `AND`/`OR` still combine terms. Limit to today with `--since 1d` (or an ISO date), and use `--regex` only for pattern matching.

```sh
agent-sessions search 'codegen-units' --all-projects --kind user
agent-sessions search 'thin lto / codegen-units' --all-projects --kind user --since 1d
agent-sessions search '"codegen-units"' --all-projects --kind user --limit 5
```

`agent-learn recall` is for reviewed memories and fuzzy session leads (technical tokens handled, ranked, gated); `agent-sessions search` is the direct full-text index for exact message lookup.

A Codex session keeps its bare ID. Other harnesses use `harness:ID`, so identical IDs from different tools never collide. Use the full session ID and byte offset printed with a hit. `show` verifies source bytes before displaying context. `doctor` prints per-harness coverage: complete, degraded, and unsupported sessions, unrecognized event kinds, and sessions whose ancestry is unknown. A session whose lines parse but contain no understood content is `unsupported`, not an empty success. Muse and Grok do not always state their ancestry; unknown ancestry is never counted as an independent root. `doctor --rebuild-index` rebuilds transcript tables while preserving learning records.

Default roots are `$CODEX_HOME/sessions` and `archived_sessions`, `$CLAUDE_CONFIG_DIR/projects`, `$XDG_DATA_HOME/muse/sessions`, `$GROK_HOME/sessions`, `$XDG_DATA_HOME/opencode`, Pi's session directory (`$PI_CODING_AGENT_SESSION_DIR`, else `$PI_CODING_AGENT_DIR/sessions`, else `~/.pi/agent/sessions`), Command Code's `$COMMANDCODE_HOME/projects`, and Antigravity's `$ANTIGRAVITY_HOME/brain`, each falling back to its usual home directory. Pass `--root [HARNESS=]PATH` to override. Indexing never follows symlinks. Pi sessions are the `<timestamp>_<id>.jsonl` files, so pi-subagents artifacts such as `events.jsonl` are not mistaken for sessions. Pi stores one tool call or result per entry; several calls from one assistant entry become one event whose call ID lists them all. A session whose header names a `parentSession` links to that session and is labeled `child`: Pi writes the same field for a user fork and for a delegated subagent. Every entry of a branched session is indexed and searchable, but experience detectors and review read only the active branch: the chain from the newest entry back through `parentId`, so a failure on an abandoned branch is never paired with a success on another. OpenCode 2 keeps sessions in a SQLite database (`opencode.db`), not transcript files: the indexer opens it read-only, reads only the `session_v2` and `session_message` tables (never the account or credential tables), reindexes a session whenever its message count or update time changes, and takes shell exit codes from the recorded tool metadata. A hit's byte offset is `message sequence × 1000 + block`, and `show` verifies the row's digest. Search matches whole tokens, so a Chinese or Japanese query matches only a whole unsegmented run.

## Benchmarks

`make perf-local` measures the collector paths with no network and writes one JSON document
per run under `.work/perf/`; `make perf-report` compares the oldest and newest run of each
scenario. `REPEATS` (default 5), `SCENARIO`, `RECORDS`, `OBJECTS`, and `OUT` override the
defaults. `SCENARIO=learning-import` seeds 1k synthetic learning records by default and
measures snapshot export and serialization. `SCENARIO=mirror-initial OBJECTS=200` prepares
64 KiB synthetic transcript and artifact objects through compression and `rage`, and records
plaintext, compressed, and ciphertext bytes plus CPU per class; set `AGE_BIN` when `rage` is
not on `PATH`. Ciphertext bytes live in the sample's `timings` because randomized age
ciphertext length can vary by a few bytes, so they are measured, not used as a deterministic
gate. `perf-worker` boots the built bundle with real Miniflare D1 and R2 and measures it over
loopback HTTP; build it once with `npm run build` in
`agent-memory/cloudflare` after `npm ci`. `perf-deployed` probes the production origin with
the service token and records an observational run: it checks that an unauthenticated
request is refused, reuses one keep-alive TLS connection after a warmup, and records wall
time, client process CPU, response bytes, result counts, reconnects, and the Worker's
`Server-Timing` D1/R2 durations. Production documents carry `environment: "production"`,
`measurement: "observational"`, and `perf_gate: "observational"` because the host-load gate
does not control network variance. A local run needs at least five timing samples per
operation before it reports `perf_gate: "passed"`.

`SCENARIO=learning-import RECORDS=n` on 2026-11-01 exported 1,000 records as 670,399
bytes at 70,074 records/s (median 16.763 ms, p95 18.800 ms) and 10,000 records as
6,753,139 bytes at 103,951 records/s (median 136.472 ms, p95 181.534 ms). The
100,000-record run exported 68,023,339 bytes with stable deterministic counters; its
timing gate was deferred because the host was busy. The 1k/10k/100k FTS search baselines
are in the campaign record: a 20-result page issues two D1 queries and stays near 31–36 ms
median under Miniflare. `make perf-report` compares the oldest and newest run of each
scenario.

The capture baseline (200 sessions × 10 messages × 10 projects, 2,600 events) on
2026-10-31:

| Operation | bytes read | content opens | stat ops | git processes | SQLite changes |
| --- | --- | --- | --- | --- | --- |
| fresh index | 979,580 | 400 | 600 | 10 | 24,181 |
| one-line append | 3,015 | 3 | 203 | 1 | 11 |
| unchanged refresh | 0 | 0 | 200 | 0 | 0 |

The mirror baseline (`SCENARIO=mirror-initial OBJECTS=10 REPEATS=2`, 20 objects of 64 KiB,
`rage 0.12.1`) on 2026-11-01:

| Class | Plaintext | Compressed | Ratio | zlib objects | Compress CPU | Encrypt CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| transcript chunk (synthetic) | 655,360 | 22,390 | 3.4% | 10 | 1.4 ms | 11.5 ms |
| random artifact | 655,360 | 655,360 | 100% | 0 | 7.0 ms | 13.9 ms |

The artifact is incompressible by construction; the transcript payload is repetitive
synthetic text, so it bounds the best case rather than a real transcript. The 20 objects
produced about 683,700 ciphertext bytes. Wall p50 was 269.8 ms (p95 276.8 ms) and CPU
median 39.3 ms, including one `rage` process per object.

Time-based samples are recorded only when `perf-gate-ready` passes before and after the
sample; a busy host leaves `perf_gate: deferred`, and the deterministic counters still gate
the change. Mirror upload scenarios record plaintext, compressed, and ciphertext bytes so
compression savings are measured rather than assumed.

## Retention

Every refresh captures each transcript into `~/.agent-sessions/archive`, a `0700` tree of immutable objects named by their SHA-256 digest plus one versioned generation per capture. A generation records the chunk order, offsets, source size, the source signature that produced it, its companion artifacts, and the native session metadata (parent, source, branch tip, workspaces, step count) the harness reported at capture. A generation is immutable: a change to any of those creates a new generation and leaves the earlier one untouched. Search, `show`, `agent-learn recall`, and evidence verification keep working after a harness deletes or expires the file (Claude Code removes old transcripts after `cleanupPeriodDays`), and after the whole source machine is gone once the generation is acknowledged centrally.

- Capture happens during a refresh, which any `agent-sessions` command, `agent-sessions sync`, or scheduled `agent-learn run` performs for every transcript root regardless of the project filter. A file a harness deletes before any refresh saw it is not recoverable.
- An unchanged source reads no content bytes and adds no generation. A changed source is read once end to end, and only chunks whose digest is new are written, so one appended line to a 4 MB log writes 160 bytes instead of re-copying it. The read is one sequential pass; the point of the chunk digests is that no prefix is ever trusted without hashing it. A rewrite, truncation, or replacement adds a new generation and keeps the earlier one, and a generation always equals the bytes that were read, never a splice of an older prefix.
- Capture is refused for a symlink, a non-regular file, or a source that changes while it is being read. Publication writes an object under a unique staging name, fsyncs it and its directory, renames it, then commits the generation in one transaction; an interruption leaves either the previous generation or the complete new one, never a half state.
- A changed companion or changed native metadata with an unchanged transcript adds a generation that reuses the transcript's chunks and reads no content bytes. The transfer protocol (version 2) carries the generation's manifest digest, and the receiver recomputes it, so a changed sidecar is always delivered and a central branch tip always equals the collector's.
- Command Code companions (`<id>.meta.json`, `<id>.checkpoints.jsonl`, `<id>.prompts.jsonl`, `<id>.share.json`) and Grok's `summary.json` and `events.jsonl` are captured as generation artifacts. Antigravity's `tasks/`, `steps/`, `messages/`, and `scratch/` and Command Code's `file-history/` stay outside the default boundary; `--artifacts` on the capture path opts in under an 8 MiB per-file cap.
- The state directory may be on a different filesystem from the transcripts; capture copies bytes and never links them. Muse Code refuses to resume a log with multiple hard links, and capture never creates one.
- OpenCode's database is not copied as a file. The archive keeps its own `opencode.db` with only the session and message rows, added or updated on refresh and never removed when OpenCode drops them. Each session is exported from that sanitized copy to a canonical JSONL generation keyed by native session id, so mirroring, central indexing, tombstones, restore, and evidence verification use the same generation path as the file harnesses. Credential tables are never copied.
- Learning-worker sessions, ephemeral sessions, and forgotten sessions are never archived. `reset-agents` does not touch the archive.
- The archive holds complete, unredacted transcripts. Keep it out of Git and include it in backup and disk-encryption decisions. `agent-learn forget-session` is the only way to remove an archived session.
- What survives: harness cleanup or deletion; a process crash at any publication point (tested); an equal-size rewrite with a restored modification time (a new generation is captured because the inode change time also moves). What is not claimed: power loss beyond what the filesystem and the object fsync establish, an interior rewrite of a growing log that keeps the final captured chunk identical, or recovery of a live process or an external tool side effect. Archiving a transcript is not resuming a process.

## Command Code and Antigravity CLI

Both are indexed like every other harness: discovery, metadata, events, archive, search, `show`, `doctor`, and experience capture. Command Code IDs are `cmd:ID`; Antigravity IDs are `agy:CONVERSATION`. The aliases `command`, `command-code`, `commandcode`, and `antigravity` are accepted anywhere a harness name is, and are normalized once.

- **Command Code** (`cmd 1.88.2`, transcript `version: 3`): `~/.commandcode/projects/<slug>/<id>.jsonl`. The first line is the session header with `cwd`; every later line is a tree entry with `parentId`, Anthropic-shaped content blocks (`text`, `thinking`, `tool_use`, `tool_result`), and per-entry model usage. `tool_use.id` is preserved. `tool_result` has no status field, so its outcome stays `unknown` unless the output itself carries an exit code. Sidecars are companions, never separate sessions. The active branch is the ancestor chain of `meta.json`'s `branchPoint` when a fork records one, otherwise the chain ending at the newest entry, so a failure on an abandoned branch is never paired with a success on the active one. Compaction entries are preserved; their field names are not yet verified, so they surface as `unknown:<type>` until a real record is captured.
- **Antigravity CLI** (`agy 1.4.10`): `~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl`, else `transcript.jsonl`. The `_full` variant is the untruncated one: `truncated_fields` appears only in the truncated variant. `logs/chunks/**` is a byte-exact split of the same log and is never indexed as a second session. Workspace URIs, `parent_conversation_id`, `nesting_depth`, and `step_count` come from `conversation_summaries.db`, opened read-only; a conversation with more than one workspace reports all of them instead of picking the first. `USER_INPUT` becomes `user`; `PLANNER_RESPONSE` becomes `assistant` plus `tool-call`; `GENERIC` becomes `tool-output` with `status` `ERROR` mapped to an error outcome; `CHECKPOINT` becomes `compacted`; `SYSTEM_MESSAGE` becomes `context`. Antigravity records no tool-call id, so the id is synthesized from `(step_index, ordinal)` and marked `synthetic`. A transcript shorter than its recorded `step_count`, or one carrying `truncated_fields`, is `degraded` with an `index_issues` row rather than a complete success.

Antigravity CLI is not Gemini CLI, the Antigravity app, or the IDE. Its `/resume` verifies a conversation against the backend and can import app conversations, so a resumable cloud conversation is not evidence that a complete local log exists. Both harnesses are excluded from model review unless named with `enable --harness`.

## Central archive

One machine can be the central store for several collectors. It is an ordinary agent-memory state directory; nothing runs as a daemon, and local operation continues while it is unreachable.

```sh
# On the central host: nothing to start. The receiver is a fixed command.
# On each collector, ~/.agent-sessions/config.json:
# {"central": {"transport": "ssh", "host": "archive",
#              "command": "agent-sessions-serve ingest --protocol 2 --origin 1f2e...",
#              "allowed_origins": ["1f2e..."]}}
agent-sessions identity show                     # this collector's origin id
agent-sessions sync --to central --dry-run       # pending generations
agent-sessions sync --to central                 # capture, deliver, ack
agent-sessions search 'codex-units' --central --kind user
agent-sessions show 'cmd:ID@ORIGIN' --central
```

The central host needs the receiver package (`agent-memory/src/agent_memory`) and a Python with stdlib `sqlite3`. It does not need the launchers, `uv`, or the learning extras: `python3 -m agent_memory.serve ingest --protocol 2 --origin ID STATE_DIR` is the whole server. Tested against Ubuntu 24.04 with Python 3.12.3; the launchers on a collector still require 3.15.

The encrypted Cloudflare mirror is a second central transport. Its config block is
`{"central": {"transport": "cloudflare", "url": "https://memories.17166969.xyz",
"service_token_file": "...", "age_recipients_file": "...", "age_identity_file": "..."}}`.
The service-token file must be mode 0600 and hold `client_id` and `client_secret`. `sync`
binds the collector origin through Access, fetches remote tombstones before capture, pushes
local tombstones before generations, compresses each object with zlib when that saves at
least 5%, encrypts it to the recipient set, uploads only missing plaintext, and marks a
generation durable only after the Worker acknowledges its manifest digest. The first sync
pins the Worker's `store_id`; a different store with existing generation or learning
acknowledgements is refused.
`agent-sessions mirror status --remote cloudflare` prints the pinned identity, and
`agent-sessions mirror adopt --remote cloudflare` clears the old pin's local
acknowledgements before rebinding. An unchanged sync performs one `hello` and no object,
generation, or tombstone writes.

A sync that changes generations, tombstones, or the learning journal uploads a recovery
snapshot: a sanitized SQLite copy in 1 MiB content-addressed chunks plus pins for every
archive object the copy references. Snapshots are created at most once a day; a learning
change waits for the next one, and the local learning journal keeps it until then. The pins
travel as sorted pages of up to 10,000 objects that the Worker stores in R2, and the commit
names their count, bytes, and SHA-256, so committing a snapshot writes D1 rows for its chunks
but none per pinned object. The copy omits the transcript search projection, which restore
rebuilds from the captured bytes, so it carries the archive and learning catalog without
duplicating the local search index. A failed snapshot is retried after 15 minutes, then after
twice the previous wait, up to a day, and a successful one resets the wait.
`agent-sessions mirror snapshots` lists them, and `agent-sessions mirror verify --objects`
recomputes every manifest and snapshot and attests a snapshot only after every pinned
archive object inside it has been downloaded; `--sample` below the pinned-object count
reports evidence without attesting. `agent-sessions mirror restore --snapshot ID --to DIR`
rebuilds a new store from one snapshot, applies current tombstones, and replays every live
generation the snapshot does not already contain, including work committed by another
collector; it activates no project or review grant. `agent-sessions mirror gc` reports unreferenced objects without
deleting; `--apply` prunes snapshots outside the 7-daily/4-weekly retention and sweeps
objects unreferenced for an hour, both in bounded batches so a large backlog can be drained
without one oversized request. A verified snapshot is kept only while it is inside that
four-week window, so an old attestation cannot pin storage forever. `agent-sessions mirror rotate --recipients FILE
--identity NEWKEY` re-encrypts the origin to a new recipient set and leaves the old identity
unable to read the new ciphertext.

### Configure and schedule

```sh
agent-sessions mirror keygen --identity ~/.agent-sessions/age.key \
  --recovery ~/.agent-sessions/age-recovery.key \
  --recipients ~/.agent-sessions/age.recipients
agent-sessions mirror configure --url https://memories.17166969.xyz \
  --token-file ~/.agent-sessions/cloudflare-token.json \
  --recipients ~/.agent-sessions/age.recipients \
  --identity ~/.agent-sessions/age.key
agent-sessions mirror enable --searchable-learning
agent-sessions mirror status --json
agent-sessions mirror doctor
agent-sessions mirror sync --dry-run --json
agent-sessions mirror sync
```

`keygen` writes the collector identity and the recovery identity with mode 0600 and a
recipients file naming both; move the recovery identity offline and keep the token out of
the repository. `configure` refuses a token file that is not regular mode 0600.
`enable --searchable-learning` prints the projection disclosure and opts in to storing
claim and applicability text in D1; it also requeues every live experience and approved
memory and prints `requeued=N`, so records created while the journal was pruned are
projected. Without the flag the mirror still syncs, but no learning record is projected or
indexed. `disable` turns scheduled sync off, and `disable --searchable-learning` also
withdraws the projection opt-in.

`mirror status --json` reports the pinned store and revision, pending generations, objects,
and bytes, the last error, the last sync time, and the last verified snapshot. Generation
counts are live counts: a tombstoned generation is not reported even if an older row
survives storage. `mirror sync --dry-run --json` reports the same backlog plus an estimated
request count and never opens a connection. `mirror sync --reconcile` requires the
searchable projection opt-in, applies remote tombstones first, and replaces the local live
record set in one staged epoch; it leaves the journal for the next sync to snapshot before
pruning. `mirror doctor` checks the config, token mode,
identities, recipients, state, and remote reachability; `--offline` skips the remote check.

Scheduling is explicit and never installed by sync. Render
`dotfiles/launchd/com.josh.agent-sessions-mirror.plist.in`, replacing `@@HOME@@`,
`@@STATE_DIR@@`, and `@@REMOTE@@`, lint it with `plutil -lint`, and load it with
`launchctl bootstrap gui/$(id -u) PLIST`. The portable equivalent is
`0 * * * * agent-sessions mirror sync`. An overlapping run exits 0 with
`overlapped=1`, a disabled mirror exits 0 with `enabled=0`, and an unreachable mirror exits
non-zero with the backlog intact for the next run. Routine sync uses HTTPS only and never
invokes `cf`. Schedule `agent-sessions mirror gc --apply` separately, for example weekly;
sync never applies retention.

For a local or offline run, point the same commands at a directory: `agent-sessions sync --central-state DIR`, and `search --central --central-state DIR`. The receiver and the SSH path share one implementation, so the framing, digest checks, and acknowledgements are identical.

- Object frames are zlib-compressed when both ends offer it, which every build of this tool does. The manifest already declares each object's size, so the receiver inflates to at most that size, refuses anything larger, and verifies the digest afterwards; an object that does not shrink travels uncompressed. Measured on 40 of the largest local objects, 2.6 MB became 1.1 MB.
- Delivery is at-least-once. A generation is acknowledged as remotely durable only after the receiver has verified every object's digest and size, matched the manifest origin to the authenticated peer, checked that the chunk offsets tile the declared size, that the source digest and manifest digest follow from the content, and that the artifact and metadata fields are well formed, and committed the manifest. A manifest that fails any check is refused before a session record exists. A retry of an accepted generation is a no-op that re-acks.
- Durability is recorded against the receiving store's own identity, which it reports when the connection opens, not against the local label. Pointing the same label at a second store therefore re-delivers what that store has not seen, instead of trusting an ack from a different machine.
- Stored is not the same as searchable. The central side indexes each accepted generation itself, from the captured bytes, so search and `show` never need the source host or its filesystem.
- The sender keeps its own copy until the ack. While offline it keeps a backlog with the attempt count and last error, and `doctor --json` reports it.
- Tombstones travel first. A session forgotten at a collector refuses later re-delivery, so a stale or restored machine cannot resurrect it A received tombstone is applied with the same dependency-aware semantics as a local `forget-session`: derived experiences and findings are withdrawn, the session's generations are deleted, and any object no other generation references is removed from disk. Only the tombstone itself is kept. A restore that reapplies current tombstones does the same.
- `host` is whatever `ssh` accepts, including a user: an alias backed by Tailscale SSH needs `ubuntu@host`, because Tailscale resolves the *local* user name otherwise and refuses. `ssh` runs with normal host-key verification. No content, query, path, or session id is ever interpolated into the remote command; the command is fixed in configuration, and `agent-sessions-serve` refuses an origin it was not started with. Sync and every `--central` query share one SSH transport that drains stderr and enforces an inactivity deadline (120 s without a byte moving in either direction): the watchdog kills the `ssh` process, the pending read fails with a timeout error, and the transport reaps the process before returning, so a connected but silent receiver cannot hang a collector or a query.
- Learning records travel as records, not databases. After the generations, a sync sends one snapshot of the collector's experiences and approved memories whose evidence is all local: original ids, kind, claim, applicability, evidence references (session, position, digest, role), supersession links, and lifecycle state including withdrawn, retired, and superseded. The central store keeps them in separate `learning_import_*` tables keyed by origin and original id, with the evidence sessions rewritten to their central ids so `show --central` verifies them. A snapshot replaces that origin's previous one atomically, so a record deleted at the collector disappears centrally. Imported records are advisory: they never enter the local learning tables, are never promoted, and `agent-sessions memory` labels them as not an instruction. A snapshot is refused for an unknown kind or state, a bad digest, or a repeated record, and a record citing a tombstoned session is dropped. Forgetting a session also deletes every imported record that cites it. The snapshot is re-sent only when its digest changes; `doctor --json` reports `learning_synced`. Restoring records onto a replacement collector is not implemented.
- A collector declares one origin identity, a random persisted id, never a hostname or a hardware id. `agent-sessions identity regenerate` mints a new one and leaves recorded origins alone. The same native id from two origins is two sessions; a copied history is not independent evidence.

Optional scheduling is explicit and never installed by sync: `dotfiles/launchd/com.josh.agent-sessions-sync.plist.in` for macOS, or a cron entry such as `*/15 * * * * agent-sessions sync --to central`. Overlapping runs exit immediately with `overlapped=1`. Missed runs are recovered by the next one; a hook hint is optional and never blocks a session on network access.

## Backup, restore, and recovery

```sh
agent-sessions backup create /backups/agent-sessions-$(date +%F)
agent-sessions backup restore /backups/agent-sessions-2026-10-30 --to /new/state --tombstones-from ~/.agent-sessions
agent-sessions archive verify
agent-sessions archive reindex
agent-sessions archive prune-legacy
agent-sessions doctor --json
```

`archive prune-legacy` reports the pre-object-store transcript trees by checking each file against a captured generation or artifact; `--apply` deletes only the files whose bytes are already captured and leaves the archive for a database harness untouched.

A backup is a SQLite backup-API snapshot plus a digest-verified copy of every immutable object pinned in `manifest.json`. It holds the shared object lock from before the snapshot until the last object is copied; removing an unreferenced object takes that lock exclusively, so a concurrent forget deletes catalog rows immediately but cannot unlink an object the backup still needs. The backup is built in a sibling staging directory, the manifest is written last, and one rename publishes it, so a destination holds a complete backup or does not exist. A corrupt or missing live object fails the backup instead of being copied. Restore defaults to a **new** destination, verifies every object and `PRAGMA integrity_check`, refuses a newer schema, restores canonical learning and lifecycle records, reapplies current tombstones before anything becomes searchable, rebuilds derived indexes from the captured bytes alone, and activates no service and no review grant.

The built-in backup is used.

Recovery procedures, in the order they are usually needed:

1. **Source machine lost.** On another machine, `search --central` and `show --central` still verify the acknowledged generations. Nothing needs restoring.
2. **Central store lost.** Restore the newest backup into a new directory, then point `central.state_dir` at it or re-ingest from any surviving collector.
3. **Interrupted transfer.** Re-run `sync`; accepted generations are not re-sent and the backlog reports what remains.
4. **Damaged object.** `archive verify` names the digest. Re-restore from backup, or re-sync that generation from a collector that still holds it.
5. **Forgotten session reappears after a restore.** Restore with `--tombstones-from` pointing at the current state, or re-sync from a collector that carries the tombstone. A tombstoned generation is never reindexed.

## Promotion policy

Recurring findings can propose a contextual sentence in an existing allowlisted skill or a trigger before its `Skip` clause. Two independent originating sessions in two enabled repositories must support a global skill edit. Findings in other repositories with similar wording and the same direction nominate evidence for review; the reviewer decides whether it supports the same action, and distinct projects and root sessions establish independence. Commands and conventions belong in project memory. The applier uses an ignored `.work/wt/` worktree and the existing skill checker, and integrates only into a clean, unchanged branch. It does not push. See [`learning_promote.py`](src/agent_memory/learning_promote.py) for validation, staging, and recovery rules.

A malformed edit, or an independent approval that lists risks, goes back to the proposer with the reason and is retried up to three times against unchanged evidence. An independent reviewer that declines holds the finding. A proposal that contradicts approved advice for the same owner and applicability goes back to the reviewer with that advice attached; otherwise the new finding is held with reason `conflict` and the approved advice stays.

Project memories are stored exactly as reviewed, at most 600 characters of action and 400 of applicability, with optional source-file conditions. Global instructions, routers, security and permission policy, dependencies, unrelated projects, and broad new skills cannot be auto-promoted. `recurring_mistakes` counts later sessions whose correction clusters with the original finding; `regressions_reported` counts complaints that quote the promoted advice. Neither counts an unrelated correction, and neither establishes that an edit caused improvement.

## Evaluation

All evaluation is offline unless a command says otherwise.

```sh
uv run --no-project --python '>=3.15' python -I agent-memory/eval/bench_recall.py validate
TMPDIR="$PWD/.work/tmp" uv run --no-project --python '>=3.15' python -I agent-memory/eval/bench_recall.py run --split dev
TMPDIR="$PWD/.work/tmp" uv run --no-project --python '>=3.15' python -I agent-memory/eval/bench_recall.py run --split heldout --haystack 150 --detail
```

The benchmark renders synthetic multi-project episodes into real Git repositories, worktrees, and transcripts in all four harness formats, indexes them, captures experience, installs approved memories, applies source edits, and scores recall against labeled episodes. `--system` selects the legacy word-overlap recall, plain FTS5 search, or the new pipeline. `--haystack N` adds N synthetic routine sessions per project. The `dev` split is for tuning. The `heldout` split was written independently and was not used to choose any parameter. `explain CASE` shows why one case passed or failed.

`agent-memory/eval/live_recall.py --live` compares a no-memory baseline, a retrieved-experience treatment, and an unrelated-task control with native Codex runs and scores the commands the agent ran, not its prose. It makes paid model calls and refuses to run without `--live`. Its offline tests are `agent-memory/eval/test_live_recall.py`.

### Measured results

Both splits are synthetic: 12 projects each, about 90 episodes across four harnesses, 14 approved memories, source edits, worktrees, and hard negatives. `dev` has 175 cases (148 answerable, 27 that should abstain) and was used to choose parameters. `heldout` has 141 (117 and 24), was written independently, and was run with the pipeline frozen. Precision@3 counts the results actually returned. Context is the compact JSON of the default three results.

| System | Split | Recall@1 | Recall@5 | Precision@3 | MRR | Multi-record coverage | Correct abstention | Context chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Previous `recall` (word overlap over memories) | dev | .25 | .25 | .23 | .25 | .45 | .70 | 88 |
| Plain FTS5, query as typed | dev | .08 | .10 | .08 | .09 | .00 | 1.00 | 11 |
| Plain FTS5, terms joined with OR | dev | .70 | .93 | .52 | .80 | .69 | .00 | 200 |
| New `recall`, default | dev | .57 | .65 | .57 | .61 | .38 | .89 | 472 |
| New `recall`, after one agent-written retry | dev | .74 | .82 | .74 | .77 | .50 | .85 | n/a |
| New `recall --weak` | dev | .78 | .86 | .74 | .81 | .74 | .30 | 724 |
| Previous `recall` | heldout | .24 | .24 | .24 | .24 | .17 | .96 | 53 |
| Plain FTS5, query as typed | heldout | .12 | .12 | .12 | .12 | .00 | 1.00 | 17 |
| Plain FTS5, terms joined with OR | heldout | .78 | .95 | .48 | .86 | 1.00 | .00 | 230 |
| New `recall`, default | heldout | .64 | .68 | .63 | .66 | .83 | 1.00 | 452 |
| New `recall`, after one agent-written retry | heldout | .77 | .80 | .75 | .79 | .83 | 1.00 | n/a |
| New `recall --weak` | heldout | .85 | .92 | .79 | .88 | .75 | .46 | 700 |

Cross-project leakage and stale-memory-as-verified violations are zero for the new pipeline on both splits, with and without a 150-session-per-project synthetic haystack (about 60,000 extra events; default recall Recall@5 .66, abstention .89 on dev and Recall@5 .66, abstention 1.00 on heldout). The previous `recall` also showed zero stale violations because it already filtered failed source conditions. Context is about 450 to 500 characters, roughly 120 tokens, per answered recall; an abstention prints `[]` and a short hint.

What the table shows:

- The new pipeline beats the previous `recall` by about 2.5 times on Recall@5 and searches session evidence the old one could not reach. It does not beat plain OR-joined FTS5 on Recall@5. That query returns five loosely related sessions for every question, abstains on none, and has lower precision. The new default trades recall for abstention: the same ranking reaches Recall@5 .91 on dev when the relevance gate is disabled, but the signals that separate a real match from a near miss (term coverage, rarity, score) have AUC about .88, so no threshold gives both high recall and high abstention.
- The targets of Recall@5 at least .90, Precision@3 at least .80, and abstention at least .95 are not met together. Default recall meets abstention on heldout (1.00) but not on dev (.89); `--weak` reaches Recall@5 .92 on heldout and Precision@3 .79 by giving up abstention.
- Paraphrases with almost no shared words are the main loss (dev Recall@5 .16 for that category). One agent-written retry recovers most of them (+17 points dev, +13 heldout) at a small abstention cost on dev (.89 to .85) and none on heldout. The retries were written by a separate agent shown only the query, the abstention hint, the project's seen terms, and the file list, never the fixtures. That models the retry the `do` rule asks for; it is not a measurement of any particular model.

Rejected as not paying for themselves on `dev`: a static software-synonym lexicon (+2 points Recall@5 at equal abstention), session-level evidence pooling (+1 point), and tuning the rarity threshold (no effect on small corpora). Embeddings and model reranking were not tried; the retry result is the cheaper mechanism and no model call is needed for retrieval.

A real first backfill of 1,284 sessions (3.35 GB of transcript) transferred 1.25 GB on the wire, a 2.67x reduction, and took 13 minutes against a Linux host over Tailscale. An unchanged re-sync then delivered only the sessions captured since. Archive capture on the same 200-session corpus: a fresh index reads 978,980 bytes and writes 200 non-empty transcripts as generations; an unchanged refresh reads 0 content bytes and changes nothing; a one-line append reads the changed file once end to end and writes only the new chunk. On a 4.19 MB log a one-line append wrote 160 bytes, against 4,194,765 bytes for the previous in-place copy, and kept both generations. These are work counts, not timings: `perf-gate-ready` was closed throughout because the host was busy, so no timing below is perf-gated.

Latency on the synthetic corpora is about 20 ms per answered query in process (26 ms with the haystack). On a private index of 841,000 events (471 real sessions, 1.0 GB, built in about 60 seconds, never committed), warm queries ran at about 40 to 50 ms with a maximum near 120 ms. These timings were taken under ordinary desktop load and are not perf-gate certified. Unchanged incremental indexing does the same I/O as the previous release for Codex transcripts (the benchmark's gated CPU median for 1,000 unchanged sessions is 30 ms). `agent-memory/eval/bench_sessions.py --baseline-ref REV` reproduces the comparison.

### Not established

- The live memory-use evaluation ran once (`eval/live_recall.py --live --runs 5`, one model, one task family, five runs per arm). Treatment did not beat baseline: 1 of 5 treatment runs completed correctly against 0 of 5 baseline runs, and every run in both arms repeated the mistake. No run in any arm invoked `recall`. The control arm stayed clean. Whether retrieval changes outcomes in real work remains unmeasured. In real sessions, `agent-learn recall` appears in tool calls from Codex, Claude, and Grok in projects other than `usr`, but not from Muse. The offline tests cover the fixture, the real capture pipeline, and the scoring.
- The corpora are synthetic. Quality on real sessions is unknown beyond latency and a spot check that exact identifiers return hits and generic requests abstain.
- Muse Code transcripts are parsed for user messages, assistant text, tool calls, tool results with exit codes, and skill reads; its lifecycle and streaming records are recognized and skipped. Tool-based experience capture from Muse has not been exercised on real sessions. Chinese and Japanese queries match only whole unsegmented runs.
- Model review is intermittently held as `unsafe` with `evidence_ref_not_in_context`: 16 of 62 reviews so far, when the reviewer cites a reference outside the evidence it was given. Two re-runs of previously held findings on a database copy cited only supplied references, so the cause is not established. The failure is classified as unsafe on purpose, and a new independent session reopens the finding.
- Follow-through and receipts are observational; nothing here shows that a record caused an outcome.
