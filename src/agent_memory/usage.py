"""Tool invocation report derived from indexed transcripts: how agents call `agent-learn recall` and `agent-sessions search`."""
import argparse
import collections
import dataclasses
import json
import re
import shlex
from collections.abc import Iterable, Iterator
from typing import Any, Literal, Optional, cast

Tool = Literal["recall", "search"]
Outcome = Literal["answered", "empty", "unknown"]

WINDOW_DAYS = 30
TOP_PROJECTS = 10
INVOCATION = re.compile(r"(?<![\w./-])(?:\S*/)?(agent-learn\s+recall|agent-sessions\s+search)(?![\w-])")
COMMAND_POSITION = re.compile(r"""(?:^|[;&|({]|\$\(|\b(?:exec|time|env|nohup))\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*$""")
QUOTED_COMMAND = re.compile(r"""(?:"(?:cmd|command|CommandLine)"|\b(?:cmd|command|CommandLine))\s*:\s*"((?:[^"\\]|\\.)*)\"""")
COMMAND_KEY = re.compile(r"^(?:command|CommandLine|cmd)\s*:\s?(.*)$")
FIELD_KEY = re.compile(r"^[A-Za-z][A-Za-z_]*:(?:\s|$)")
BARE_SHELL_TOOLS = frozenset(("Bash", "bash", "shell", "shell_command", "run_command", "run_terminal_command"))
SHELL_OPERATOR = re.compile(r"^[;&|()<>]+$")
HELP = frozenset(("-h", "--help"))
LINE_START = r"(?:^|\\n|\")"
ABSTENTION = re.compile(r"(?m)" + LINE_START + r"\s*\[\]\s*(?:$|\\n|\")|No relevant prior experience")
EMPTY_SEARCH = re.compile(r"(?m)" + LINE_START + r"matches=0\b")
ANSWERED = {"recall": re.compile(r'\\?"ref\\?"\s*:'), "search": re.compile(r"(?m)" + LINE_START + r"matches=[1-9]")}
LIKE_PATTERNS = ("%agent-learn%recall%", "%agent-sessions%search%")


@dataclasses.dataclass(frozen=True)
class Invocation:
    tool: Tool
    options: frozenset[str]
    help: bool


def option_names(cli: argparse.ArgumentParser, command: str) -> frozenset[str]:
    commands = next(action for action in cli._actions if isinstance(action, argparse._SubParsersAction))
    subcommand = cast(argparse.ArgumentParser, commands.choices[command])
    names: set[str] = {name for action in subcommand._actions for name in action.option_strings}
    return frozenset(name for name in names if name.startswith("--") and name != "--help")


def _command_lines(text: str) -> Iterator[str]:
    for match in QUOTED_COMMAND.finditer(text):
        try:
            decoded: object = json.loads('"' + match.group(1) + '"')
        except ValueError:
            continue
        if isinstance(decoded, str):
            yield from decoded.splitlines()
    lines = QUOTED_COMMAND.sub(" ", text).splitlines()
    in_command = bool(lines) and lines[0].strip() in BARE_SHELL_TOOLS
    for line in lines[1:]:
        key = COMMAND_KEY.match(line)
        if key:
            in_command = True
            yield cast(str, key.group(1))
        elif FIELD_KEY.match(line):
            in_command = False
        elif in_command:
            yield line


def _unquoted(prefix: str) -> bool:
    quote = None
    escaped = False
    for char in prefix:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote is None and char in "'\"":
            quote = char
        elif char == quote:
            quote = None
    return quote is None


def _arguments(rest: str) -> list[str]:
    lexer = shlex.shlex(rest, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = []
    try:
        for token in lexer:
            if SHELL_OPERATOR.match(token):
                break
            tokens.append(token)
    except ValueError:
        return cast(str, re.split(r"[;&|]", rest, maxsplit=1)[0]).split()
    return tokens


def command_count(text: str) -> int:
    lines = text.splitlines()
    keyed = len(QUOTED_COMMAND.findall(text)) + sum(1 for line in lines[1:] if COMMAND_KEY.match(line))
    return keyed or int(bool(lines) and lines[0].strip() in BARE_SHELL_TOOLS)


def invocations(text: str, known: dict[Tool, frozenset[str]]) -> list[Invocation]:
    found = []
    for line in _command_lines(text):
        for match in INVOCATION.finditer(line):
            prefix = line[:match.start()]
            if not COMMAND_POSITION.search(prefix) or not _unquoted(prefix):
                continue
            tool: Tool = "recall" if match.group(1).startswith("agent-learn") else "search"
            arguments = _arguments(line[match.end():])
            flags = {argument.partition("=")[0] for argument in arguments}
            found.append(Invocation(tool, frozenset(flags & known[tool]), bool(flags & HELP)))
    return found


def outcome(tool: Tool, output: Optional[str]) -> Outcome:
    if output is None:
        return "unknown"
    if ANSWERED[tool].search(output):
        return "answered"
    if (ABSTENTION if tool == "recall" else EMPTY_SEARCH).search(output):
        return "empty"
    return "unknown"


@dataclasses.dataclass(frozen=True)
class Call:
    session: str
    harness: str
    project: Optional[str]
    position: int
    text: str
    output: Optional[str]


def report(calls: Iterable[Call], known: dict[Tool, frozenset[str]]) -> dict[str, Any]:
    tools: dict[Tool, dict[str, Any]] = {tool: {"invocations": 0, "help_only": 0, "by_harness": collections.Counter(), "by_project": collections.Counter(),
                                                 "options": collections.Counter(), "outcomes": collections.Counter()} for tool in ("recall", "search")}
    empty_at: dict[tuple[Tool, str], int] = {}
    retried: collections.Counter[Tool] = collections.Counter()
    for call in sorted(calls, key=lambda item: (item.session, item.position)):
        found = invocations(call.text, known)
        for invocation in found:
            entry = tools[invocation.tool]
            if invocation.help:
                entry["help_only"] += 1
                continue
            key = (invocation.tool, call.session)
            if key in empty_at:
                retried[invocation.tool] += 1
                del empty_at[key]
            entry["invocations"] += 1
            entry["by_harness"][call.harness] += 1
            entry["by_project"][call.project or "unknown"] += 1
            entry["options"].update(sorted(invocation.options))
            result = outcome(invocation.tool, call.output) if len(found) == 1 and command_count(call.text) == 1 else "unknown"
            entry["outcomes"][result] += 1
            if result == "empty":
                empty_at[key] = call.position
    for tool, entry in tools.items():
        empty = entry["outcomes"]["empty"]
        entry["by_harness"] = dict(entry["by_harness"].most_common())
        entry["by_project"] = dict(entry["by_project"].most_common(TOP_PROJECTS))
        entry["options"] = {name: entry["options"][name] for name in sorted(known[tool])}
        entry["outcomes"] = {name: entry["outcomes"][name] for name in ("answered", "empty", "unknown")}
        entry["empty_then_retried"] = retried[tool]
        entry["retry_rate_after_empty"] = round(retried[tool] / empty, 3) if empty else None
    return {"window_days": WINDOW_DAYS, **tools}
