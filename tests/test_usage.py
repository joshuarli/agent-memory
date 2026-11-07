#!/usr/bin/env python3
"""Tool invocation report: which recorded tool calls count as recall or search, with which options, and what came back."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import testkit  # noqa: E402
sys.path.insert(0, str(testkit.SRC))

from agent_memory import learning, sessions_cli, usage  # noqa: E402

KNOWN: dict[usage.Tool, frozenset[str]] = {"recall": usage.option_names(learning.parser(), "recall"), "search": usage.option_names(sessions_cli.parser(), "search")}


def found(text):
    return [(item.tool, sorted(item.options), item.help) for item in usage.invocations(text, KNOWN)]


def call(text, output=None, session="s", position=0, harness="codex", project="git:/repo/.git"):
    return usage.Call(session, harness, project, position, text, output)


class InvocationTests(unittest.TestCase):
    def test_each_harness_shell_format_is_recognized(self):
        cases = {
            "claude": "Bash\ncommand: agent-learn recall 'review rescore' --project /r 2>&1 | head -40; rg -n x\ndescription: Recall",
            "claude bare": "Bash\nagent-learn recall 'follower waits' --project /r --weak 2>&1 | head -30",
            "opencode": "shell\ncommand: cd /r && agent-learn recall 'speed cache' --project /r --explain || echo NO\nworkdir: /r",
            "agy": "run_command\nCommandLine: agent-learn recall 'delete async' --project /r\ntoolSummary: Recall",
            "codex": 'exec\nconst r = await tools.exec_command({cmd:"agent-sessions search \'.\' --regex --kind user --limit 100","workdir":"/r"}); text(r.output);',
            "muse": "bash\ncommand: agent-sessions show x --all-projects\necho ===\nagent-sessions search 'ses_1' --all-projects --kind user 2>&1\ndescription: Find",
            "grok": "read_file\ntarget_file: /x/SKILL.md\nrun_terminal_command\ncommand: perf-gate-ready -s; agent-learn recall 'ultrareview docs' --project /r",
        }
        expected = {
            "claude": [("recall", ["--project"], False)],
            "claude bare": [("recall", ["--project", "--weak"], False)],
            "opencode": [("recall", ["--explain", "--project"], False)],
            "agy": [("recall", ["--project"], False)],
            "codex": [("search", ["--kind", "--limit", "--regex"], False)],
            "muse": [("search", ["--all-projects", "--kind"], False)],
            "grok": [("recall", ["--project"], False)],
        }
        self.assertEqual({name: found(text) for name, text in cases.items()}, expected)

    def test_mentions_in_file_content_queries_and_prose_are_not_invocations(self):
        texts = [
            'exec\nconst patch = "*** Begin Patch\\n*** Update File: SKILL.md\\n+- run `agent-learn recall QUERY --project PATH` first\\n*** End Patch";',
            "Write\nfile_path: /r/docs.md\ncontent: # Docs\n\nagent-learn recall 'x' --project .\n",
            "Bash\ncommand: agent-sessions search 'agent-learn recall' --all-projects 2>&1 | head",
            "Bash\ncommand: rg -n 'do/SKILL|agent-learn recall' dotfiles | head",
            "edit\npath: /r/a.md\nnewString: agent-sessions search foo",
        ]
        self.assertEqual([found(text) for text in texts], [[], [], [("search", ["--all-projects"], False)], [], []])

    def test_help_is_counted_apart_from_use(self):
        self.assertEqual(found("Bash\ncommand: agent-sessions search --help 2>&1 | sed -n 1,30p"), [("search", [], True)])


class ReportTests(unittest.TestCase):
    def test_outcomes_read_plain_and_json_escaped_output(self):
        self.assertEqual([usage.outcome("recall", '[{"ref":"experience:3","tier":"experience"}]'),
                          usage.outcome("recall", "[]\nagent-learn recall: No relevant prior experience. Proceed normally"),
                          usage.outcome("recall", 'Output:\n{"exit_code":0,"output":"[]\\nagent-learn recall: hint"}'),
                          usage.outcome("recall", 'Output:\n{"output":"[{\\"ref\\":\\"memory:1\\"}]"}'),
                          usage.outcome("search", "session=a byte=1\n  text\nmatches=1"),
                          usage.outcome("search", "sessions=3 events=9\nmatches=0"),
                          usage.outcome("recall", "Traceback (most recent call last):"),
                          usage.outcome("recall", None)],
                         ["answered", "empty", "empty", "answered", "answered", "empty", "unknown", "unknown"])

    def test_report_counts_options_harnesses_and_retries_after_an_empty_result(self):
        recall = "Bash\ncommand: agent-learn recall '%s' --project /r"
        calls = [
            call(recall % "vague words", "[]", session="a", position=1, harness="claude"),
            call(recall % "orbit_core ModuleNotFoundError" + " --weak", '[{"ref":"experience:1"}]', session="a", position=5, harness="claude"),
            call(recall % "other", "[]", session="b", position=1),
            call("Bash\ncommand: agent-sessions search --help", "usage: ...", session="b", position=2),
            call("Bash\ncommand: agent-sessions search orbit --kind user --kind tool-call", "matches=0", session="b", position=3, project=None),
            call('exec\nconst r = await Promise.all([tools.exec_command({cmd:"agent-learn recall \'q\'"}), tools.exec_command({cmd:"git status"})]);',
                 '[{"ref":"experience:1"}]', session="c", position=1),
        ]
        report = usage.report(calls, KNOWN)
        recalled, searched = report["recall"], report["search"]
        self.assertEqual((recalled["invocations"], recalled["by_harness"], recalled["outcomes"]),
                         (4, {"claude": 2, "codex": 2}, {"answered": 1, "empty": 2, "unknown": 1}))
        self.assertEqual((recalled["options"]["--project"], recalled["options"]["--weak"], recalled["options"]["--explain"]), (3, 1, 0))
        self.assertEqual((recalled["empty_then_retried"], recalled["retry_rate_after_empty"]), (1, 0.5))
        self.assertEqual((searched["invocations"], searched["help_only"], searched["by_project"], searched["options"]["--kind"], searched["outcomes"]["empty"]),
                         (1, 1, {"unknown": 1}, 1, 1))
        self.assertEqual((searched["empty_then_retried"], searched["retry_rate_after_empty"]), (0, 0.0))


if __name__ == "__main__":
    unittest.main()
