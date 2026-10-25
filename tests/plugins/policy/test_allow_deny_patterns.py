import json
import os
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.conformance.policy import protected_requests
from kora.ir.globs import Fuel
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.plugins.policy.allow_deny.arguments import (
    ArgumentInvalid,
    edited_path,
    patch_paths,
    workspace_root,
)
from kora.plugins.policy.allow_deny.patterns import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTICS,
    MAX_PATTERNS,
)
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from tests.plugins.policy.policy_support import (
    conformance_calls,
    debug_capture,
    decision,
    edit,
    fuel_for,
    layers,
    patch_text,
    policy,
    prepared_root,
    reason,
    refusal,
    request,
    shell,
    update_patch,
)

__all__ = ["kora_capture"]

REFUSED_PATTERNS: Final[list[tuple[str, str]]] = [
    ("", "the tool pattern is malformed"),
    ("Bash(", "the tool pattern is malformed"),
    ("Bash()", "the tool pattern is malformed"),
    ("Bash(a(b))", "the tool pattern is malformed"),
    ("Ba sh", "the tool pattern is malformed"),
    ("Bash(npm run:*)", "the tool pattern uses a harness form kora does not support"),
    ("Edit(//abs)", "the tool pattern uses a harness form kora does not support"),
    ("Read(~/x)", "the tool pattern uses a harness form kora does not support"),
    ("Edit(./src/**)", "the file-edit pattern argument is not a relative path pattern"),
    ("Edit(../x)", "the file-edit pattern argument is not a relative path pattern"),
    ("Edit(src/)", "the file-edit pattern argument is not a relative path pattern"),
    ("Edit(src//a)", "the file-edit pattern argument is not a relative path pattern"),
    ("Edit(/abs)", "the file-edit pattern argument is not a relative path pattern"),
    ("Edit(~x)", "the file-edit pattern argument is not a relative path pattern"),
    (
        "NotebookEdit(/abs)",
        "the file-edit pattern argument is not a relative path pattern",
    ),
    ("Bash(" + "a" * 4096 + ")", "the tool pattern is too long"),
    ("Bash(*" + "a" * 97 + "*)", "a wildcard piece of the tool pattern is too long"),
    ("Edit(" + "a" * 97 + "*)", "a wildcard piece of the tool pattern is too long"),
    (
        "Bash(*" + "\u00df" * 49 + "*)",
        "a wildcard piece of the tool pattern is too long",
    ),
    ("mcp__a*" + "b" * 100, "a wildcard piece of the tool pattern is too long"),
    (
        "Write(gates/x\u00e1*)",
        "a wildcard of the deny pattern touches a combining mark",
    ),
    (
        "Bash(rm *\u0301x)",
        "a wildcard of the deny pattern touches a combining mark",
    ),
    (
        "Write(gates/\u1fb3*)",
        "a wildcard of the deny pattern touches a combining mark",
    ),
]
ACCEPTED_PATTERNS: Final[list[str]] = [
    "Bash",
    "Bash(git *)",
    "mcp__my-server",
    "mcp__my-server__query",
    "E*(src/**)",
    "*(/bin/rm *)",
    "Edit(src/**)",
    "apply_patch(src/*)",
    "Write(gates/x\u00e1*)",
]


def violations(document: JsonObject) -> list[tuple[object, str]]:
    found = AllowDenyPolicy().validate(document)
    return [(item.path, item.message) for item in found if item.code == "IR013"]


@pytest.mark.parametrize(("pattern", "message"), REFUSED_PATTERNS)
def test_validate_refuses_each_pattern_form(pattern: str, message: str) -> None:
    document: JsonObject = {"deny": ["Read", pattern]}

    found = violations(document)

    assert found == [(("deny", 1), message)]


def test_validate_accepts_supported_patterns() -> None:
    document: JsonObject = {"allow": list(ACCEPTED_PATTERNS), "x-other": {"a": 1}}

    found = violations(document)

    assert found == []


def test_validate_bounds_arrays_and_documents() -> None:
    full: JsonObject = {"allow": [f"Read{index}" for index in range(MAX_PATTERNS)]}
    crowded: JsonObject = {
        "allow": [f"Read{index}" for index in range(MAX_PATTERNS + 1)]
    }
    heavy_patterns: list[JsonValue] = ["R" + "a" * 4000] * 17
    heavy: JsonObject = {"deny": heavy_patterns}
    shaped: JsonObject = {"allow": [7]}

    results = [
        violations(full),
        violations(crowded),
        violations(heavy),
        violations(shaped),
    ]

    assert results == [
        [],
        [(("allow",), "the pattern array is too large")],
        [(("deny",), "the pattern array is too large")],
        [(None, "the document is not a tool policy")],
    ]


