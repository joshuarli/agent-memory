Prefix: AGM
Next: 5

### AGM-4 · P1 · Effectiveness is observational; the hook has never been measured

- **Area:** Tests
- **Where:** `README.md:406`, `README.md:411`, `src/agent_memory/learning.py:1608`, `src/agent_memory/learning.py:1649`, `eval/live_recall.py:227`, `src/agent_memory/recall_hook.py:93`
- **Problem:** `quality` reports presented/acted/verified, but `_follow_through()` only checks that a later tool call used an identifier from the result and that a later test/build run passed or failed; `not_observable` states that obedience, missed memories, and causal improvement are unobservable. The one live evaluation found no treatment advantage and no run invoked `recall`. The prompt hook has since been added, but by 2026-11-06 it had only 2 attempts (1 abstention, 1 timeout, 0 injections), so whether injected results change behavior or outcomes is unmeasured.
- **Recommendation:** Add a hook-injected arm to `eval/live_recall.py --live` alongside the no-memory and treatment arms, plus an unrelated-hook control; score the commands run and task completion, not prose, and record the result in `README.md`'s measured results. Keep the paid run behind explicit approval. Treat the result as one model and task family unless the evaluation is widened.
- **Fix with:** none
- **Effort:** M
