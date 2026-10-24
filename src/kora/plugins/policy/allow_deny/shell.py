import re
from dataclasses import dataclass
from typing import Final

from kora.ir.globs import (
    CALL_CHARGE,
    PIECE_CHARGE,
    Fuel,
    StarPattern,
    folded,
    has_control_character,
    needle_weight,
    star_match,
)

MAX_COMMAND_CHARACTERS: Final[int] = 16_384
OUTSIDE_QUOTES: Final[frozenset[str]] = frozenset(";&|()<>`$\\{}#*?[]~!^")
SUBSTITUTION_OPENERS: Final[frozenset[str]] = frozenset("({")
INSIDE_DOUBLE_QUOTES: Final[frozenset[str]] = frozenset("$`\\!")
QUOTES: Final[frozenset[str]] = frozenset("'\"")
ASSIGNMENT: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s")
FIRST_WORDS: Final[frozenset[str]] = frozenset(
    {
        "env",
        "xargs",
        "eval",
        "exec",
        "source",
        ".",
        "command",
        "builtin",
        "sudo",
        "doas",
        "nohup",
        "timeout",
        "nice",
        "time",
        "stdbuf",
        "setsid",
        "busybox",
        "osascript",
        "apply_patch",
        "applypatch",
        "!",
        "coproc",
        "trap",
        "noglob",
        "nocorrect",
        "if",
        "while",
        "until",
        "for",
        "case",
        "select",
        "function",
        "then",
        "do",
        "[[",
        "uv",
        "uvx",
        "npx",
        "bunx",
        "pnpm",
        "poetry",
        "watch",
        "caffeinate",
        "arch",
        "script",
        "flock",
        "chroot",
        "ionice",
        "taskset",
        "chrt",
        "unbuffer",
        "parallel",
        "su",
        "runuser",
        "strace",
        "ltrace",
        "gdb",
        "ssh",
        "tmux",
        "screen",
        "nix-shell",
        "let",
        "declare",
        "typeset",
        "local",
        "export",
        "readonly",
        "read",
        "printf",
        "print",
        "unset",
        "test",
        "[",
        "mapfile",
        "readarray",
        "wait",
        "getopts",
        "integer",
        "float",
        "vared",
        "repeat",
        "foreach",
        "exit",
        "return",
        "shift",
        "break",
        "continue",
        "logout",
        "bye",
        "compgen",
        "complete",
        "ulimit",
        "jobs",
    }
)
AWK_NAME: Final[re.Pattern[str]] = re.compile(r"[gmn]?awk")
SHELL_NAME: Final[re.Pattern[str]] = re.compile(
    r"r?(?:sh|bash|zsh|dash|ksh|fish|csh|tcsh|ash|mksh)(?:-?[0-9][0-9.]*)?"
)
OPTION_CLUSTER: Final[re.Pattern[str]] = re.compile(r"-[A-Za-z]*[ceE]")
INTERPRETER_PREFIXES: Final[tuple[str, ...]] = (
    "python",
    "pypy",
    "perl",
    "ruby",
    "node",
    "bun",
    "deno",
    "php",
    "lua",
    "tclsh",
    "pwsh",
)
INDIRECT_OPTIONS: Final[frozenset[str]] = frozenset({"--exec", "--eval"})
FIND_EXECUTION: Final[frozenset[str]] = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir"}
)
ANCHOR_SEPARATORS: Final[tuple[str, ...]] = (" ", "/", "=")


@dataclass(frozen=True)
class Scan:
    words: tuple[str, ...]
    first_quoted: bool
    complete: bool
    special: bool


@dataclass(frozen=True)
class Command:
    complete: bool
    indirection: bool
    allowable: bool
    canonical: str
    folded: str


