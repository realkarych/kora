from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kora.conformance.capture import Capture
from kora.ir.globs import (
    MAX_GLOB_NON_ASCII,
    MAX_PROTECTED_PATHS,
    Fuel,
    FuelExhausted,
    non_ascii_count,
)
from kora.plugins.policy.allow_deny import (
    AllowDenyPolicy,
    evaluated,
)
from kora.plugins.policy.allow_deny.arguments import MAX_PATCH_BYTES, ArgumentInvalid
from kora.plugins.policy.allow_deny.patterns import (
    MAX_ARRAY_BYTES,
    MAX_LAYER_NON_ASCII,
    MAX_LAYER_PIECES,
    MAX_PATTERN_CHARACTERS,
    MAX_PATTERNS,
)
from kora.plugins.policy.allow_deny.shell import MAX_COMMAND_CHARACTERS
from kora.protocols.policy import PolicyLayers, ToolCallRequest
from tests.plugins.policy.policy_support import (
    decision,
    patch_text,
    policy,
    prepared_or_raise,
    request,
)

NEEDLE: Final[str] = "a" * 92 + "baaa"
WIDE_PIECE: Final[str] = "\u1f82b"
ASTRAL: Final[str] = "\U0001f600"
SLOW_FOLD: Final[str] = "\u1fae"
DEFAULT_FIND_WINDOW: Final[int] = 2400
DEEP_SEGMENTS: Final[int] = 50
LINK_CHAIN: Final[int] = 40


@dataclass(frozen=True)
class PolicyCall:
    request: ToolCallRequest
    layers: PolicyLayers
    protected: tuple[str, ...]


@dataclass(frozen=True)
class Spend:
    units: int
    outcome: str


def call(root: Path, harness: str, tool: str, command: str) -> ToolCallRequest:
    return request(root, harness, tool, {"command": command})


def filled(pattern: Callable[[int], str]) -> tuple[str, ...]:
    patterns: list[str] = []
    size = 0
    while len(patterns) < MAX_PATTERNS:
        text = pattern(len(patterns))
        size += len(text.encode("utf-8"))
        if size > MAX_ARRAY_BYTES:
            break
        patterns.append(text)
    return tuple(patterns)


def both(allow: tuple[str, ...], deny: tuple[str, ...]) -> PolicyLayers:
    return PolicyLayers(namespace=policy(allow, deny), node=policy(allow, deny))


def patch_of(paths: Callable[[int], str]) -> str:
    headers: list[str] = []
    size = len(patch_text())
    while True:
        header = f"*** Add File: {paths(len(headers))}"
        size += len(header.encode("utf-8")) + 1
        if size > MAX_PATCH_BYTES:
            return patch_text(*headers)
        headers.append(header)