def test_validate_stays_within_the_diagnostics_bound() -> None:
    malformed: list[JsonValue] = ["("] * MAX_PATTERNS
    document: JsonObject = {"allow": malformed, "deny": malformed}

    found = AllowDenyPolicy().validate(document)

    rendered = json.dumps([item.model_dump(mode="json") for item in found])
    assert len(found) == 2 * MAX_PATTERNS
    assert len(found) <= MAX_DIAGNOSTICS
    assert len(rendered.encode("utf-8")) <= MAX_DIAGNOSTIC_BYTES


def test_a_match_at_the_last_index_names_it(
    kora_capture: Capture, tmp_path: Path
) -> None:
    deny = tuple(f"Bash(zz{index})" for index in range(MAX_PATTERNS - 1)) + (
        "Bash(git *)",
    )

    found = reason(
        kora_capture, shell(tmp_path, "git status"), layers(node=policy(deny=deny))
    )

    assert found == f"node.deny.{MAX_PATTERNS - 1}"


def test_patterns_match_literally_and_ignore_extra_keys(
    kora_capture: Capture, tmp_path: Path
) -> None:
    call = request(
        tmp_path, "claude", "Bash", {"command": "ls", "timeout": 5, "x": "y"}
    )

    reasons = [
        reason(kora_capture, call, layers(node=policy(allow=("Bash(ls)",)))),
        reason(kora_capture, call, layers(node=policy(allow=("Bash(l)",)))),
        reason(kora_capture, call, layers(node=policy(deny=("Bash",)))),
    ]

    assert reasons == ["allowed", "not_allowed", "node.deny.0"]


def test_argument_patterns_on_tools_without_extraction_fail_closed(
    kora_capture: Capture, tmp_path: Path
) -> None:
    read = request(tmp_path, "claude", "Read", {"file_path": "x"})
    tool = request(tmp_path, "claude", "mcp__fs__read", {"path": "x"})

    reasons = [
        reason(kora_capture, read, layers(node=policy(deny=("Read(src/*)",)))),
        reason(kora_capture, read, layers(node=policy(allow=("Read(*)",)))),
        reason(kora_capture, tool, layers(node=policy(allow=("mcp__fs",)))),
        reason(kora_capture, tool, layers(node=policy(deny=("mcp__fs__read(x)",)))),
    ]

    assert reasons == ["node.deny.0", "not_allowed", "allowed", "node.deny.0"]


@pytest.mark.parametrize(
    "tool", ["Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"]
)
@pytest.mark.parametrize(
    "deny", ["Edit(src/*)", "E*(src/*)", "Write(src/*)", "*(src/*)"]
)
def test_the_edit_family_is_denied_together(
    kora_capture: Capture, tmp_path: Path, tool: str, deny: str
) -> None:
    root = prepared_root(tmp_path)

    found = reason(
        kora_capture, edit(root, tool, "src/a.py"), layers(node=policy(deny=(deny,)))
    )

    assert found == "node.deny.0"


def test_edit_allows_and_single_tool_patterns(
    kora_capture: Capture, tmp_path: Path
) -> None:
    root = prepared_root(tmp_path)
    two_paths = request(
        root,
        "codex",
        "apply_patch",
        {
            "command": patch_text(
                "*** Add File: src/a.py", "+x", "*** Add File: docs/b.md", "+y"
            )
        },
    )

    reasons = [
        reason(
            kora_capture,
            edit(root, "Write", "src/a.py"),
            layers(node=policy(allow=("E*(src/*)",))),
        ),
        reason(
            kora_capture,
            edit(root, "Edit", "src/a.py"),
            layers(node=policy(allow=("E*(src/*)",))),
        ),
        reason(kora_capture, two_paths, layers(node=policy(allow=("Edit(src/**)",)))),
        reason(
            kora_capture,
            two_paths,
            layers(node=policy(allow=("Edit(src/**)", "Edit(docs/*)"))),
        ),
        reason(
            kora_capture, two_paths, layers(node=policy(deny=("apply_patch(docs/*)",)))
        ),
        reason(
            kora_capture,
            edit(root, "Edit", "src/a.py"),
            layers(node=policy(deny=("apply_patch(src/*)",))),
        ),
        reason(
            kora_capture,
            edit(root, "Edit", "tests/a.py"),
            layers(node=policy(deny=("Edit(tests)",))),
        ),
        reason(
            kora_capture,
            edit(root, "Edit", "tests/a.py"),
            layers(node=policy(deny=("E*(/abs/x)",))),
        ),
    ]

    assert reasons == [
        "not_allowed",
        "allowed",
        "not_allowed",
        "allowed",
        "node.deny.0",
        "allowed",
        "allowed",
        "allowed",
    ]


