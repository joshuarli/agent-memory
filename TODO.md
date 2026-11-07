Prefix: AGM
Next: 10

### AGM-9 · P2 · Pi prompts get no memory surfacing

- **Area:** Agent learning
- **Where:** `usr` `dotfiles/base/.pi/agent/extensions/`, `src/agent_memory/learning.py` (`hook` entry point)
- **Problem:** Claude Code and Codex run `agent-learn hook --harness claude|codex` on `UserPromptSubmit` (`usr` `dotfiles/base/.codex/config.toml:302-306`). Pi's only extension, `git-execution-guard.js`, handles `tool_call` and never calls `agent-learn`, so Pi sessions are indexed but get memory only when the agent calls `recall` itself, which agents did in 51 of 988 sessions.
- **Recommendation:** Add a Pi extension in `usr` that, on the first task prompt of a session, pipes a `UserPromptSubmit`-shaped payload (event name, cwd, session id, prompt) to `agent-learn hook --harness pi` and adds the returned `additionalContext` to that user turn. Skip it for pi-subagents children. Cover it offline the same way `dotfiles/pi-git-guard-test.mjs` covers the git guard.
- **Fix with:** do
- **Effort:** S

### AGM-5 · P3 · Unsegmented scripts match only whole runs

- **Area:** Agent learning
- **Where:** `src/agent_memory/store.py` (`events_fts`)
- **Problem:** The FTS5 `unicode61` tokenizer treats a run of Chinese or Japanese characters as one token, so a query matches only the exact run.
- **Recommendation:** Measure a trigram index for events containing such scripts against its size cost before adding one.
- **Fix with:** none
- **Effort:** M

### AGM-6 · P3 · Muse transcripts lack tool events

- **Area:** Agent learning
- **Where:** `src/agent_memory/transcripts.py` (`_muse_record`)
- **Problem:** Only user messages and skill reads are verified from fixtures, so Muse sessions yield no tool-based experience and Muse ancestry stays unknown.
- **Recommendation:** Capture a real Muse session, add its assistant and tool event shapes and any parent field to the normalizer, and add fixtures.
- **Fix with:** none
- **Effort:** S

### AGM-7 · P3 · Pi subagent delegation is unverified against a real run

- **Area:** Agent learning
- **Where:** `src/agent_memory/transcripts.py` (`default_roots`, `read_header`), `src/agent_memory/store.py` (`_archive_source`)
- **Problem:** Pi sessions are indexed, archived, and branch-filtered from fixtures shaped like two real single-agent sessions. No parallel-subagent run has been captured, so three assumptions are unproven. Child session files may live outside `~/.pi/agent/sessions` (for example under `$TMPDIR/pi-subagents-uid-N/`), where the index would never see them. They may not match the `<timestamp>_<id>.jsonl` name the adapter requires. Run artifacts under that temp directory are cleaned up (workflow artifacts after 24 hours), so they may need archiving. The capture attempt on 2026-10-25 hit the Gemini free-tier limit of 20 requests per day per model, which the first undirected run used up reading documentation.
- **Recommendation:** After the quota resets, run `cd .work/pi-capture && vault GEMINI_API_KEY -- pi -p --no-approve --no-skills 'Call the subagent tool exactly once, in the foreground, with tasks set to two scout children that run in parallel: the first reads a.txt and reports its line count, the second reads b.txt and reports its line count. Do not read any documentation first.'` (create `a.txt` and `b.txt` first), using the default `TMPDIR`. Diff `$TMPDIR/pi-subagents-uid-*` and `~/.pi/agent/sessions` before and after. Add the child session location as a Pi root if it is outside the sessions directory, check that `parentSession` links the child to the parent in `agent-sessions recent`, and add a sanitized fixture to `tests/test_sessions.py`. Archive the run artifacts only if a consumer needs them.
- **Fix with:** none
- **Effort:** S

### AGM-8 · P3 · Mirror durability is bounded by the 15-minute capture interval

- **Area:** Operability
- **Where:** `src/agent_memory/mirror.py` (`sync` calls `refresh` before upload, `pending_report`), `src/agent_memory/store.py` (`pending_generations`, `transfer_generation`), `~/usr/dotfiles/launchd/com.josh.agent-sessions-mirror.plist.in:25`
- **Problem:** Reads and writes are local-first, but remote durability is eventual. The scheduled sync captures first and uploads second every 3600 seconds, so a session written just after a run can stay local-only for about an hour plus upload time. If the host is lost in that window, the remote has no copy. Durability is visible only as aggregate pending generations, objects, and bytes in `mirror status`, not per session. Shortening the interval also shortens the capture interval, and each changed session creates an immutable generation with new 64 KiB chunks, so busy sessions multiply generations, objects, and mirror storage; event-driven capture at session end avoids that churn but hooks are per-harness and unreliable. Synchronous acknowledgement would close the window but couple local writes to Cloudflare latency and availability, contrary to local-first.
- **Recommendation:** Choose an RPO target, then make the smallest matching change. For about five minutes, shorten the scheduled interval and keep uploads asynchronous. For seconds, add a debounced end-of-turn capture-and-sync trigger while retaining the periodic backstop. Add per-session durable and pending reporting by joining `sessions`, `archive_generation`, and `transfer_generation`, and keep synchronous acknowledgement only behind an explicit `--wait` or `--durable` path. Account for generation and object growth before shortening capture itself.
- **Fix with:** do
- **Effort:** M