def realistic_shell(root: Path) -> PolicyCall:
    return realistic_command(root, MAX_COMMAND_CHARACTERS // 4)


def realistic_long_shell(root: Path) -> PolicyCall:
    return realistic_command(root, MAX_COMMAND_CHARACTERS)


def realistic_command(root: Path, length: int) -> PolicyCall:
    command = "git log " + " ".join(f"arg{index}" for index in range(length // 4))
    return PolicyCall(
        call(root, "codex", "Bash", command[:length]),
        both(
            tuple(f"Bash(tool{index} *)" for index in range(99)) + ("Bash(git *)",),
            tuple(f"Bash(danger{index} *)" for index in range(100)),
        ),
        tuple(f"gate{index}" for index in range(10)),
    )


def realistic_patch(root: Path) -> PolicyCall:
    text = patch_text(*[f"*** Add File: src/module{index}.py" for index in range(64)])
    return PolicyCall(
        call(root, "codex", "apply_patch", text),
        both(
            ("Edit(src/**)",), tuple(f"Edit(vendor{index}/**)" for index in range(100))
        ),
        tuple(f"gate{index}" for index in range(10)),
    )


def shell_allows(pattern: str) -> tuple[str, ...]:
    return filled(lambda index: pattern)


def short_shell_denies(root: Path) -> PolicyCall:
    return PolicyCall(
        call(root, "codex", "Bash", " ".join(["a"] * (MAX_COMMAND_CHARACTERS // 2))),
        both(shell_allows("Bash(a*c*a)"), filled(lambda index: "Bash(a*b*a)")),
        (),
    )


def needle_denies(root: Path, length: int) -> PolicyCall:
    return PolicyCall(
        call(root, "codex", "Bash", "a" * length),
        both(
            shell_allows(f"Bash(a*{NEEDLE}*a)"),
            filled(lambda index: f"Bash(a*{NEEDLE}*a)"),
        ),
        (),
    )


def long_shell_denies(root: Path) -> PolicyCall:
    return needle_denies(root, MAX_COMMAND_CHARACTERS)


def short_needles(root: Path) -> PolicyCall:
    return needle_denies(root, DEFAULT_FIND_WINDOW)


def edit_paths(root: Path) -> PolicyCall:
    text = patch_of(lambda index: f"src/{'a' * 48}{index}.py")
    star_dense = "Edit(" + "*a" * 30 + "*z)"
    return PolicyCall(
        call(root, "codex", "apply_patch", text),
        both(
            filled(lambda index: "Edit(docs/*)" if index else "Edit(src/*)"),
            filled(lambda index: star_dense),
        ),
        (),
    )


def many_short_paths(root: Path) -> PolicyCall:
    return PolicyCall(
        call(root, "codex", "apply_patch", patch_of(lambda index: f"p{index}")),
        both((), ()),
        ("docs",),
    )


def protected_globs(root: Path) -> PolicyCall:
    segments = "/".join(["a"] * 200)
    glob = "**/" + "/".join(["*a*a*"] * 30) + "/b"
    return PolicyCall(
        call(
            root, "codex", "apply_patch", patch_of(lambda index: f"{segments}/f{index}")
        ),
        both((), ()),
        tuple([glob] * 64),
    )


def deep_root(root: Path) -> str:
    deep = root
    for _ in range(DEEP_SEGMENTS):
        deep = deep / "dddddddddddddddd"
    deep.mkdir(parents=True, exist_ok=True)
    previous = root / "hop0"
    previous.symlink_to(deep)
    for index in range(1, LINK_CHAIN):
        current = root / f"hop{index}"
        current.symlink_to(previous)
        previous = current
    return previous.name


def deep_resolution(root: Path) -> PolicyCall:
    entry = deep_root(root)
    return PolicyCall(
        call(root, "codex", "apply_patch", patch_of(lambda index: f"{entry}/f{index}")),
        both((), ()),
        ("docs",),
    )


def shell_allow_needles(root: Path) -> PolicyCall:
    return PolicyCall(
        call(root, "codex", "Bash", "a" * MAX_COMMAND_CHARACTERS),
        both(shell_allows(f"Bash(a*{NEEDLE}*a)"), filled(lambda index: "Bash(z*)")),
        (),
    )


def non_ascii_paths(root: Path) -> PolicyCall:
    return PolicyCall(
        call(
            root,
            "codex",
            "apply_patch",
            patch_of(lambda index: "/".join(["\u0390" * 100] * 3) + str(index)),
        ),
        both((), ()),
        ("docs",),
    )


def short_needle_paths(root: Path) -> PolicyCall:
    pattern = f"Edit(x*{NEEDLE}*a)"
    return PolicyCall(
        call(
            root,
            "codex",
            "apply_patch",
            patch_of(lambda index: f"x/{index}/" + "/".join(["a" * 199] * 12)),
        ),
        both(filled(lambda index: pattern), filled(lambda index: pattern)),
        (),
    )


def piece_dense_edit_denies(root: Path) -> PolicyCall:
    dense = "Edit(" + "*a" * ((MAX_PATTERN_CHARACTERS - 10) // 2) + "*b*)"
    denies = (dense,) * (MAX_LAYER_PIECES // (dense.count("*") + 1))
    return PolicyCall(
        call(
            root,
            "codex",
            "apply_patch",
            patch_of(lambda index: f"{index}/" + "/".join(["a" * 200] * 19)),
        ),
        both((), denies),
        (),
    )


def piece_dense_globs(root: Path) -> PolicyCall:
    segments = "/".join(["a" * 60] * 40)
    glob = "**/" + "/".join(["*a" * 29 + "*"] * 4) + "/b"
    return PolicyCall(
        call(
            root, "codex", "apply_patch", patch_of(lambda index: f"{segments}/f{index}")
        ),
        both((), ()),
        tuple([glob] * 64),
    )


def anchored_first_pieces(root: Path) -> PolicyCall:
    first = "a " * 47 + "b"
    return PolicyCall(
        call(root, "codex", "Bash", ("a " * (MAX_COMMAND_CHARACTERS // 2)).rstrip()),
        both((), filled(lambda index: f"Bash({first}*z)")),
        (),
    )


def anchor_spaces(root: Path) -> PolicyCall:
    first = " " * 91 + "b"
    command = "x '" + " " * (MAX_COMMAND_CHARACTERS - 4) + "'"
    return PolicyCall(
        call(root, "codex", "Bash", command),
        both((), filled(lambda index: f"Bash({first}  *z)")),
        (),
    )


def anchor_periodic(root: Path) -> PolicyCall:
    first = ("a " * 48)[:93] + "aa"
    return PolicyCall(
        call(root, "codex", "Bash", ("a " * (MAX_COMMAND_CHARACTERS // 2)).rstrip()),
        both((), filled(lambda index: f"Bash({first}*z)")),
        (),
    )


def anchor_occurrences(root: Path) -> PolicyCall:
    return PolicyCall(
        call(root, "codex", "Bash", "b" + "a" * (MAX_COMMAND_CHARACTERS - 1)),
        both((), filled(lambda index: "Bash(a*z)")),
        (),
    )


def two_piece_glob_runs(root: Path) -> PolicyCall:
    segments = "/".join(["aaa"] * 120)
    glob = "**/" + "/".join(["a*a"] * 30) + "/zz"
    return PolicyCall(
        call(
            root, "codex", "apply_patch", patch_of(lambda index: f"{segments}/f{index}")
        ),
        both((), ()),
        tuple(f"{glob}{index}" for index in range(MAX_PROTECTED_PATHS)),
    )


def literal_glob_runs(root: Path) -> PolicyCall:
    segments = "/".join(["a"] * 200)
    glob = "**/" + "/".join(["a"] * 30) + "/zz"
    return PolicyCall(
        call(
            root, "codex", "apply_patch", patch_of(lambda index: f"{segments}/f{index}")
        ),
        both((), ()),
        tuple(f"{glob}{index}" for index in range(MAX_PROTECTED_PATHS)),
    )


def composite_array(tag: str) -> tuple[str, ...]:
    unit = "*" + "A" * 12 + ASTRAL
    body = unit * ((MAX_PATTERN_CHARACTERS - 16) // len(unit))
    patterns: list[str] = []
    size = pieces = wide = 0
    while len(patterns) < MAX_PATTERNS:
        text = f"Bash({tag}{len(patterns):03d}" + body + " *)"
        size += len(text.encode("utf-8"))
        pieces += text.count("*") + 1
        wide += non_ascii_count((text,))
        if (
            size > MAX_ARRAY_BYTES
            or pieces > MAX_LAYER_PIECES // 2
            or wide > MAX_LAYER_NON_ASCII // 2
        ):
            break
        patterns.append(text)
    return tuple(patterns)


def composite_variant(root: Path, variant: int = 0) -> PolicyCall:
    root.mkdir(parents=True, exist_ok=True)
    glob = "/".join([("*" + SLOW_FOLD + ASTRAL) * 30 + "*"] * 4)
    return PolicyCall(
        call(
            root,
            "codex",
            "apply_patch",
            patch_of(lambda index: f"src/{'a' * 48}{index}.py"),
        ),
        PolicyLayers(
            namespace=policy(
                composite_array(f"v{variant}w"), composite_array(f"v{variant}x")
            ),
            node=policy(
                composite_array(f"v{variant}y"), composite_array(f"v{variant}z")
            ),
        ),
        tuple(f"{glob}{index}" for index in range(MAX_PROTECTED_PATHS)),
    )


def fold_patterns() -> tuple[str, ...]:
    return tuple(
        f"Bash(f{index}" + SLOW_FOLD * (MAX_PATTERN_CHARACTERS - 8) + ")"
        for index in range(2)
    )


def fold_globs() -> tuple[str, ...]:
    return tuple(
        f"g{index}/" + SLOW_FOLD * (MAX_GLOB_NON_ASCII // MAX_PROTECTED_PATHS)
        for index in range(MAX_PROTECTED_PATHS)
    )


def fold_dense(root: Path) -> PolicyCall:
    return PolicyCall(
        call(root, "codex", "Bash", "ls"),
        both(fold_patterns(), fold_patterns()),
        fold_globs(),
    )


def fold_dense_anchors(root: Path) -> PolicyCall:
    occurrences = anchor_occurrences(root)
    return PolicyCall(
        occurrences.request,
        both(fold_patterns(), occurrences.layers.node.deny),
        fold_globs(),
    )


def fold_dense_wide_command(root: Path) -> PolicyCall:
    command = "x '" + SLOW_FOLD * (MAX_COMMAND_CHARACTERS - 4) + "'"
    return PolicyCall(
        call(root, "codex", "Bash", command),
        both(fold_patterns(), filled(lambda index: "Bash(\u03b9*z)")),
        fold_globs(),
    )


ROWS: Final[dict[str, Callable[[Path], PolicyCall]]] = {
    "policy_shell_short_denies": short_shell_denies,
    "policy_shell_long_denies": long_shell_denies,
    "policy_short_needles": short_needles,
    "policy_edit_paths": edit_paths,
    "policy_many_short_paths": many_short_paths,
    "policy_protected_globs": protected_globs,
    "policy_deep_resolution": deep_resolution,
    "policy_non_ascii_paths": non_ascii_paths,
    "policy_short_needle_paths": short_needle_paths,
    "policy_composite": composite_variant,
    "policy_shell_allow_needles": shell_allow_needles,
    "policy_piece_dense_edit_denies": piece_dense_edit_denies,
    "policy_piece_dense_globs": piece_dense_globs,
    "policy_anchored_first_pieces": anchored_first_pieces,
    "policy_anchor_spaces": anchor_spaces,
    "policy_anchor_periodic": anchor_periodic,
    "policy_anchor_occurrences": anchor_occurrences,
    "policy_two_piece_glob_runs": two_piece_glob_runs,
    "policy_literal_glob_runs": literal_glob_runs,
    "policy_fold_dense": fold_dense,
    "policy_fold_dense_anchors": fold_dense_anchors,
    "policy_fold_dense_wide_command": fold_dense_wide_command,
}
DECIDED_ROWS: Final[frozenset[str]] = frozenset(
    {"policy_shell_short_denies", "policy_non_ascii_paths", "policy_fold_dense"}
)
FLOOR_SHARE: Final[float] = 1 / 3
FLOORED: Final[frozenset[str]] = frozenset(
    {"policy_realistic_shell", "policy_realistic_patch"}
)
REALISTIC: Final[dict[str, Callable[[Path], PolicyCall]]] = {
    "policy_realistic_shell": realistic_shell,
    "policy_realistic_long_shell": realistic_long_shell,
    "policy_realistic_patch": realistic_patch,
}


def spend(subject: PolicyCall, budget: int) -> Spend:
    fuel = Fuel(budget)
    try:
        evaluated(
            subject.request, prepared_or_raise(subject.layers, subject.protected), fuel
        )
    except FuelExhausted:
        return Spend(fuel.spent, "exhausted")
    except ArgumentInvalid:
        return Spend(fuel.spent, "refused")
    return Spend(fuel.spent, "decided")


def decide(subject: PolicyCall, capture: Capture, provider: AllowDenyPolicy) -> bool:
    found = decision(
        capture, subject.request, subject.layers, subject.protected, provider
    )
    return found.allow