def test_new_files_follow_the_rules(kora_capture: Capture, tmp_path: Path) -> None:
    root = prepared_root(tmp_path)
    creation = patch_text("*** Add File: new/dir/a.py", "+x")
    add = request(root, "codex", "apply_patch", {"command": creation})
    protected_add = request(
        root,
        "codex",
        "apply_patch",
        {"command": patch_text("*** Add File: src/new/a.py", "+x")},
    )

    reasons = [
        reason(kora_capture, edit(root, "Write", "new/dir/a.py")),
        reason(
            kora_capture,
            edit(root, "Write", "src/new/a.py"),
            layers(node=policy(allow=("Edit(src/**)",))),
        ),
        reason(
            kora_capture, edit(root, "Write", "src/new/a.py"), protected=("src/**",)
        ),
        reason(kora_capture, add, protected=("docs",)),
        reason(kora_capture, protected_add, protected=("src/**",)),
        reason(kora_capture, edit(root, "Write", "new/../../x"), protected=("docs",)),
    ]

    assert reasons == [
        "allowed",
        "allowed",
        "protected_path",
        "allowed",
        "protected_path",
        "argument_invalid",
    ]


def test_the_workspace_boundary(kora_capture: Capture, tmp_path: Path) -> None:
    real = tmp_path / "real"
    prepared_root(real)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    outside = tmp_path / "outside"
    outside.mkdir()
    (real / "src" / "escape").symlink_to(outside)
    resolved_spelling = str(real.resolve() / "src" / "a.py")

    reasons = [
        reason(kora_capture, edit(alias, "Edit", "/etc/hosts"), protected=("docs",)),
        reason(kora_capture, edit(alias, "Edit", "/etc/hosts")),
        reason(
            kora_capture,
            edit(alias, "Edit", str(alias / "src" / "a.py")),
            protected=("docs",),
        ),
        reason(
            kora_capture, edit(alias, "Edit", resolved_spelling), protected=("docs",)
        ),
        reason(
            kora_capture, edit(alias, "Edit", "src/escape/a.py"), protected=("docs",)
        ),
    ]

    assert reasons == [
        "argument_invalid",
        "allowed",
        "allowed",
        "allowed",
        "argument_invalid",
    ]


def test_resolution_follows_the_kernel(kora_capture: Capture, tmp_path: Path) -> None:
    root = prepared_root(tmp_path)
    (root / "src" / "sub").mkdir()
    (root / "link").symlink_to(root / "tests" / "unit")
    (root / "dangling").symlink_to(root / "tests" / "new.sh")
    (root / "café").mkdir()
    (root / "secrets").mkdir()
    (root / "key").symlink_to(root / "secrets" / "key")
    (root / "src" / "escape").symlink_to(root / "secrets")
    (root / "into").symlink_to(root / "src")
    previous = root / "chain0"
    previous.symlink_to(root / "src")
    for index in range(1, 41):
        current = root / f"chain{index}"
        current.symlink_to(previous)
        previous = current

    reasons = [
        reason(
            kora_capture, edit(root, "Edit", "link/../gate.sh"), protected=("tests/**",)
        ),
        reason(kora_capture, edit(root, "Write", "dangling"), protected=("tests/**",)),
        reason(kora_capture, edit(root, "Edit", "café/x"), protected=("café",)),
        reason(kora_capture, edit(root, "Edit", "chain39/a.py"), protected=("docs",)),
        reason(kora_capture, edit(root, "Edit", "chain40/a.py"), protected=("docs",)),
        reason(kora_capture, edit(root, "Edit", "TESTS/a.py"), protected=("tests",)),
        reason(
            kora_capture,
            edit(root, "Edit", "TESTS/a.py"),
            layers(node=policy(allow=("Edit(tests/*)",))),
        ),
        reason(
            kora_capture,
            edit(root, "Edit", "link/../key"),
            layers(node=policy(deny=("Edit(secrets/*)",))),
        ),
        reason(
            kora_capture,
            edit(root, "Edit", "key"),
            layers(node=policy(deny=("Edit(secrets/*)",))),
        ),
        reason(
            kora_capture,
            edit(root, "Edit", "src/escape/key"),
            layers(node=policy(allow=("Edit(src/*)",))),
        ),
        reason(
            kora_capture,
            edit(root, "Edit", "into/a.py"),
            layers(node=policy(allow=("Edit(src/*)",))),
        ),
        reason(
            kora_capture, edit(root, "Write", "newdir/../a.py"), protected=("docs",)
        ),
    ]

    assert reasons == [
        "argument_invalid",
        "protected_path",
        "protected_path",
        "allowed",
        "argument_invalid",
        "protected_path",
        "not_allowed",
        "argument_invalid",
        "node.deny.0",
        "not_allowed",
        "not_allowed",
        "argument_invalid",
    ]


