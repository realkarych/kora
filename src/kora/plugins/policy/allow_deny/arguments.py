import errno
import os
import stat
from collections import deque
from dataclasses import dataclass
from typing import Final

from kora.ir.globs import (
    Fuel,
    PathSubject,
    folded,
    folded_subject,
    has_control_character,
    normalised_segments,
)
from kora.protocols.policy import ToolCallRequest

MAX_PATH_CHARACTERS: Final[int] = 4096
MAX_LINKS: Final[int] = 40
MAX_PATCH_BYTES: Final[int] = 65_536
SYSTEM_CALL_CHARGE: Final[int] = 5120
COMPONENT_CHARGE: Final[int] = 56
PATH_CHARGE: Final[int] = 15_360
NON_ASCII_CHARGE: Final[int] = 192
PATH_COMPONENT_CHARGE: Final[int] = 640
MISSING: Final[frozenset[int]] = frozenset({errno.ENOENT, errno.ENOTDIR})
ARGUMENT_KEYS: Final[dict[tuple[str, str], str]] = {
    ("claude", "Bash"): "command",
    ("claude", "Edit"): "file_path",
    ("claude", "Write"): "file_path",
    ("claude", "MultiEdit"): "file_path",
    ("claude", "NotebookEdit"): "notebook_path",
    ("codex", "Bash"): "command",
    ("codex", "apply_patch"): "command",
}
PATCH_TOOLS: Final[frozenset[tuple[str, str]]] = frozenset({("codex", "apply_patch")})
BEGIN_PATCH: Final[str] = "*** Begin Patch"
END_PATCH: Final[str] = "*** End Patch"
END_OF_FILE: Final[str] = "*** End of File"
ADD_FILE: Final[str] = "*** Add File: "
DELETE_FILE: Final[str] = "*** Delete File: "
UPDATE_FILE: Final[str] = "*** Update File: "
MOVE_TO: Final[str] = "*** Move to: "
PATH_HEADERS: Final[tuple[str, ...]] = (ADD_FILE, DELETE_FILE, UPDATE_FILE, MOVE_TO)
WHITE_SPACE: Final[str] = (
    " \t\n\x0b\x0c\r\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
TRIMMED: Final[str] = WHITE_SPACE + "\ufeff"
SEPARATORS: Final[frozenset[str]] = frozenset("\u2028\u2029")
HUNK_PREFIXES: Final[tuple[str, ...]] = (" ", "+", "-", "@@")


class ArgumentInvalid(Exception):
    def __init__(self) -> None:
        super().__init__("the tool argument cannot be checked")


@dataclass(frozen=True)
class Root:
    lexical: str
    resolved: str


@dataclass(frozen=True)
class EditedPath:
    lexical: str
    resolved: str
    lexical_subject: PathSubject
    resolved_subject: PathSubject
    folded_lexical: str
    folded_resolved: str


def named_argument(request: ToolCallRequest) -> str:
    key = ARGUMENT_KEYS.get((request.harness, request.tool))
    value = None if key is None else request.arguments.get(key)
    if not isinstance(value, str):
        raise ArgumentInvalid
    return value


def encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def checked_text(path: str) -> str:
    if (
        not path
        or len(path) > MAX_PATH_CHARACTERS
        or path.startswith("~")
        or path != path.strip(TRIMMED)
        or has_control_character(path)
        or not encodable(path)
    ):
        raise ArgumentInvalid
    return path


def lexical(path: str) -> str:
    return "/" + "/".join(normalised_segments(path))


def inspected(path: str, fuel: Fuel, components: int) -> os.stat_result | None:
    fuel.charge(SYSTEM_CALL_CHARGE + COMPONENT_CHARGE * components)
    refused = False
    missing = False
    result: os.stat_result | None = None
    try:
        result = os.lstat(path)
    except OSError as error:
        missing = error.errno in MISSING
        refused = not missing
    if refused:
        raise ArgumentInvalid
    return result


def link_target(path: str, fuel: Fuel, components: int) -> str:
    fuel.charge(SYSTEM_CALL_CHARGE + COMPONENT_CHARGE * components)
    refused = False
    target = ""
    try:
        target = os.readlink(path)
    except OSError:
        refused = True
    if refused:
        raise ArgumentInvalid
    return target


@dataclass(frozen=True)
class Resolution:
    path: str
    final: os.stat_result | None


def resolved(path: str, fuel: Fuel, base: str = "/") -> Resolution:
    pending: deque[str] = deque(path.split("/"))
    parts: list[str] = [part for part in base.split("/") if part]
    links = 0
    missing = False
    final: os.stat_result | None = None
    while pending:
        part = pending.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            if missing:
                raise ArgumentInvalid
            if parts:
                parts.pop()
            final = None
            continue
        if missing:
            parts.append(part)
            continue
        candidate = "/" + "/".join([*parts, part])
        status = inspected(candidate, fuel, len(parts) + 1)
        if status is None:
            missing = True
            final = None
            parts.append(part)
            continue
        if stat.S_ISLNK(status.st_mode):
            links += 1
            if links > MAX_LINKS:
                raise ArgumentInvalid
            target = link_target(candidate, fuel, len(parts) + 1)
            if target.startswith("/"):
                parts = []
            pending.extendleft(reversed(target.split("/")))
            continue
        parts.append(part)
        final = status
    return Resolution("/" + "/".join(parts), final)


def within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def relative(path: str, root: str) -> str:
    if path == root:
        return ""
    return path[len(root) :].lstrip("/")


def workspace_root(root: str, fuel: Fuel) -> Root:
    if not root.startswith("/") or any(
        part in ("", ".", "..") for part in root[1:].split("/")
    ):
        raise ArgumentInvalid
    return Root(lexical(root), resolved(root, fuel).path)


def resolved_under(joined: str, root: Root, fuel: Fuel) -> Resolution:
    for prefix in sorted({root.lexical, root.resolved}, key=len, reverse=True):
        if prefix != "/" and (joined == prefix or joined.startswith(prefix + "/")):
            return resolved(joined[len(prefix) :], fuel, root.resolved)
    return resolved(joined, fuel)


def edited_path(raw: str, root: Root, fuel: Fuel) -> EditedPath:
    text = checked_text(raw)
    fuel.charge(
        PATH_CHARGE
        + len(text)
        + PATH_COMPONENT_CHARGE * text.count("/")
        + (0 if text.isascii() else NON_ASCII_CHARGE * len(text))
    )
    joined = text if text.startswith("/") else root.lexical + "/" + text
    lexical_path = lexical(joined)
    resolution = resolved_under(joined, root, fuel)
    if ".." in joined.split("/") and (
        resolved_under(lexical_path, root, fuel).path != resolution.path
    ):
        raise ArgumentInvalid
    lexical_root = max(
        (
            candidate
            for candidate in (root.lexical, root.resolved)
            if within(lexical_path, candidate)
        ),
        key=len,
        default=None,
    )
    if lexical_root is None or not within(resolution.path, root.resolved):
        raise ArgumentInvalid
    final = resolution.final
    if final is not None and stat.S_ISREG(final.st_mode) and final.st_nlink > 1:
        raise ArgumentInvalid
    lexical_relative = relative(lexical_path, lexical_root)
    resolved_relative = relative(resolution.path, root.resolved)
    folded_lexical = folded(lexical_relative)
    folded_resolved = (
        folded_lexical
        if resolved_relative == lexical_relative
        else folded(resolved_relative)
    )
    lexical_subject = folded_subject(folded_lexical)
    return EditedPath(
        lexical=lexical_relative,
        resolved=resolved_relative,
        lexical_subject=lexical_subject,
        resolved_subject=(
            lexical_subject
            if folded_resolved == folded_lexical
            else folded_subject(folded_resolved)
        ),
        folded_lexical=folded_lexical,
        folded_resolved=folded_resolved,
    )


def rust_lines(text: str) -> list[str]:
    return [line.removesuffix("\r") for line in text.split("\n")]


def trimmed(line: str) -> str:
    return line.strip(WHITE_SPACE)


def checked_marker(line: str) -> str:
    if has_control_character(line) or SEPARATORS.intersection(line):
        raise ArgumentInvalid
    return trimmed(line)


def unwrapped_lines(text: str) -> list[str]:
    lines = rust_lines(trimmed(text))
    if len(lines) >= 2 and trimmed(lines[0]).startswith("<<"):
        tag = checked_marker(lines[0])[2:].strip("'\"")
        if tag and checked_marker(lines[-1]) == tag:
            return lines[1:-1]
    return lines


def header_of(line: str) -> str | None:
    return next((header for header in PATH_HEADERS if line.startswith(header)), None)


def patch_paths(text: str) -> tuple[str, ...]:
    if len(text.encode("utf-8", "surrogatepass")) > MAX_PATCH_BYTES:
        raise ArgumentInvalid
    lines = unwrapped_lines(text)
    if (
        len(lines) < 2
        or checked_marker(lines[0]) != BEGIN_PATCH
        or checked_marker(lines[-1]) != END_PATCH
    ):
        raise ArgumentInvalid
    paths: list[str] = []
    section = ""
    for line in lines[1:-1]:
        if not trimmed(line).startswith("***"):
            if not hunk_line_allowed(section, line):
                raise ArgumentInvalid
            continue
        marker = checked_marker(line)
        header = header_of(marker)
        if header == MOVE_TO and marker != line:
            raise ArgumentInvalid
        if header is not None and not line.startswith("***"):
            raise ArgumentInvalid
        if header is None:
            if marker != END_OF_FILE or section != UPDATE_FILE:
                raise ArgumentInvalid
            continue
        paths.append(marker[len(header) :])
        if header != MOVE_TO:
            section = header
    if not paths:
        raise ArgumentInvalid
    return tuple(paths)


def hunk_line_allowed(section: str, line: str) -> bool:
    if section == ADD_FILE:
        return line.startswith("+")
    if section == UPDATE_FILE:
        return not trimmed(line) or line.startswith(HUNK_PREFIXES)
    return False


def request_paths(request: ToolCallRequest) -> tuple[str, ...]:
    argument = named_argument(request)
    if (request.harness, request.tool) in PATCH_TOOLS:
        return patch_paths(argument)
    return (argument,)
