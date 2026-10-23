"""The one shell-line parser (review round 3, F8): the spec gate, the dev
image's tool check and the verifier read a `test_cmd` the same way.

`parse` splits a line into simple commands at the top-level list and pipe
operators (`&&`, `||`, `|`, `|&`, `;`, `&`, newline), respecting quotes and
backslashes, and keeps each command's span in the original text so a caller
can add to one without re-quoting anything. `&` that is part of a
redirection (`2>&1`, `&>`, `>&2`) is not an operator.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT = re.compile(r"^\d*(>>?|<|>&|<&|&>>?)(\S*)$")
# Wrappers that run the next word as the command.
PREFIXES = {"env", "command", "exec", "builtin", "nohup", "time", "nice", "stdbuf"}


@dataclass(frozen=True)
class Command:
    text: str  # the command as written
    start: int
    end: int  # text == line[start:end]
    words: tuple[str, ...]  # shell words, env assignments and redirections removed

    def _run(self) -> list[str]:
        words = list(self.words)
        while words and words[0] in PREFIXES:
            words.pop(0)
            while words and (words[0].startswith("-") or ASSIGNMENT.match(words[0])):
                words.pop(0)
        return words

    @property
    def program(self) -> str:
        """The command that runs: wrappers skipped, `/bin/true` read as `true`."""
        words = self._run()
        return words[0].rsplit("/", 1)[-1] if words else ""

    @property
    def args(self) -> tuple[str, ...]:
        return tuple(self._run()[1:])


@dataclass(frozen=True)
class Line:
    commands: tuple[Command, ...]
    ops: tuple[str, ...]  # ops[i] sits between commands[i] and commands[i + 1]; may end with one


def _words(text: str) -> tuple[str, ...]:
    try:
        raw = shlex.split(text.strip().strip("(){} \t"), comments=True)
    except ValueError:
        raw = text.split()
    out: list[str] = []
    skip = False
    for w in raw:
        if skip:
            skip = False
            continue
        m = REDIRECT.match(w)
        if m:
            skip = not m.group(2)  # `> file` takes the next word
            continue
        if not out and ASSIGNMENT.match(w):
            continue
        out.append(w)
    return tuple(w for w in out if w not in ("{", "}", "(", ")", "!"))


def parse(line: str) -> Line:
    commands: list[Command] = []
    ops: list[str] = []
    start = i = 0
    quote = ""
    n = len(line)

    def cut(end: int, op: str, nxt: int) -> int:
        commands.append(Command(line[start:end], start, end, _words(line[start:end])))
        ops.append(op)
        return nxt

    while i < n:
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == "#" and (i == 0 or line[i - 1] in " \t\n;&|"):
            nl = line.find("\n", i)
            i = n if nl < 0 else nl
            continue
        two = line[i : i + 2]
        if two in ("&&", "||", "|&"):
            start = cut(i, two, i + 2)
            i = start
            continue
        if c in ";\n|":
            start = cut(i, c, i + 1)
            i = start
            continue
        if c == "&":
            prev = line[i - 1] if i else ""
            nxt = line[i + 1] if i + 1 < n else ""
            if prev in "<>" or nxt == ">":
                i += 1  # part of a redirection
                continue
            start = cut(i, "&", i + 1)
            i = start
            continue
        i += 1
    tail = line[start:]
    if tail.strip() or not commands:  # else a trailing operator: one more op than gaps
        commands.append(Command(tail, start, n, _words(tail)))
    return Line(tuple(commands), tuple(ops))


def programs(line: str) -> list[str]:
    """Each command's program, in order, once each."""
    out: list[str] = []
    for c in parse(line).commands:
        if c.program and c.program not in out:
            out.append(c.program)
    return out