def test_a_decomposed_name_resolves_to_its_target(
    kora_capture: Capture, tmp_path: Path
) -> None:
    root = prepared_root(tmp_path)
    (root / "cafe\u0301").symlink_to(root / "tests")

    found = reason(
        kora_capture, edit(root, "Edit", "cafe\u0301/a.py"), protected=("tests",)
    )

    assert found == "protected_path"


def test_hard_links_are_refused_while_paths_are_checked(
    kora_capture: Capture, tmp_path: Path
) -> None:
    root = prepared_root(tmp_path)
    gate = root / "tests" / "gate.sh"
    gate.write_text("x")
    os.link(gate, root / "src" / "linked.sh")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("x")
    os.link(outside, root / "src" / "outer.txt")

    reasons = [
        reason(kora_capture, edit(root, "Edit", "src/linked.sh"), protected=("tests",)),
        reason(
            kora_capture,
            edit(root, "Edit", "src/linked.sh"),
            layers(node=policy(deny=("Edit(secrets/*)",))),
        ),
        reason(kora_capture, edit(root, "Edit", "src/outer.txt"), protected=("docs",)),
        reason(kora_capture, edit(root, "Edit", "src/linked.sh")),
    ]

    assert reasons == [
        "argument_invalid",
        "argument_invalid",
        "argument_invalid",
        "allowed",
    ]


@pytest.mark.parametrize("path", ["~/x", "a\rb", "a\x00b", "a" * 4097, ""])
def test_unreadable_paths_are_refused(
    kora_capture: Capture, tmp_path: Path, path: str
) -> None:
    call = edit(prepared_root(tmp_path), "Edit", path)

    found = reason(kora_capture, call, protected=("docs",))

    assert found == "argument_invalid"


def test_conformance_protected_forms_reach_the_protected_rule(
    kora_capture: Capture, tmp_path: Path
) -> None:
    calls = conformance_calls(prepared_root(tmp_path))

    reasons = {
        reason(kora_capture, variant, protected=("tests/**",))
        for edit_call in calls.edits
        for variant in protected_requests(edit_call, "tests/unit/case.py")
    }

    assert reasons == {"protected_path"}


