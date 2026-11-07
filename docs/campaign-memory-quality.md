# Campaign: memory quality, retention, and measurement

## Status

- **Goal:** keep the evidence model; make ordinary recall selective and measurably useful; bound local storage growth; measure all of it before anything is deleted.
- **Landed:** MQ-1 `e82ee7b`, MQ-2 `d1d7d8c`, MQ-3 `b1768b5`, MQ-4 `9824582` (heldout-2 only), MQ-5 `5531380`, MQ-7 `4ce5f14`, MQ-12 `112275c`.
- **In flight:** MQ-8, MQ-13.
- **Ruling from the MQ-5 run (`gpt-6-luna`, `medium`, 5 per arm):** baseline 1/5, treatment 0/5 (never called `recall`), hook 3/5, oracle 5/5, controls clean. The hook always retrieved the right experience; the hook-oracle gap is how the line states the fix. MQ-14 targets it.
- **Next slice:** MQ-8, then MQ-13.
- **Ruling from real data (2026-11-07, `archive status --json` on the owner's index):** database 2.9 GB (`events` 1.88 GB, FTS 630 MB, two identical 120 MB indexes on `events(session, position)`); archive 5.8 GB unique, of which older-generation-only bytes are 113 MB (2%). Compression (MQ-8) is the storage lever. Generation pruning in MQ-9 is worth little and drops to report-only.
- **Ruling from MQ-4:** heldout-2 returns the answer text for 6 of 71 cases by default and 21 of 71 after one retry, against 24 and 51 session hits. MQ-11 targets span hits, not session hits.

## Charter

- Decide and keep going. Record a decision here only when a later slice depends on it.
- Hard stops park the item: a production write (see `AGENTS.md`), a paid model run that is not already approved, deleting stored sessions, archive objects, or generations, or a public contract change listed below that the owner has not approved.
- One writer by default. MQ-1, MQ-2, and MQ-5 own disjoint files and may run in parallel, each in `.work/wt/<id>`.
- Every slice starts with a failing test for the behavior it changes. Bug fixes keep their reproducing test.
- Commits are Conventional Commits scoped to the workstream (`recall`, `learn`, `eval`, `archive`). The item ID goes in the body.

## Decisions

| Decision | Kind | Reason |
| --- | --- | --- |
| The Cloudflare mirror stays offline (`agent-sessions mirror disable`). No Worker, D1, or R2 changes, and no `make check`, `worker-*`, or `perf-*` runs. | owner | Avoid Cloudflare spend. |
| Local changes must still keep the mirror protocol valid, because the mirror can be re-enabled. Archive slices run `tests/test_mirror.py` and `tests/test_transfer.py`. | prescribed | Re-enabling must not need a migration of objects written while offline. |
| Live evals run on Codex with `gpt-6-luna` at `medium` effort only, pinned as `MODEL` and `EFFORT` in `eval/live_recall.py`, never read from the `usr` tier table. | owner | Bound spend; a tier-table edit must not change what is billed. |
| Results are reported for that model only. | prescribed | One model and one task family support no general claim. |
| Retrieval eligibility, storage retention, and semantic forgetting stay separate operations. `forget-session` is never used for compaction. | prescribed | A retained memory can need old evidence, and stale advice can be useful as history. |
| Presentation (hook or recall hit) never extends retention or raises authority. Only reconfirmation by new evidence reactivates an automatically retired record. | prescribed | Retrieval frequency is biased by the current paraphrase misses. |
| Real queries stay outside the repo. | delegated | The repo is public and the queries come from private sessions. |
| This campaign deletes nothing. Retention runs as a planner that reports what it would retire, compress, or prune. | prescribed | Enforcement needs the simulation evidence from MQ-9 first. |

## Baseline

- `heldout-2`, default retrieval: 24/71 answerable hits (33.8%), paraphrase 1/29, trajectory 0/9, path/symbol/command 5/5, abstention 21/21. With the supplied retry: 51/71.
- Loop: `tests/test_recall.py` about 33 s, `tests/test_learning.py` about 36 s.
- Confirmed defects at `c8845ee`:
  - `src/agent_memory/recall.py:600`: `any(path in changed ... for path in changed if named)` is always true when `named` is non-empty. An unrelated change marks any file-naming memory `files_changed_since_capture`.
  - `src/agent_memory/experience.py:519-525`: a fingerprint or near-duplicate merge adds sources to a `retired` record and leaves it retired.

## Sequence

| ID | Item | Owns | Prereq | Acceptance |
| --- | --- | --- | --- | --- |
| MQ-1 | Freshness fallback flags only memories whose named files are in the changed set. | `src/agent_memory/recall.py`, `tests/test_recall.py` | none | `tests/test_recall.py`: a memory naming `src/cache.py` stays `verified` when only `unrelated.txt` changed, and becomes `needs_revalidation` when `src/cache.py` changed. |
| MQ-2 | A merge that adds a new supporting session to a `retired` record moves it back to `retained` with a state reason. `withdrawn` and `superseded` records stay put. | `src/agent_memory/experience.py`, `tests/test_learning.py` | none | `tests/test_learning.py`: each of the three states gets a case. |
| MQ-3 | Recall quality gate: `bench_recall.py gate` compares per-category default and retry hit counts, abstention, forbidden project leakage, stale-as-current results, and serialized result bytes against a committed baseline. It exits nonzero on regression. | `eval/bench_recall.py`, `eval/recall-baseline.json`, `eval/test_bench_recall.py` (picked up by `make test-python`) | none | The gate passes at HEAD and fails on a fixture with one regression in each measure. |
| MQ-4 | Span labels: fixtures name the fact or evidence span that answers each query. The judge requires the span to appear in the returned text, not only the right session. | `eval/recall-fixtures/`, `eval/bench_recall.py` | MQ-3 | The gate reports both measures. The baseline is re-recorded and any drop is stated in the commit body. |
| MQ-5 | Oracle arm for `eval/live_recall.py`: injects the correct memory, to separate retrieval failure from failure to use evidence. | `eval/live_recall.py`, `eval/test_live_recall.py` | none | `eval/test_live_recall.py`: the oracle prompt carries the lesson, and the arm appears in `summarize` and `verdict`. |
| MQ-6 | Real-query corpus: 50 to 100 reviewed queries from real sessions, each frozen to its corpus cutoff and repository revision, covering exact identifiers, paraphrases, decisions, multi-session problems, obsolete workarounds, and no-answer cases. | harness in `eval/`; queries under `~/.local/state/agent-memory/eval/real-queries/`, never committed | MQ-4 | `bench_recall.py gate --corpus <path>` reports per-category results on the owner's machine. |
| MQ-7 | Storage breakdown in `agent-sessions archive status --json` and `doctor --json`: database and WAL bytes, bytes per table and index (through `dbstat` when the SQLite build has it, otherwise reported as unavailable), unique archive bytes, current versus older generations, bytes pinned by retained experiences, and unreferenced bytes. | `src/agent_memory/archive.py`, `src/agent_memory/store.py` (read-only queries), `tests/test_archive.py` | none | `tests/test_archive.py`: the synthetic 30-append workload reports the older-generation and pinned bytes that a hand count gives. |
| MQ-8 | Compressed local archive objects: an explicit per-object encoding, digests over the original bytes, and per-chunk compression that keeps bounded reads. Old uncompressed objects stay readable. Mirror upload sends the same plaintext as before. | `src/agent_memory/archive.py`, `tests/test_archive.py`, `tests/test_mirror.py` | MQ-7 | Mixed-encoding round trip; `tests/test_mirror.py` and `tests/test_transfer.py` pass unchanged; compression ratio, inspect latency, and peak memory are measured on real transcripts and stated in the commit body. |
| MQ-9 | Retention planner, simulation only: `agent-sessions archive plan --json` lists generations and objects it would prune (keep the latest generation per session, generations retained evidence needs, and a bounded sample of the rest), plus experiences it would retire. A simulated-clock suite runs a year of lifecycle events. | new planner module, `tests/test_retention.py` | MQ-2, MQ-7 | The simulated-clock suite covers each scenario below and checks that the inspected evidence of every retained memory survives the plan. |
| MQ-10 | Transcript fallback eligibility: events that only support `withdrawn` or `superseded` experiences are excluded from the prompt hook. Explicit `recall` still returns them, labeled historical. | `src/agent_memory/recall.py`, `src/agent_memory/recall_hook.py`, `tests/test_recall.py` | MQ-1 | A withdrawn lesson's source event does not reach the hook and does come back as historical from `recall`. |
| MQ-11 | Retrieval without models: improve default span hits, and session hits on paraphrase and trajectory queries. Candidate methods are project-vocabulary expansion from experience identifiers and claim/applies-when weighting. | `src/agent_memory/recall.py` | MQ-3, MQ-4 | The gate shows a paraphrase gain on `heldout-2`, no abstention or leakage regression, and no loss on MQ-6. The baseline is raised in the same commit. |
| MQ-12 | Hook lines may use the unused part of the 600-character budget and are cut at a word boundary. | `src/agent_memory/recall_hook.py`, `tests/test_recall.py` | none | Landed. The live-eval hook line carries the full fixed command. |
| MQ-13 | Drop `events_session_position`, which duplicates the UNIQUE autoindex on `events(session, position)` (120 MB on the owner's index). | `src/agent_memory/store.py`, `tests/test_sessions.py` | none | `EXPLAIN QUERY PLAN` for the session/position lookups uses the autoindex; `tests/test_sessions.py` passes; a migrated copy of an existing database loses only that index. |
| MQ-14 | Experience summaries lead with the resolution (the command, setting, or change that worked) before the failure narrative, so a hook line states the fix first. | `src/agent_memory/experience.py`, `tests/test_learning.py` | MQ-12 | Gate shows no hit, abstention, or span regression; the live-eval hook line begins with the fix. Re-run `--runs 5` only with owner approval. |

### Simulated-clock scenarios (MQ-9)

Temporary task state ages out of ordinary recall. A rare durable constraint with no hits stays available. An automatically retired lesson with new evidence becomes active. A withdrawn lesson that is observed again stays withdrawn. A superseding decision wins current recall. An old transcript matching the query does not return obsolete advice as current. Retained memories stay inspectable after compaction. A restored older snapshot still honors forgetting.

## Needs environment

- **Remote `memory_search` quality:** needs the mirror back online. Deferred.
- **Pi automatic surfacing:** no Pi extension calls the prompt hook. Filed as AGM-9; the fix lives in `usr`.

## Execution boundary and non-goals

- **Allowed:** local code, tests, fixtures, and docs in this repo; offline evals; local commits.
- **Forbidden:** production writes; Worker changes; deleting stored data; paid runs without approval; editing `usr`.
- **Not in scope:** embeddings or any model in retrieval; FTS `detail=` changes; replacing SQLite or the JSON encodings; enforced pruning.
- **Contract changes for the owner:** MQ-2 adds a `retired` to `retained` state transition. MQ-7 adds fields to `archive status` and `doctor` JSON. MQ-8 changes the on-disk object format and needs a reader for both encodings. MQ-9 adds an `archive plan` command. MQ-10 changes what the prompt hook can surface. MQ-12 changed hook line lengths. MQ-13 drops an index from existing databases.
