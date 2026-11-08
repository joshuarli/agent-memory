# Is agent-memory effective?

Assessed 2026-11-07, at the end of the [memory-quality campaign](campaign-memory-quality.md). The project is shelved on this result.

## Conclusion

Not shown to be effective in real use. A remembered lesson changes the outcome when it reaches the agent, and the prompt hook delivers it on a task built to need it. On real queries, retrieval rarely returns the answer, so the mechanism does not yet pay off in daily work.

## Evidence

| Question | Result | Evidence |
| --- | --- | --- |
| Does a lesson help when the agent sees it? | Oracle arm (lesson pasted into the prompt): 5/5 correct. Baseline without memory: 0 to 1 of 5. | `eval/live_recall.py`, Codex `gpt-6-luna` at `medium`, one constructed task, 5 runs per arm |
| Does the prompt hook deliver it? | 4/5 correct; 1 of those 5 still repeated the mistake. The hook retrieved the right experience every time. | Same run, after MQ-14 rewrote resolved-failure claims |
| Do agents call `recall` when told they can? | 0/5. They never called it. | Same run, treatment arm |
| Does memory interfere with unrelated tasks? | No. Both control arms were clean. | Same run |
| On real back-references, is the right earlier session cited? | 16/45 (36%) | 69 labeled real queries, scored as of each prompt (`eval/real_recall.py`) |
| Is the answer text returned? | 5/45 (11%) | Same corpus |
| When nothing applies, does recall abstain? | 18/24 (75%). The rest inject something irrelevant. | Same corpus |
| Synthetic held-out split | Session hit 24/71, answer text 6/71 by default | `eval/bench_recall.py`, heldout-2 |

## Reading

1. The idea holds. The oracle arm is the ceiling, and it is high.
2. Only push delivery works. Agents do not pull from memory on their own.
3. Retrieval is the bottleneck. It usually misses the fact, or cites the right session without the line holding it. MQ-11's model-free retrieval changes raised the dev split only and did not generalize.
4. The need is rarer than assumed. About 8% of prompts that sound like a back-reference ("again", "last time") refer to an earlier session.

## Limits of this evidence

- The live evaluation is one constructed task with 5 runs per arm: smoke-level, not causal.
- A DeepSeek model labeled the real corpus. Every cited answer was checked to appear verbatim in its cited earlier session, but no person reviewed the judgments. The corpus is private, under `~/.local/state/agent-memory/eval/real-queries/`.
- Nothing measured whether injected memory changed what agents did in real sessions.

## What would reopen it

The hook writes a receipt for every injection (`learning_receipts`). Joining receipts with the commands, edits, and `inspect` calls that follow them in the same session would show whether agents use what is injected. That needs no labels and no paid runs. Retrieval worth retrying would target answer text on the real corpus, not the synthetic dev split.