def test_filesystem_errors_leave_no_path_behind(tmp_path: Path) -> None:
    root = prepared_root(tmp_path)
    locked = root / "locked"
    locked.mkdir()
    (locked / "inner").mkdir()
    locked.chmod(0)
    marker_path = "locked/inner/secret-marker.txt"
    try:
        with debug_capture() as capture:
            found = reason(
                capture, edit(root, "Edit", marker_path), protected=("docs",)
            )
            records = capture.finish()
        with pytest.raises(ArgumentInvalid) as raised:
            edited_path(
                marker_path, workspace_root(str(root), Fuel(10**9)), Fuel(10**9)
            )
    finally:
        locked.chmod(0o700)

    assert found == "argument_invalid"
    assert all("secret-marker" not in json.dumps(record) for record in records)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def test_fuel_exhaustion_denies_before_any_later_outcome(tmp_path: Path) -> None:
    root = prepared_root(tmp_path)
    deep = root / "tests"
    for index in range(60):
        deep = deep / f"d{index}"
    deep.mkdir(parents=True)
    path = str(deep.relative_to(root) / "a.py")
    starved = AllowDenyPolicy(fuel_budget=20_000)
    allow_heavy = layers(
        node=policy(allow=tuple(f"Bash(zz{index}*)" for index in range(MAX_PATTERNS)))
    )

    with debug_capture() as capture:
        resolving = decision(
            capture, edit(root, "Edit", path), protected=("tests",), subject=starved
        )
        allowing = decision(
            capture, shell(root, "git status"), allow_heavy, subject=starved
        )
        records = capture.finish()

    denials = [
        (record.get("reason"), record.get("cause"))
        for record in records
        if record.get("event") == "policy.allow_deny.denied"
    ]
    assert (resolving.reason, allowing.reason) == (
        "argument_invalid",
        "argument_invalid",
    )
    assert denials == [("argument_invalid", "fuel")] * 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (update_patch("a.py"), ("a.py",)),
        (
            patch_text(
                "*** Add File: n.py",
                "+x",
                "*** Delete File: d.py",
                "*** Update File: u.py",
                "*** Move to: m.py",
                "@@ def f():",
                " context",
                "",
                "-old",
                "+new",
                "*** End of File",
            ),
            ("n.py", "d.py", "u.py", "m.py"),
        ),
        (update_patch("a.py").replace("\n", "\r\n"), ("a.py",)),
        ("<<'EOF'\n" + update_patch("a.py") + "\nEOF\n", ("a.py",)),
        ("  " + update_patch("a.py") + "  \n", ("a.py",)),
    ],
)
def test_patch_envelope_paths(text: str, expected: tuple[str, ...]) -> None:
    found = patch_paths(text)

    assert found == expected


@pytest.mark.parametrize(
    "text",
    [
        patch_text(),
        update_patch("a.py") + "\ntrailing",
        patch_text("*** Environment ID: other", "*** Update File: a.py"),
        patch_text("*** Add File: a.py", "not a plus line"),
        patch_text("*** Delete File: a.py", "+x"),
        patch_text("*** Unknown: a.py"),
        "*** Update File: a.py\n*** End Patch",
        patch_text("*** Add File: a.py", "+" + "x" * 65_536),
        patch_text(
            "*** Update File: a.py", "@@", "-x", "  *** Update File: b.py", "+y"
        ),
        patch_text("*** Add File: a.py", "+x", "\t*** Delete File: b.py"),
        "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y",
    ],
)
def test_patch_envelope_refusals(text: str) -> None:
    with pytest.raises(ArgumentInvalid):
        patch_paths(text)


def test_the_command_scanner_and_deny_anchors(
    kora_capture: Capture, tmp_path: Path
) -> None:
    allow_git = layers(node=policy(allow=("Bash(git *)",)))
    deny_rm = layers(node=policy(deny=("Bash(rm *)",)))
    push_deny = layers(node=policy(deny=("Bash(git push *)",)))

    reasons = [
        reason(
            kora_capture,
            shell(tmp_path, "git   status"),
            layers(node=policy(allow=("Bash(git status)",))),
        ),
        reason(kora_capture, shell(tmp_path, "git commit -m 'a b'"), allow_git),
        reason(kora_capture, shell(tmp_path, "git commit -m 'ab'"), allow_git),
        reason(
            kora_capture,
            shell(tmp_path, "./git status"),
            layers(node=policy(allow=("Bash(git status)",))),
        ),
        reason(kora_capture, shell(tmp_path, "/bin/rm -rf x"), deny_rm),
        reason(kora_capture, shell(tmp_path, "foo rm -rf x"), deny_rm),
        reason(kora_capture, shell(tmp_path, "RM -rf x"), deny_rm),
        reason(kora_capture, shell(tmp_path, "farm -rf x"), deny_rm),
        reason(kora_capture, shell(tmp_path, "git push"), push_deny),
        reason(kora_capture, shell(tmp_path, "git push origin +main:main"), push_deny),
        reason(
            kora_capture,
            shell(tmp_path, "git push origin +main:main"),
            layers(node=policy(allow=("Bash(git push *)",))),
        ),
        reason(
            kora_capture,
            shell(tmp_path, "a" * 16_384),
            layers(node=policy(allow=("Bash(a*)",))),
        ),
        reason(kora_capture, shell(tmp_path, "rg --pre=rm -rf x ."), deny_rm),
        reason(
            kora_capture,
            shell(tmp_path, "git push git push"),
            layers(node=policy(deny=("Bash(git push)",))),
        ),
        reason(
            kora_capture,
            shell(tmp_path, "ls", harness="codex"),
            layers(node=policy(allow=("Bash(ls)",))),
        ),
    ]

    assert reasons == [
        "allowed",
        "not_allowed",
        "allowed",
        "not_allowed",
        "node.deny.0",
        "node.deny.0",
        "node.deny.0",
        "allowed",
        "node.deny.0",
        "node.deny.0",
        "allowed",
        "allowed",
        "node.deny.0",
        "node.deny.0",
        "allowed",
    ]