def scanned(command: str) -> Scan:
    words: list[str] = []
    current: list[str] = []
    first_quoted = False
    in_word = False
    quote = ""
    special = has_control_character(command)
    for character in command:
        if quote == "'":
            if character == "'":
                quote = ""
            else:
                special = special or substitutes(current, character)
                current.append(character)
            continue
        if quote == '"':
            if character == '"':
                quote = ""
            else:
                special = (
                    special
                    or character in INSIDE_DOUBLE_QUOTES
                    or substitutes(current, character)
                )
                current.append(character)
            continue
        if character in QUOTES:
            quote = character
            first_quoted = first_quoted or not words
            in_word = True
            continue
        if character == " ":
            if in_word:
                words.append("".join(current))
            current, in_word = [], False
            continue
        special = special or character in OUTSIDE_QUOTES
        current.append(character)
        in_word = True
    if in_word:
        words.append("".join(current))
    return Scan(tuple(words), first_quoted, quote == "", special)


def substitutes(word: list[str], character: str) -> bool:
    return character == "`" or (
        character in SUBSTITUTION_OPENERS and bool(word) and word[-1] == "$"
    )


def basename(word: str) -> str:
    return word.rsplit("/", 1)[-1].casefold()


def first_word_indirect(text: str, quoted: bool) -> bool:
    name = basename(text)
    return (
        quoted
        or not text.isascii()
        or text.startswith("-")
        or ASSIGNMENT.match(text) is not None
        or name in FIRST_WORDS
        or SHELL_NAME.fullmatch(name) is not None
        or AWK_NAME.fullmatch(name) is not None
        or name.startswith(INTERPRETER_PREFIXES)
    )


def any_word_indirect(words: tuple[str, ...]) -> bool:
    if basename(words[0]) == "find" and any(word in FIND_EXECUTION for word in words):
        return True
    return any(
        word.partition("=")[0] in INDIRECT_OPTIONS
        or word.startswith("=")
        or OPTION_CLUSTER.fullmatch(word) is not None
        for word in words
    )


def parsed_command(command: str) -> Command:
    if len(command) > MAX_COMMAND_CHARACTERS:
        return Command(False, True, False, "", "")
    scan = scanned(command)
    words = scan.words
    canonical = " ".join(words)
    indirection = (
        not scan.complete
        or scan.special
        or not words
        or first_word_indirect(words[0], scan.first_quoted)
        or any_word_indirect(words)
    )
    allowable = (
        not indirection
        and "/" not in words[0]
        and not any(WHITESPACE.search(word) for word in words)
    )
    denied_form = " ".join(word for word in words if word)
    return Command(
        scan.complete, indirection, allowable, canonical, folded(denied_form)
    )


def anchor_at(text: str, position: int) -> bool:
    return position == 0 or text[position - 1] in ANCHOR_SEPARATORS


def earliest_anchor(pattern: StarPattern, text: str, fuel: Fuel) -> int | None:
    first = pattern.pieces[0]
    fuel.charge(CALL_CHARGE + PIECE_CHARGE + len(text) * needle_weight(len(first)))
    position = text.find(first)
    while position > 0 and not anchor_at(text, position):
        fuel.charge(CALL_CHARGE)
        position = text.find(first, position + 1)
    return None if position < 0 else position


def anchored_match(pattern: StarPattern, text: str, fuel: Fuel) -> bool:
    if pattern.literal:
        piece = pattern.pieces[0]
        fuel.charge(CALL_CHARGE + PIECE_CHARGE + len(piece))
        return text.endswith(piece) and anchor_at(text, len(text) - len(piece))
    anchor = earliest_anchor(pattern, text, fuel)
    return anchor is not None and star_match(pattern, text, fuel, anchor)


def deny_argument_matches(
    folded_argument: StarPattern,
    tail_free: StarPattern | None,
    command: Command,
    fuel: Fuel,
) -> bool:
    if command.indirection:
        return True
    if anchored_match(folded_argument, command.folded, fuel):
        return True
    return tail_free is not None and anchored_match(tail_free, command.folded, fuel)


def allow_argument_matches(
    argument: StarPattern | None, command: Command, fuel: Fuel
) -> bool:
    if not command.allowable:
        return False
    return argument is None or star_match(argument, command.canonical, fuel)


def mentions_patch(command: str) -> bool:
    lowered = command.casefold()
    return "apply_patch" in lowered or "applypatch" in lowered
