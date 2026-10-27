import re
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.policy.allow_deny.patterns import MAX_PATTERNS
from kora.protocols.policy import PolicyLayers, ToolCallRequest
from tests.plugins.policy.policy_support import (
    debug_capture,
    decision,
    edit,
    layers,
    policy,
    prepared_root,
    reason,
    refusal,
    request,
    shell,
)

__all__ = ["kora_capture"]

INDIRECTION: Final[list[str]] = [
    "ls a#;rm -rf x",
    "ls; rm x",
    "echo x >> f",
    "cat <<< x",
    "a 2>&1",
    "a &> f",
    "a |& b",
    "echo $HOME",
    "echo `id`",
    'echo "$x"',
    "a\nb",
    "a\tb",
    "/bin/r? -rf x",
    "/bin/r[m] -rf x",
    "=rm x",
    "FOO=1 rm x",
    "FOO+=1 bash x.sh",
    "\\rm x",
    "'rm' x",
    "/bin/sh x",
    "python3.12 x.py",
    "BASH x.sh",
    "find . -exec rm {} ;",
    "git commit -c x",
    "x --exec=y",
    "echo 'unterminated",
    "x" * 16_385,
    "",
    "ls =rm",
    "unshare -r bash -lc 'r\"\"m -rf x'",
    "unshare -r perl -E 'system 1'",
    "grep -ie x f",
    "find . -okdir rm x ';'",
    "find . -execdir rm -rf x ';'",
    "find . -ok rm x ';'",
    "x { y",
    "x } y",
    "ls ~/x",
    'echo "a!b"',
    "-rm x",
    "\u00e9cho x",
    "x --exec y",
    "x --eval y",
    "x -e y",
    "x 'a[$(rm v)]'",
    "x 'a[`rm -rf v`]'",
    "x 'path[${(e)${:-${(#):-36}(rm -rf v)}}]'",
    "x '$''(rm -rf v)'",
    "x 'a[$'\"(rm -rf v)]\"",
    "x '$'\"{(e)x}\"",
    "/bin/rm^x -rf v",
    "gawk 'BEGIN{system(1)}'",
    "mawk x",
    "nawk x",
]
UNTOUCHED: Final[list[tuple[str, str]]] = [
    ("sed 's/$//' f", "Bash(sed *)"),
    ("grep '^x$' f", "Bash(grep *)"),
    ("jq '$x' f", "Bash(jq *)"),
    ("git log --format='%H'", "Bash(git log *)"),
]
SHELL_PATCHES: Final[list[str]] = [
    "cd x && apply_patch <<'EOF'\n*** Begin Patch\n*** End Patch\nEOF",
    "APPLY_PATCH <<'EOF'",
    "echo applypatch",
    "x" * 16_385,
    "echo 'unterminated",
]


DENYING: Final[list[tuple[PolicyLayers, str]]] = [
    (layers(node=policy(deny=("Bash(zz-none)",))), "node.deny.0"),
    (layers(namespace=policy(deny=("Bash(zz-none)",))), "namespace.deny.0"),
    (layers(node=policy(deny=("B*(zz-none)",))), "node.deny.0"),
]
EVERY_DENY: Final[list[str]] = [expected for _, expected in DENYING]
ALLOWS: Final[tuple[str, ...]] = ("Bash", "*", "Bash(*)", "B*")


def denied_everywhere(capture: Capture, call: ToolCallRequest) -> list[str]:
    return [reason(capture, call, denying) for denying, _ in DENYING]


def allowed_anywhere(capture: Capture, call: ToolCallRequest) -> list[bool]:
    return [
        decision(capture, call, chosen, protected).allow
        for allow in ALLOWS
        for chosen in (
            layers(node=policy(allow=(allow,))),
            layers(namespace=policy(allow=(allow,))),
        )
        for protected in ((), ("tests",))
    ]