def test_the_installed_entry_point_loads_allow_deny(kora_capture: Capture) -> None:
    metadata = load_provider(
        ProtocolName.POLICY, "allow_deny", kora_capture.diagnostics
    )

    created = metadata.implementation({})

    assert isinstance(created, AllowDenyPolicy)
    assert metadata.config_schema == {"type": "object", "additionalProperties": False}
    assert AllowDenyPolicy.config_schema["type"] == "object"
    assert AllowDenyPolicy.config_schema != metadata.config_schema
    with pytest.raises(ValidationError):
        metadata.implementation({"deny": ["Bash"]})


@pytest.mark.parametrize(
    "separator",
    ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r"],
)
def test_patch_headers_refuse_other_line_separators(
    kora_capture: Capture, tmp_path: Path, separator: str
) -> None:
    root = prepared_root(tmp_path)
    text = patch_text(f"*** Add File: x{separator}+/../src/a", "+pwned")
    call = request(root, "codex", "apply_patch", {"command": text})

    with pytest.raises(ArgumentInvalid):
        patch_paths(text)
    found = reason(kora_capture, call, protected=("src",))

    assert found == "argument_invalid"


def test_patch_content_lines_keep_their_control_characters() -> None:
    text = patch_text("*** Update File: a.py", "@@", "-\x0cold", "+\x0bnew\tline")

    found = patch_paths(text)

    assert found == ("a.py",)


def test_the_trailing_any_rule_follows_collapsed_stars(
    kora_capture: Capture, tmp_path: Path
) -> None:
    denying = layers(node=policy(deny=("Bash(git push **)",)))

    found = reason(kora_capture, shell(tmp_path, "git push"), denying)

    assert found == "node.deny.0"


def test_a_workspace_root_must_be_absolute_and_normalised(
    kora_capture: Capture, tmp_path: Path
) -> None:
    real = prepared_root(tmp_path / "real" / "ws")
    (tmp_path / "real" / "sub").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "link").symlink_to(tmp_path / "real" / "sub")
    (real / "t2").symlink_to(real / "tests")
    spelled = Path(str(tmp_path / "a" / "link") + "/../ws")
    relative_root = request(
        Path("relative/ws"), "claude", "Edit", {"file_path": "a.py"}
    )

    reasons = [
        reason(kora_capture, edit(spelled, "Write", "t2/a.py"), protected=("tests",)),
        reason(kora_capture, relative_root, protected=("tests",)),
    ]

    assert reasons == ["argument_invalid", "argument_invalid"]


def test_the_command_is_read_only_when_a_rule_needs_it(
    kora_capture: Capture, tmp_path: Path
) -> None:
    missing = request(tmp_path, "claude", "Bash", {"description": "x"})
    write = edit(prepared_root(tmp_path), "Write", "/etc/x")

    reasons = [
        reason(kora_capture, missing, layers(node=policy(deny=("Bash",)))),
        reason(kora_capture, missing, layers(node=policy(allow=("Read",)))),
        reason(kora_capture, missing, layers(node=policy(allow=("Bash(ls)",)))),
        reason(kora_capture, write, layers(node=policy(allow=("E*",)))),
    ]

    assert reasons == ["node.deny.0", "not_allowed", "argument_invalid", "not_allowed"]


def test_fuel_runs_out_inside_allow_evaluation(tmp_path: Path) -> None:
    allow = layers(node=policy(allow=("Bash(a*" + "*a" * 40 + "*b)",)))
    call = shell(tmp_path, " ".join(["a"] * 8000))
    needed = fuel_for(call, allow)

    with debug_capture() as capture:
        enough = decision(capture, call, allow, subject=AllowDenyPolicy(needed))
        short = decision(capture, call, allow, subject=AllowDenyPolicy(needed - 1))
        records = capture.finish()

    causes = [
        record.get("cause")
        for record in records
        if record.get("event") == "policy.allow_deny.denied"
    ]
    assert (enough.reason, short.reason) == ("not_allowed", "argument_invalid")
    assert causes == ["rule", "fuel"]


