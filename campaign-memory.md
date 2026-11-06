# Campaign: memory activation and hybrid recall

## Status

- **Goal:** Recall reaches agents without them choosing to call it, and recall finds paraphrased experience without an agent-written retry, without losing exact-identifier lookup, correct abstention, or the authority and source-verification guarantees.
- **Base revision:** `3ba4a00`.
- **Accepted:** M0 (`83d3745`), M1 (`d3bb6e9`), M2 (`5430904`), M3 (usr `aba8c54`), M5 (`944be8a`), M6 (`2c47459` and the deadline commit after it). **Red:** none. **Withdrawn:** M4. The owner ruled out models in retrieval (Owner 6); its code and downloaded models are deleted.
- **M6 evidence:** `make test-python` and `make lint` green before the review fixes. After them, the recall, learning, learning-ops, and recovery suites pass, plus ruff and ty. The independent review found that `Learner` initialization took a write lock before the deadline, so a concurrent `run` held every prompt for the 5 s harness timeout (reproduced, then fixed, with a regression test under reserved and exclusive locks). It also found follow-through, `quality`, retirement, forget, and markup gaps, all fixed with tests that fail on the earlier code. A live probe on the real state after the fix took 2.2 s cold under load average 9, so the deadline was raised from 2 s to 3 s; warm, the hook injects in 0.6 s.
- **Not done:** the live paid evaluation (out of scope). Whether injected results change outcomes is unmeasured; `agent-learn quality` now reports `hook_attempts` and `hook_outcomes` as sessions accumulate.
- **Next slice:** none. The campaign is closed.

## Charter

- Decide and keep going. Record a decision in **Decisions** only when a later slice depends on it.
- Execution grant: everything under **Execution boundary**. Park any item that needs more.
- One writer by default. M0's split authoring is delegated to an agent that has not seen `src/agent_memory/recall.py` or any benchmark result, so the split stays independent.
- A failing item gets its cause before any retry (`root-cause`). Allow 3 attempts per slice, then park it on `park/<id>` with the cause and a retry condition in **Status**.
- Commits: Conventional Commits with the workstream as scope (`eval`, `recall`, `hook`, `docs`); the item ID goes in the body; code comments never cite this doc; no formatters. Push each accepted slice.
- Scratch under `.work/`; tests run with `TMPDIR="$PWD/.work/tmp"`.
- A negative result is an accepted outcome. It is recorded in the README with its numbers, and the code that produced it is deleted.

## Decisions

### Owner (2026-11-06)

1. Order: activation first, then retrieval quality.
2. No new runtime dependency in the package.
3. `~/usr` edits are authorized for hook wiring and the `do` recall rule.
4. The live paid evaluation (`eval/live_recall.py --live`) is out of scope.
5. Out of scope: a model-based knowledge extraction lane, semantic duplicate adjudication, expiration timestamps, extracted preferences, workspace-shared memory.
6. Retrieval uses no models: no embeddings, rerankers, or model calls in recall or the hook. Model review under `agent-learn enable` is unaffected.

### Prescribed

| # | Decision | Reason |
| --- | --- | --- |
| P1 | `heldout-2` is written once, before any tuning, and frozen. Every acceptance number for a retrieval change comes from it. `dev` stays the tuning split; `heldout` is reported but no longer counts as independent. | `heldout` results have been read repeatedly, and it has only 16 paraphrase cases. |
| P2 | The opportunity replay and as-of filtering live in `eval/`, not in the product. | It is a measurement, not a feature. |
| P3 | The prompt hook never indexes, never calls a model, and fails open with `{}` under a 3 s deadline. It runs once per session, on the first task prompt, and records the attempt. It injects only non-abstaining results, at most 3 lines and 600 characters, and drops user-turn leads that echo the prompt. It fires only in projects whose experience is captured (observation, a project grant, or the wildcard). | Hooks run on every prompt; the existing abstention gate is the relevance filter. M1 measured opening prompts, so once per session is the measured behavior. No project had observation on; captured experience comes from review grants. |
| P4 | Injected text goes into the user turn (Claude Code `additionalContext`), never the system prompt. Receipts carry `source: "hook"`. | Keeps the prompt cache intact and makes hook-sourced recall measurable through the existing follow-through. |

## Sequence

