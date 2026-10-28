import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, cast

from kora.cel.diagnostics import DiagnosticError
from kora.cel.meter import Meter
from kora.cel.paths import Field, JsonValue
from kora.cel.render import render_string
from kora.cel.reserved import RESERVED_IDENTIFIERS
from kora.cel.tokens import TokenKind, lex

IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EVERY_TEXT: Final[str] = "[*]"


class Read(Enum):
    WHOLE = "whole"
    PRESENCE = "presence"


@dataclass(frozen=True)
class Every:
    pass


EVERY: Final[Every] = Every()
ProjectionStep = Field | Every
ProjectionPath = tuple[ProjectionStep, ...]


class PathSetError(ValueError):
    pass


def is_plain_name(name: str) -> bool:
    if IDENTIFIER.fullmatch(name) is None or name in RESERVED_IDENTIFIERS:
        return False
    tokens = lex(name, Meter())
    return len(tokens) == 2 and tokens[0].kind is TokenKind.IDENTIFIER


def render_step(step: ProjectionStep, first: bool) -> str:
    if isinstance(step, Every):
        return EVERY_TEXT
    if is_plain_name(step.name):
        return step.name if first else f".{step.name}"
    return f"[{render_string(step.name)}]"


def render_path(path: ProjectionPath) -> str:
    return "".join(render_step(step, index == 0) for index, step in enumerate(path))


def parse_rendered_path(text: str) -> ProjectionPath:
    try:
        tokens = lex(text, Meter())
    except DiagnosticError as error:
        raise PathSetError("a projection path is not canonical") from error
    steps: list[ProjectionStep] = []
    position = 0
    while tokens[position].kind is not TokenKind.END:
        token = tokens[position]
        if token.kind is TokenKind.IDENTIFIER and position == 0:
            steps.append(Field(str(token.value)))
            position += 1
        elif token.kind is TokenKind.DOT:
            name = tokens[position + 1]
            if name.kind is not TokenKind.IDENTIFIER:
                raise PathSetError("a projection path is not canonical")
            steps.append(Field(str(name.value)))
            position += 2
        elif token.kind is TokenKind.LEFT_BRACKET:
            inner = tokens[position + 1]
            closing = tokens[position + 2] if position + 2 < len(tokens) else inner
            if closing.kind is not TokenKind.RIGHT_BRACKET:
                raise PathSetError("a projection path is not canonical")
            if inner.kind is TokenKind.STAR:
                steps.append(EVERY)
            elif inner.kind is TokenKind.STRING and isinstance(inner.value, str):
                steps.append(Field(inner.value))
            else:
                raise PathSetError("a projection path is not canonical")
            position += 3
        else:
            raise PathSetError("a projection path is not canonical")
    path = tuple(steps)
    if render_path(path) != text:
        raise PathSetError("a projection path is not canonical")
    return path


def sorted_paths(paths: Iterable[ProjectionPath]) -> tuple[ProjectionPath, ...]:
    return tuple(sorted(set(paths), key=render_path))


@dataclass(frozen=True)
class PathSet:
    whole: tuple[ProjectionPath, ...] = ()
    presence: tuple[ProjectionPath, ...] = ()

    def rendered(self) -> dict[str, list[str]]:
        return {
            "whole": [render_path(path) for path in self.whole],
            "presence": [render_path(path) for path in self.presence],
        }


def parse_path_set(whole: list[str], presence: list[str]) -> PathSet:
    path_set = PathSet(
        tuple(parse_rendered_path(text) for text in whole),
        tuple(parse_rendered_path(text) for text in presence),
    )
    if path_set.rendered() != {"whole": whole, "presence": presence}:
        raise PathSetError("a projection path set is not canonical")
    if normalized(path_set) != path_set:
        raise PathSetError("a projection path set is not canonical")
    return path_set


def new_children() -> dict[ProjectionStep, "Trie"]:
    return {}


@dataclass
class Trie:
    whole: bool = False
    presence: bool = False
    children: dict[ProjectionStep, "Trie"] = field(default_factory=new_children)

    def at(self, path: ProjectionPath) -> "Trie":
        node = self
        for step in path:
            node = node.children.setdefault(step, Trie())
        return node

    def add(self, path: ProjectionPath, read: Read) -> None:
        node = self.at(path)
        if read is Read.WHOLE:
            node.whole = True
        else:
            node.presence = True


def build_trie(path_set: PathSet) -> Trie:
    root = Trie()
    for path in path_set.whole:
        root.add(path, Read.WHOLE)
    for path in path_set.presence:
        root.add(path, Read.PRESENCE)
    return root


def frozen_paths(root: Trie) -> PathSet:
    whole: list[ProjectionPath] = []
    presence: list[ProjectionPath] = []
    pending: list[tuple[ProjectionPath, Trie]] = [((), root)]
    while pending:
        path, node = pending.pop()
        if node.whole:
            whole.append(path)
        elif node.children:
            pending.extend(
                (path + (step,), child) for step, child in node.children.items()
            )
        elif node.presence:
            presence.append(path)
    return PathSet(sorted_paths(whole), sorted_paths(presence))


def normalized(path_set: PathSet) -> PathSet:
    return frozen_paths(build_trie(path_set))


def path_sets(reads: Mapping[str, Mapping[ProjectionPath, Read]]) -> dict[str, PathSet]:
    result: dict[str, PathSet] = {}
    for name, paths in reads.items():
        root = Trie()
        for path, read in paths.items():
            root.add(path, read)
        result[name] = frozen_paths(root)
    return result


def emptied(value: JsonValue) -> JsonValue:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return ""
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        return 0.0
    return [] if isinstance(value, list) else {}


Slot = tuple[list[JsonValue] | dict[str, JsonValue], int | str]


def cut(value: JsonValue, path_set: PathSet, meter: Meter | None = None) -> JsonValue:
    used = meter if meter is not None else Meter()
    holder: list[JsonValue] = [None]
    pending: list[tuple[JsonValue, Trie, Slot]] = [
        (value, build_trie(path_set), (holder, 0))
    ]
    while pending:
        source, node, (target, key) = pending.pop()
        used.tick()
        projected = project_node(source, node, pending)
        if isinstance(target, list):
            target[cast(int, key)] = projected
        else:
            target[cast(str, key)] = projected
    return holder[0]


def project_node(
    source: JsonValue,
    node: Trie,
    pending: list[tuple[JsonValue, Trie, Slot]],
) -> JsonValue:
    if node.whole:
        return source
    if not node.children:
        return emptied(source)
    every = node.children.get(EVERY)
    if isinstance(source, list):
        if every is None:
            return []
        elements: list[JsonValue] = [None] * len(source)
        for index, element in enumerate(source):
            pending.append((element, every, (elements, index)))
        return elements
    if isinstance(source, dict):
        members: dict[str, JsonValue] = {}
        for step, child in node.children.items():
            if isinstance(step, Field) and step.name in source:
                members[step.name] = None
                pending.append((source[step.name], child, (members, step.name)))
        return members
    return emptied(source)