@pytest.mark.parametrize("command", INDIRECTION, ids=range(len(INDIRECTION)))
def test_tool_policy_layers_indirection_matches_every_deny_and_no_allow(
    kora_capture: Capture, tmp_path: Path, command: str
) -> None:
    call = shell(tmp_path, command)

    denied = denied_everywhere(kora_capture, call)
    allowed = allowed_anywhere(kora_capture, call)

    assert denied == EVERY_DENY
    assert allowed == [False] * len(allowed)


@pytest.mark.parametrize("command", SHELL_PATCHES, ids=range(len(SHELL_PATCHES)))
def test_tool_policy_layers_refuses_shell_patches_under_the_shell_condition(
    kora_capture: Capture, tmp_path: Path, command: str
) -> None:
    claude = shell(tmp_path, command)
    codex = shell(tmp_path, command, harness="codex")

    protected = reason(kora_capture, claude, protected=("tests",))
    edit_rule = reason(
        kora_capture, codex, layers(node=policy(deny=("apply_patch(src/*)",)))
    )
    unguarded = reason(kora_capture, claude)

    assert (protected, edit_rule, unguarded) == (
        "argument_invalid",
        "argument_invalid",
        "allowed",
    )


def test_tool_policy_layers_reason_precedence(
    kora_capture: Capture, tmp_path: Path
) -> None:
    root = prepared_root(tmp_path)
    outside = edit(root, "Edit", "/etc/hosts")
    protected_edit = edit(root, "Edit", "tests/unit/case.py")
    git = shell(root, "git status")

    reasons = [
        reason(kora_capture, outside, protected=("**",)),
        reason(
            kora_capture,
            protected_edit,
            layers(node=policy(deny=("Edit",))),
            ("tests",),
        ),
        reason(
            kora_capture,
            git,
            layers(policy(deny=("Bash(git *)",)), policy(deny=("Bash(git *)",))),
        ),
        reason(
            kora_capture,
            git,
            layers(node=policy(deny=("Bash(zz)", "Bash(git *)", "Bash(git*)"))),
        ),
        reason(
            kora_capture,
            git,
            layers(policy(allow=("Read",)), policy(deny=("Bash(git *)",))),
        ),
        reason(kora_capture, git, layers(policy(allow=("Read",)), policy())),
        reason(kora_capture, git, layers(policy(), policy(allow=("Read",)))),
        reason(kora_capture, git, layers(policy(allow=("Bash(git *)",)), policy())),
    ]

    assert reasons == [
        "argument_invalid",
        "protected_path",
        "namespace.deny.0",
        "node.deny.1",
        "node.deny.0",
        "not_allowed",
        "not_allowed",
        "allowed",
    ]


def test_tool_policy_layers_raise_in_order(
    kora_capture: Capture, tmp_path: Path
) -> None:
    oversized = policy(allow=tuple(f"Read{index}" for index in range(MAX_PATTERNS + 1)))
    pieced = policy(deny=("Bash(" + "*a" * 2040 + ")",) * 5)
    folding = policy(deny=("Bash(" + "\u1f82" * 4000 + ")",) * 5)
    invalid_layer = layers(node=policy(allow=("Bash(",)))
    git = shell(tmp_path, "git status")
    unknown = request(tmp_path, "unknown-harness", "Bash", {"command": "ls"})

    codes = [
        refusal(kora_capture, unknown, invalid_layer, ("/abs",)),
        refusal(kora_capture, git, invalid_layer, ("/abs",)),
        refusal(kora_capture, git, layers(node=oversized)),
        refusal(kora_capture, git, layers(node=pieced)),
        refusal(kora_capture, git, layers(namespace=folding)),
        refusal(kora_capture, git, layers(node=policy(deny=("Write(x\u00e1*)",)))),
        refusal(kora_capture, git, protected=("/abs",)),
        refusal(kora_capture, git, protected=tuple(f"d{index}" for index in range(65))),
    ]

    assert codes == [
        "policy_harness_unsupported",
        "tool_policy_invalid",
        "tool_policy_invalid",
        "tool_policy_invalid",
        "tool_policy_invalid",
        "tool_policy_invalid",
        "protected_paths_invalid",
        "protected_paths_invalid",
    ]