| ID | Slice | Acceptance |
| --- | --- | --- |
| M0 | Re-baseline `dev` and `heldout` per category, and label every paraphrase miss by the record kind it targets. A separate agent writes `eval/recall-fixtures/heldout-2/` (at least 60 cases, at least 25 paraphrase, at least 12 that should abstain, at least 10 exact identifier or path), plus its retries. | `bench_recall.py validate` passes on all three splits; the baseline table is in **Baseline** |
| M1 | `eval/replay_recall.py`: for each archived session's opening prompt, run recall over only the records and sessions that existed before it began. Report sessions, substantive prompts, non-abstaining hits, sampled relevance, and whether the agent called recall. | Report in **Baseline**. If fewer than 5% of substantive prompts get a relevant hit, M2 and M3 are parked with the numbers and the campaign closes. |
| M2 | `agent-learn hook` handles `UserPromptSubmit` per P3 and P4. Tests cover deadline, abstention, deduplication, the observation gate, worker sessions, and the output shape. | The suite passes; the hook's warm latency on the real state directory is measured when the perf gate is ready |
| M3 | `~/usr`: wire the Claude Code `UserPromptSubmit` hook (and Codex if its hook system has a prompt event), and update the `do` recall rule to say hooked harnesses already receive recall. | The `usr` checks pass; the hook runs in a real session |
| M4 | Withdrawn (Owner 6). | — |
| M5 | Record the M4 result in the README. | `make test-python`, `make lint` |
| M6 | Close: `review` of the campaign diff, fix what it finds, final report in **Status**. | `make test-python`, `make lint` |

## Baseline

### M0 (2026-11-06, `94b28bd`, `--system new`, no haystack)

| Split | Recall@5 | Paraphrase Recall@5 | After one retry | Correct abstention | Leakage, stale |
| --- | --- | --- | --- | --- | --- |
| dev (175 cases) | .65 | .16 (5/31) | .82 | .89 (.85 after retry) | 0, 0 |
| heldout (141) | .68 | .19 (3/16) | .80 | 1.00 | 0, 0 |
| heldout-2 (92) | .34 | .03 (1/29) | .72 | 1.00 | 0, 0 |

`heldout-2` was written by an agent that did not read `src/agent_memory/` or any result. It has 12 projects, 54 episodes, 13 memories, 92 cases, of which 29 are paraphrase and 21 abstain.

Paraphrase misses by the record kind that holds the evidence:

| Split | Approved memory | Experience | Session events only |
| --- | --- | --- | --- |
| dev (26 misses) | 7 | 6 | 13 |
| heldout (13 misses) | 5 | 1 | 7 |

Half the misses have no compact record behind them, so a leg over compact records alone can recover at most the other half.

### M1 (2026-11-06, snapshot of the real `~/.agent-sessions`: 2,674 sessions, 2.0 M events, 1,037 retained experiences, 0 approved memories)

`eval/replay_recall.py` replays each top-level session's first task prompt through recall as of that session's start (no records observed later, no sessions updated later). A sampled relevance judge (a separate agent, 60 hits per run) labeled what would have been shown.

| Run | Task prompts | Non-abstaining | Relevant / marginal / irrelevant in sample | Echoes in sample | Median, p95 |
| --- | --- | --- | --- | --- | --- |
| Plain recall | 739 | 410 (55%) | 14 / 37 / 9 | 33 | 665 ms, 1.31 s |
| Hook rendering (echoes dropped, instruction-file prompts skipped) | 732 | 213 (29%); 155 more were echo-only | 34 / 21 / 5 | 2 | 519 ms, 1.20 s |

Agents called recall themselves in 51 of 988 sessions. With hook rendering, about 17% of task prompts would get a relevant injection and about 2% an irrelevant one. Results are mostly event leads (402 of 427 lines); experiences are 25. The host was busy throughout, so the latencies come from a run that overlapped other load and are not perf-gated.

## Execution boundary and non-goals

**Authorized:** local builds and tests; writes under `.work/`; commits and pushes in this repo and `~/usr`.

**Forbidden:** every production write listed in `AGENTS.md`; paid model calls; CI changes; a runtime dependency in the package.

**Contract changes, for the owner:** the hook's `UserPromptSubmit` input and output; the `source` field on receipts.

## Reference

| Concern | Location |
| --- | --- |
| Hook entry | `src/agent_memory/learning.py:1764`, `src/agent_memory/learning_ops.py` `hook` |
| Recall pipeline | `src/agent_memory/recall.py` `Retriever` |
| Recall CLI | `src/agent_memory/learning.py:1238` |
| Benchmark | `eval/bench_recall.py` (`RecallSystem`, `prepare`, `aggregate`) |
| Codex hooks | `~/usr/dotfiles/base/.codex/config.toml` `[[hooks.Stop]]` |
| Claude Code hooks | `~/usr/dotfiles/base/.claude/settings.json` `.hooks` |
| `do` recall rule | `~/usr/dotfiles/base/.agents/skills/do/SKILL.md:22` |

### M4

One measurement ran before the slice was withdrawn: a gated local-embedding leg raised paraphrase Recall@5 on `heldout-2` from .03 to .07, with no change in abstention or identifier lookup. It was not shipped.
