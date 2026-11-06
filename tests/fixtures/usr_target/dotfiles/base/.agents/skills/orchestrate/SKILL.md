---
name: orchestrate
description: "Delegate to subagents. Skip tightly coupled work."
---

# Orchestrate

| Work | Codex | Claude and Grok | OpenCode | Command Code | Muse Code | Antigravity CLI | DeepSeek TUI | fx | Pi |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Read-only scouting, searches, inventories, documentation lookup, log triage, straightforward fact gathering | `gpt-6-luna`, `medium` | `scout` | `opencode-go/muse-spark-1.3-contributor`, `explore` | `deepseek/deepseek-v4.1-flash`, `Explore` | `muse-spark-1.3`, `low` | `gemini-3.8-flash-medium` | `deepseek-flash`, `low` | `meta/muse-spark-1.3-contributor`, `low` | `opencode-go/muse-spark-1.3-contributor`, `scout`, `low` |
| Mechanical edits, routine implementation, straightforward documentation, crisp acceptance criteria | `gpt-6-luna`, `high` | `mechanic` | `opencode-go/muse-spark-1.3-contributor`, `general` | `deepseek/deepseek-v4.1-flash`, `General` | `muse-spark-1.3`, `medium` | `gemini-3.8-flash-high` | `deepseek-flash`, `low` | `meta/muse-spark-1.3-contributor`, `medium` | `opencode-go/muse-spark-1.3-contributor`, `mechanic`, `medium` |
| Normal implementation, debugging, tests, ordinary review | `gpt-6.1-terra`, `medium` | `worker` | `opencode-go/muse-spark-1.3-contributor`, `general` | `deepseek/deepseek-v4.1-flash`, `General` | `muse-spark-1.3`, `high` | `gemini-3.8-flash-high` | `deepseek-flash`, `high` | `meta/muse-spark-1.3-contributor`, `high` | `opencode-go/muse-spark-1.3-contributor`, `worker`, `high` |
| Difficult debugging, cross-cutting implementation, architecture, integration, demanding review | `gpt-6.1-terra`, `high` | `expert` | `opencode-go/muse-spark-1.3-contributor`, `general` | `deepseek/deepseek-v4.1-flash`, `General` | `muse-spark-1.3`, `xhigh` | `gemini-4-argon-high` | `deepseek-flash`, `max` | `meta/muse-spark-1.3-contributor`, `xhigh` | `opencode-go/muse-spark-1.3-contributor`, `expert`, `xhigh` |
| The hardest work: a failure an `expert` could not explain, a design others will build on, the final adversarial review before landing | `gpt-6.1-sol`, `high` | `god` | unavailable — keep local; do not delegate | unavailable — keep local; do not delegate | `muse-spark-1.3`, `xhigh` | `gemini-4-argon-high` | `deepseek-flash`, `max` | `meta/muse-spark-1.3-contributor`, `xhigh` | `opencode-go/muse-spark-1.3-contributor`, `god`, `xhigh` |

**Report:** delegated lanes and their evidence.