def test_prepared_configurations_are_bounded_and_reused(
    kora_capture: Capture, tmp_path: Path
) -> None:
    provider = AllowDenyPolicy()
    git = shell(tmp_path, "git status")
    chosen = [layers(node=policy(deny=(f"Bash(tool{index} *)",))) for index in range(6)]

    git_deny = policy(deny=("Bash(git *)",))
    placed = [layers(namespace=git_deny), layers(node=git_deny)]
    patch_route = shell(tmp_path, "cd x && apply_patch <<'EOF'", harness="codex")

    reasons = [
        decision(kora_capture, git, item, subject=provider).reason for item in chosen
    ]
    cached = list(provider.configurations.values())[-1]
    repeated = decision(kora_capture, git, chosen[-1], subject=provider).reason
    reused = list(provider.configurations.values())[-1] is cached
    by_layer = [
        decision(kora_capture, git, item, subject=provider).reason for item in placed
    ]
    guarded = [
        decision(kora_capture, patch_route, layers(), protected, provider).reason
        for protected in ((), ("tests",))
    ]

    assert reasons == ["allowed"] * 6
    assert repeated == "allowed"
    assert reused
    assert by_layer == ["namespace.deny.0", "node.deny.0"]
    assert guarded == ["allowed", "argument_invalid"]
    assert len(provider.configurations) == 4


@pytest.mark.parametrize("trailing", [" ", "\u00a0", "\u3000"])
def test_a_move_target_with_surrounding_space_is_refused(trailing: str) -> None:
    text = patch_text(
        "*** Update File: src/a.py",
        f"*** Move to: docs/a.md{trailing}",
        "@@",
        "-x",
        "+y",
    )

    with pytest.raises(ArgumentInvalid):
        patch_paths(text)


def test_missing_fields_and_empty_commands(
    kora_capture: Capture, tmp_path: Path
) -> None:
    root = prepared_root(tmp_path)
    missing_edit = request(root, "claude", "Edit", {"old_string": "x"})

    reasons = [
        reason(kora_capture, missing_edit, layers(node=policy(deny=("Edit",)))),
        reason(kora_capture, shell(root, ""), protected=("tests",)),
        reason(kora_capture, shell(root, ""), layers(node=policy(allow=("Bash",)))),
    ]

    assert reasons == ["argument_invalid", "allowed", "not_allowed"]


def test_validate_refuses_layers_past_their_piece_and_folding_bounds() -> None:
    dense: list[JsonValue] = ["Bash(" + "*a" * 1023 + ")"]
    wide: list[JsonValue] = ["Bash(" + "\u1f82" * 4000 + ")"]
    at_bound: JsonObject = {"deny": dense * 8}
    pieced: JsonObject = {"deny": dense * 9}
    folding: JsonObject = {"deny": wide * 5}

    results = [violations(at_bound), violations(pieced), violations(folding)]

    assert results == [
        [],
        [(None, "the tool policy has too many wildcard pieces")],
        [(None, "the tool policy has too many characters outside ASCII")],
    ]


def test_protected_globs_past_their_bounds_are_refused(
    kora_capture: Capture, tmp_path: Path
) -> None:
    call = shell(tmp_path, "git status")
    pieced = "/".join(["*a" * 60 + "*"] * 16)
    folding = "\u1f82" * 300
    long_ascii = "a" * 1100

    codes = [
        refusal(kora_capture, call, protected=(pieced,) * 9),
        refusal(kora_capture, call, protected=(folding,) * 64),
        refusal(kora_capture, call, protected=(long_ascii,) * 64),
        refusal(kora_capture, call, protected=("tests/\udc80",)),
    ]

    assert codes == ["protected_paths_invalid"] * 4


@pytest.mark.parametrize(
    "path",
    [" src/a.py", "src/a.py ", "\u00a0src/a.py", "\ufeffsrc/a.py", "src/a.py\u3000"],
)
def test_edit_paths_with_surrounding_white_space_are_refused(
    kora_capture: Capture, tmp_path: Path, path: str
) -> None:
    root = prepared_root(tmp_path)

    found = reason(kora_capture, edit(root, "Edit", path), protected=("src/**",))

    assert found == "argument_invalid"