def test_tool_policy_layers_log_one_content_free_record_per_outcome(
    tmp_path: Path,
) -> None:
    marker = "kora-marker-in-the-command"
    git = shell(tmp_path, f"git {marker}")
    denying = layers(node=policy(deny=("Bash(git *)",)))
    unknown = request(tmp_path, "unknown-harness", "Bash", {"command": marker})

    with debug_capture() as capture:
        decision(capture, git)
        decision(capture, git, denying)
        refusal(capture, unknown)
        refusal(capture, git, protected=("/abs",))
        refusal(capture, git, layers(node=policy(allow=(f"Bash({marker}",))))
        records = capture.finish()

    events = [
        (
            record.get("event"),
            record.get("reason"),
            record.get("cause"),
            record.get("code"),
        )
        for record in records
        if str(record.get("event", "")).startswith("policy.allow_deny.")
    ]
    assert events == [
        ("policy.allow_deny.allowed", "allowed", None, None),
        ("policy.allow_deny.denied", "node.deny.0", "rule", None),
        ("policy.allow_deny.refused", None, None, "policy_harness_unsupported"),
        ("policy.allow_deny.refused", None, None, "protected_paths_invalid"),
        ("policy.allow_deny.refused", None, None, "tool_policy_invalid"),
    ]
    assert all(marker not in str(record) for record in records)


def test_tool_policy_layers_empty_words_do_not_split_a_deny(
    kora_capture: Capture, tmp_path: Path
) -> None:
    split = shell(tmp_path, "rm '' -rf y")
    message = shell(tmp_path, "git commit -m ''")

    denied = reason(kora_capture, split, layers(node=policy(deny=("Bash(rm -rf *)",))))
    allowed = decision(
        kora_capture, message, layers(node=policy(allow=("Bash(git commit *)",)))
    )

    assert denied == "node.deny.0"
    assert allowed.allow


@pytest.mark.parametrize(("command", "allow"), UNTOUCHED, ids=range(len(UNTOUCHED)))
def test_tool_policy_layers_leave_ordinary_quoted_dollars_allowed(
    kora_capture: Capture, tmp_path: Path, command: str, allow: str
) -> None:
    call = shell(tmp_path, command)

    allowed = decision(kora_capture, call, layers(node=policy(allow=(allow,))))

    assert allowed.allow


SPECIFICATION: Final[Path] = (
    Path(__file__).parents[3] / "knowledge" / "specs" / "protocols.md"
)
FIRST_WORD_BULLET: Final[str] = "- a first word whose case-folded basename is a shell"


def listed_first_words() -> list[str]:
    lines = SPECIFICATION.read_text(encoding="utf-8").splitlines()
    bullet = next(line for line in lines if line.startswith(FIRST_WORD_BULLET))
    names = re.findall(r"`([^`]+)`", bullet)
    return [name for name in names if not (len(name) == 1 and name.isalpha())]


LISTED_FIRST_WORDS: Final[list[str]] = listed_first_words()


def test_tool_policy_layers_read_the_listed_first_words() -> None:
    names = LISTED_FIRST_WORDS

    found = set(names)

    assert len(names) > 90
    assert {"bash", "let", "env", "if", "python"} <= found


@pytest.mark.parametrize("name", LISTED_FIRST_WORDS)
def test_tool_policy_layers_listed_first_words_are_indirection(
    kora_capture: Capture, tmp_path: Path, name: str
) -> None:
    call = shell(tmp_path, f"{name} x")

    denied = denied_everywhere(kora_capture, call)
    bare = reason(kora_capture, call, layers(node=policy(allow=("Bash",))))
    allowed = allowed_anywhere(kora_capture, call)

    assert denied == EVERY_DENY
    assert bare == "not_allowed"
    assert allowed == [False] * len(allowed)
