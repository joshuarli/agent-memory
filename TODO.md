Prefix: AGM
Next: 5

### AGM-1 · P1 · CLI recall receipts lose the calling session

- **Area:** Data
- **Where:** `src/agent_memory/learning.py:1889`, `src/agent_memory/learning.py:1897`, `src/agent_memory/learning.py:1630`, `src/agent_memory/learning.py:146`
- **Problem:** Every agent-initiated `agent-learn recall` writes `learning_receipts.session` as NULL. In the local store on 2026-11-06, 190 of 190 agent receipts had `session=NULL`; only the single hook receipt carried a session. `retrieve()` and `record_receipts()` already accept a session, but the CLI dispatch at line 1889 never supplies one, and `inspect` at 1897 does the same. `quality` then reconstructs the session in `_receipt_session()` by scanning the project's sessions for an `agent-learn ... recall` tool call within ±120 seconds. Concurrent sessions, delayed transcript writes, or a call made outside a transcript event misattribute follow-through to the wrong session, so per-session and per-harness effectiveness numbers can be silently wrong.
- **Recommendation:** Add `--session HARNESS:ID` to `recall` and `inspect`, and honor a harness-supplied session environment variable where one exists; pass the value through to `retrieve(..., session=...)` and `inspect(..., session=...)` and store it. Make `_receipt_session()` prefer the stored id and keep the ±120 s scan only for legacy NULL rows. Add a test with two concurrent sessions in one project asserting distinct attribution.
- **Fix with:** durable-state
- **Effort:** S

### AGM-2 · P1 · `agent-sessions search` writes no usage receipt

- **Area:** Operability
- **Where:** `src/agent_memory/sessions_cli.py:794`, `src/agent_memory/learning.py:145`, `src/agent_memory/learning.py:191`, `src/agent_memory/learning.py:1571`
- **Problem:** `_search()` refreshes the index, queries it, and prints, but records nothing. `learning_receipts.source` only permits `agent` and `hook` (migration at line 191), so the table cannot represent a search. `agent-learn quality` therefore reports recall and hook activity only. A 30-day transcript scan on 2026-11-06 found about 40 `agent-sessions search` invocations (opencode 22, Claude 6, Codex 5, Muse 5, plus this session) against about 106 recall invocations; none of the searches appear in receipts. The question "are agents using agent-sessions?" cannot be answered from the quality surface.
- **Recommendation:** Record one minimal usage row per search invocation with project, cwd, normalized query, session, harness, result count, and whether it ran centrally. Add a dedicated `learning_search_usage` table rather than overloading the record-oriented receipts table; reuse the session attribution from AGM-1. Report search and recall side by side in `quality`.
- **Fix with:** durable-state
- **Effort:** S

### AGM-3 · P1 · `quality` has no invocation or option-usage report

- **Area:** Operability
- **Where:** `src/agent_memory/learning.py:1571`, `src/agent_memory/learning.py:146`, `src/agent_memory/learning.py:1765`, `src/agent_memory/sessions_cli.py:1009`
- **Problem:** `quality()` aggregates receipts by event, tier/status, source, and verification, but cannot show how often a tool is invoked, by which harness or project, or with which options. The receipts table stores no option columns, and neither `recall`'s flags (`--weak`, `--explain`, `--no-verify`, `--no-refresh`, `--limit`) nor `search`'s flags (`--regex`, `--kind`, `--since`, `--limit`, `--central`) are persisted. The 30-day sample shows `--weak` never used and `--explain` effectively never, and the "retry once after an abstention" rule cannot be measured at all; that evidence currently requires ad hoc transcript mining.
- **Recommendation:** Store the normalized option set on each usage row (a compact JSON column or explicit booleans), then add `quality.usage` with invocation counts by harness/project, option histograms, abstention rate, and the rate at which an abstention is followed by a retry in the same session. Keep the existing observational fields unchanged.
- **Fix with:** durable-state
- **Effort:** S

### AGM-4 · P1 · Effectiveness is observational; the hook has never been measured

- **Area:** Tests
- **Where:** `README.md:406`, `README.md:411`, `src/agent_memory/learning.py:1608`, `src/agent_memory/learning.py:1649`, `eval/live_recall.py:227`, `src/agent_memory/recall_hook.py:93`
- **Problem:** `quality` reports presented/acted/verified, but `_follow_through()` only checks that a later tool call used an identifier from the result and that a later test/build run passed or failed; `not_observable` states that obedience, missed memories, and causal improvement are unobservable. The one live evaluation found no treatment advantage and no run invoked `recall`. The prompt hook has since been added, but by 2026-11-06 it had only 2 attempts (1 abstention, 1 timeout, 0 injections), so whether injected results change behavior or outcomes is unmeasured.
- **Recommendation:** Add a hook-injected arm to `eval/live_recall.py --live` alongside the no-memory and treatment arms, plus an unrelated-hook control; score the commands run and task completion, not prose, and record the result in `README.md`'s measured results. Keep the paid run behind explicit approval. Treat the result as one model and task family unless the evaluation is widened.
- **Fix with:** none
- **Effort:** M
