# Campaign: memory activation and hybrid recall

## Status

- **Goal:** Recall reaches agents without them choosing to call it, and recall finds paraphrased experience without an agent-written retry, without losing exact-identifier lookup, correct abstention, or the authority and source-verification guarantees.
- **Base revision:** `3ba4a00`.
- **Accepted:** none. **Red:** none. **Parked:** none.
- **Next slice:** M0.

## Charter

- Decide and keep going. Record a decision in **Decisions** only when a later slice depends on it.
- Execution grant: everything under **Execution boundary**. Park any item that needs more.
- One writer by default. M0's split authoring is delegated to an agent that has not seen `src/agent_memory/recall.py` or any benchmark result, so the split stays independent.
- A failing item gets its cause before any retry (`root-cause`). Allow 3 attempts per slice, then park it on `park/<id>` with the cause and a retry condition in **Status**.
- Commits: Conventional Commits with the workstream as scope (`eval`, `recall`, `hook`, `embed`, `docs`); the item ID goes in the body; code comments never cite this doc; no formatters. Push each accepted slice.
- Scratch under `.work/`; tests run with `TMPDIR="$PWD/.work/tmp"`.
- A negative result is an accepted outcome. It is recorded in the README with its numbers, and the code that produced it is deleted.

## Decisions

### Owner (2026-11-06)

1. Order: activation first, then the semantic leg. The semantic leg is measured against hook-mode lexical recall (default, no retry), because a hook cannot write the retry that rescues paraphrases today.
2. The embedder is a local model behind an external command. No hosted embeddings, no reranker, no new runtime dependency in the package.
3. `~/usr` edits are authorized for hook wiring and the `do` recall rule.
4. The live paid evaluation (`eval/live_recall.py --live`) is out of scope.
5. Out of scope: a model-based knowledge extraction lane, semantic duplicate adjudication, expiration timestamps, extracted preferences, workspace-shared memory.

### Prescribed

| # | Decision | Reason |
| --- | --- | --- |
| P1 | `heldout-2` is written once, before any tuning, and frozen. Every acceptance number for the semantic leg comes from it. `dev` stays the tuning split; `heldout` is reported but no longer counts as independent. | `heldout` results have been read repeatedly, and it has only 16 paraphrase cases. |
| P2 | The opportunity replay and as-of filtering live in `eval/`, not in the product. | It is a measurement, not a feature. |
| P3 | The prompt hook never indexes, never calls a model, and fails open with `{}` under a hard deadline. It injects only non-abstaining results, at most 3 records and 600 characters, and never repeats a reference already shown in that session. It fires only for projects with observation on. | Hooks run on every prompt; the existing abstention gate is the relevance filter. |
| P4 | Injected text goes into the user turn (Claude Code `additionalContext`), never the system prompt. Receipts carry `source: "hook"`. | Keeps the prompt cache intact and makes hook-sourced recall measurable through the existing follow-through. |
| P5 | Vectors are a derived index: one table keyed by model id and content digest, float32 via `array`, rebuilt by `reindex`, removed on `forget` and tombstone, excluded from backup and mirror. | Rebuildable from canonical records; no new durable contract. |
| P6 | The semantic leg covers compact records only (approved memories and experiences), not raw session events. It adds candidates through reciprocal rank fusion; the existing relevance gate, authority classification, and source-condition checks decide what is returned. | Exact lookups stay deterministic; semantics only add candidates. |

## Sequence

| ID | Slice | Acceptance |
| --- | --- | --- |
| M0 | Re-baseline `dev` and `heldout` per category, and label every paraphrase miss by the record kind it targets. A separate agent writes `eval/recall-fixtures/heldout-2/` (at least 60 cases, at least 25 paraphrase, at least 12 that should abstain, at least 10 exact identifier or path), plus its retries. | `bench_recall.py validate` passes on all three splits; the baseline table is in **Baseline** |
| M1 | `eval/replay_recall.py`: for each archived session's opening prompt, run recall over only the records and sessions that existed before it began. Report sessions, substantive prompts, non-abstaining hits, sampled relevance, and whether the agent called recall. | Report in **Baseline**. If fewer than 5% of substantive prompts get a relevant hit, M2 and M3 are parked with the numbers and the campaign continues at M4. |
| M2 | `agent-learn hook` handles `UserPromptSubmit` per P3 and P4. Tests cover deadline, abstention, deduplication, the observation gate, worker sessions, and the output shape. | The suite passes; the hook's warm latency on the real state directory is measured when the perf gate is ready |
| M3 | `~/usr`: wire the Claude Code `UserPromptSubmit` hook (and Codex if its hook system has a prompt event), and update the `do` recall rule to say hooked harnesses already receive recall. | The `usr` checks pass; the hook runs in a real session |
| M4 | The embedder protocol, the vector index (P5), and the fused candidate leg (P6), compared on all three splits across four arms: lexical, semantic fallback on abstain, RRF hybrid, and hook-mode lexical. | **A1** on `heldout-2` |
| M5 | Ship the winning arm as the default when an embedder is configured, or record the negative result and delete the leg. Update the README results and limits. | `make test-python`, `make lint` |
| M6 | Close: `review` of the campaign diff, fix what it finds, final report in **Status**. | `make test-python`, `make lint` |

## Acceptance

- **A1** (semantic leg on `heldout-2`, against hook-mode lexical): paraphrase Recall@5 gains at least 15 points; overall Recall@5 does not fall; no exact_identifier, path_symbol_command, or short_identifier case regresses; correct abstention falls by at most .02; cross-project leakage and stale-as-verified violations stay 0. Added recall latency and index size per 1,000 records are reported.

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

## Execution boundary and non-goals

**Authorized:** local builds and tests; writes under `.work/`; commits and pushes in this repo and `~/usr`; installing a local embedding model for the benchmark.

**Forbidden:** every production write listed in `AGENTS.md`; paid model calls; CI changes; a runtime dependency in the package.

**Contract changes, for the owner:** the hook's `UserPromptSubmit` input and output; the `source` field on receipts; the embedder command protocol and its `config.json` key; the vector table.

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
