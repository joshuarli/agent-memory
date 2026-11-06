"""Shared closed vocabularies for the agent-memory project.

Harness names, finding states, signals, tiers, and retry kinds are closed
sets: a typo is a bug, not a new variant. They are typed as Literals so the
checker rejects misspellings at the boundary where a dynamic string enters.
Event kinds stay open (adapters emit ``unknown:<...>`` for shapes they do not
recognize), so kinds remain plain strings grouped by the *_KINDS tuples.
"""
from typing import Final, Literal

Harness = Literal["codex", "claude", "muse", "grok", "opencode", "pi", "cmd", "agy"]
HARNESSES: Final[tuple[Harness, ...]] = ("codex", "claude", "muse", "grok", "opencode", "pi", "cmd", "agy")

HARNESS_ALIASES: Final[dict[str, Harness]] = {
    "command": "cmd", "command-code": "cmd", "commandcode": "cmd",
    "antigravity": "agy",
}

DatabaseHarness = Literal["opencode"]
DATABASE_HARNESSES: Final[tuple[DatabaseHarness, ...]] = ("opencode",)

OPENCODE_DB: Final = "opencode.db"
OPENCODE_SUBPOSITIONS: Final = 1000

SUBSTANTIVE_KINDS: Final[tuple[str, ...]] = (
    "user", "assistant", "developer", "tool-call", "tool-output",
    "task_complete", "compacted", "context", "skill_read_observed",
)

Tier = Literal["instruction", "experience", "lead"]

FindingState = Literal[
    "pending", "reviewing", "deferred", "rejected", "held",
    "promoted", "memory", "superseded", "reversed",
]

Signal = Literal["correction", "repeated-command", "discovery", "regression", "experience"]

RetryKind = Literal[
    "evidence", "conflict", "git", "lock", "validation",
    "transient", "unsafe", "capability", "none", "invalid_response",
]

FeedbackVerdict = Literal["helped", "irrelevant", "stale", "misleading"]

ReceiptEvent = Literal["returned", "inspected", "abstained"]
