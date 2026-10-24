import asyncio
import dataclasses
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Final, cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import (
    MARKER,
    check,
    context_for,
    fail,
    kora_capture,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.ir import ContractViolation, ToolPolicy
from kora.ir.json import JsonObject, JsonValue
from kora.protocols.policy import (
    RULE_ID,
    Policy,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
    is_allowing_reason,
    is_reason,
)

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_policy_allow_list_restricts_in_either_layer",
    "test_policy_allows_what_no_rule_refuses",
    "test_policy_decisions_are_deterministic",
    "test_policy_deny_in_either_layer_denies",
    "test_policy_protected_path_is_denied",
    "test_policy_validate_refuses_a_malformed_policy",
    "test_provider_logs_through_its_context",
]

PROTECTED_SETS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("tests/**",), "tests/unit/case.py"),
    (("config/*.lock",), "config/kora.lock"),
    (("docs/**", "Makefile", "tests/**"), "tests/unit/case.py"),
    (("config/*.lock", "tests/**"), "tests/unit/case.py"),
    (("Makefile",), "Makefile"),
    (("docs/**", "config/kora.toml"), "config/kora.toml"),
)
PROTECTIONS: Final[tuple[tuple[str, ...], ...]] = (
    (),
    *(globs for globs, _ in PROTECTED_SETS),
)
UNPROTECTED_PATH: Final[str] = "src/case.py"
EDIT_TOOLS: Final[dict[str, frozenset[str]]] = {
    "claude": frozenset({"Edit", "Write", "NotebookEdit"}),
    "codex": frozenset({"apply_patch"}),
}
MARKER_ARGUMENT: Final[str] = "kora_conformance_marker"
LAYERS: Final[tuple[str, str]] = ("namespace", "node")
CODE_MARKER: Final[str] = MARKER.replace("-", "_")
Decision = tuple[ToolCallRequest, PolicyLayers, tuple[str, ...]]


@dataclass(frozen=True)
class EditCall:
    tool: str
    pattern: str
    build: Callable[[str], ToolCallRequest]
    naming: Callable[[str], str]


@dataclass(frozen=True)
class PolicyCalls:
    shell: ToolCallRequest
    shell_other: ToolCallRequest
    matching: str
    narrow: str
    other: str
    edits: tuple[EditCall, ...]
    malformed: JsonObject


@pytest.fixture
def kora_provider(kora_policy_provider: Policy) -> Policy:
    return kora_policy_provider


@pytest.fixture
def kora_exercise(kora_policy_exercise: object) -> object:
    return kora_policy_exercise


def marked(request: ToolCallRequest) -> ToolCallRequest:
    arguments = {**request.arguments, MARKER_ARGUMENT: CODE_MARKER}
    return dataclasses.replace(request, arguments=arguments)


def variants(request: ToolCallRequest) -> Iterator[ToolCallRequest]:
    for agent_id in (None, "conformance-agent"):
        for tool_use_id, node_path in (
            ("conformance-call-1", request.node_path),
            ("conformance-call-2", f"conformance-parent/{request.node_path}"),
        ):
            yield marked(
                dataclasses.replace(
                    request,
                    agent_id=agent_id,
                    tool_use_id=tool_use_id,
                    node_path=node_path,
                )
            )


def policy(allow: tuple[str, ...] = (), deny: tuple[str, ...] = ()) -> ToolPolicy:
    return ToolPolicy.model_validate({"allow": list(allow), "deny": list(deny)})


def layered(layer: str, chosen: ToolPolicy, other: ToolPolicy) -> PolicyLayers:
    if layer == "namespace":
        return PolicyLayers(namespace=chosen, node=other)
    return PolicyLayers(namespace=other, node=chosen)


def violations(value: object) -> list[ContractViolation]:
    if type(value) is not list:
        fail("validate returns ContractViolations")
    items = cast("list[object]", value)
    found = [item for item in items if type(item) is ContractViolation]
    if len(found) != len(items):
        fail("validate returns ContractViolations")
    return found


def cites_a_pattern(reason: str, layers: PolicyLayers) -> bool:
    if RULE_ID.fullmatch(reason) is None:
        return True
    layer, kind, index = reason.split(".")
    chosen = layers.namespace if layer == "namespace" else layers.node
    patterns = chosen.allow if kind == "allow" else chosen.deny
    return int(index) < len(patterns)


def checked(value: object, layers: PolicyLayers) -> PolicyDecision:
    if type(value) is not PolicyDecision:
        fail("decide returns a PolicyDecision")
    check(type(value.allow) is bool, "allow is an exact bool")
    check(is_reason(value.reason), "the reason is a rule id or a policy reason")
    check(
        is_allowing_reason(value.reason) == value.allow,
        "the reason agrees with the decision",
    )
    check(cites_a_pattern(value.reason, layers), "a rule id cites an existing pattern")
    check(CODE_MARKER not in value.reason, "the reason echoes nothing")
    return value


def decided(
    provider: Policy, capture: Capture, calls: Iterable[Decision]
) -> list[PolicyDecision]:
    context = context_for(capture)
    with asyncio.Runner() as runner:
        return [
            checked(
                runner.run(provider.decide(request, layers, protected, context)),
                layers,
            )
            for request, layers, protected in calls
        ]


def edit_requests(
    calls: PolicyCalls, path: str
) -> Iterator[tuple[EditCall, ToolCallRequest]]:
    for edit in calls.edits:
        for request in variants(edit.build(path)):
            yield edit, request


def texts(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from texts(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from texts(item)


@pytest.mark.kora_conformance
def test_policy_validate_refuses_a_malformed_policy(
    kora_policy_provider: Policy, kora_policy_calls: PolicyCalls
) -> None:
    document = kora_policy_calls.malformed

    found = violations(kora_policy_provider.validate(document))

    check(bool(found), "validate refuses the malformed policy")
    for violation in found:
        check(violation.path is not None, "each violation has a path")
        rendered = json.dumps(violation.model_dump(mode="json"))
        check(
            all(text not in rendered for text in texts(document) if len(text) > 3),
            "no violation carries the policy's text",
        )


def deny_placements(
    other: str, matching: str
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    placements: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
        ((other, matching), ()),
        ((matching, other), ()),
        ((other, matching, other), ()),
        ((other, other, matching), ()),
    ]
    return placements + [((other, matching), globs) for globs in PROTECTIONS[1:]]


@dataclass(frozen=True)
class RuleTarget:
    request: ToolCallRequest
    matching: str
    narrow: str


def rule_targets(calls: PolicyCalls) -> Iterator[RuleTarget]:
    for request in variants(calls.shell):
        yield RuleTarget(request, calls.matching, calls.narrow)
    for edit, request in edit_requests(calls, UNPROTECTED_PATH):
        yield RuleTarget(request, edit.pattern, edit.naming(UNPROTECTED_PATH))


def placed_denies(calls: PolicyCalls, layer: str) -> Iterator[tuple[Decision, int]]:
    for target in rule_targets(calls):
        for denies, protected in deny_placements(calls.other, target.matching):
            for same in ((), (target.matching,)):
                for other in ((), (target.matching,)):
                    layers = layered(
                        layer, policy(allow=same, deny=denies), policy(allow=other)
                    )
                    yield (
                        (target.request, layers, protected),
                        denies.index(target.matching),
                    )


def contested_denies(calls: PolicyCalls, layer: str) -> Iterator[tuple[Decision, int]]:
    for target in rule_targets(calls):
        denies = (calls.other, target.matching)
        for chosen, other in (
            (policy(deny=denies), policy(deny=(calls.other,))),
            (policy(deny=denies), policy(allow=(target.narrow,))),
            (policy(allow=(target.narrow,), deny=denies), policy()),
        ):
            yield (target.request, layered(layer, chosen, other), ()), 1


@pytest.mark.kora_conformance
@pytest.mark.parametrize("layer", LAYERS)
def test_policy_deny_in_either_layer_denies(
    kora_policy_provider: Policy,
    kora_policy_calls: PolicyCalls,
    kora_capture: Capture,
    layer: str,
) -> None:
    calls = kora_policy_calls
    cited = [
        *placed_denies(calls, layer),
        *contested_denies(calls, layer),
    ]

    decisions = decided(kora_policy_provider, kora_capture, [call for call, _ in cited])

    check(all(not decision.allow for decision in decisions), "a matching deny denies")
    check(
        all(
            decision.reason in (f"{layer}.deny.{index}", "denied")
            for decision, (_, index) in zip(decisions, cited, strict=True)
        ),
        "a deny cites the matching rule",
    )


@pytest.mark.kora_conformance
@pytest.mark.parametrize("layer", LAYERS)
def test_policy_allow_list_restricts_in_either_layer(
    kora_policy_provider: Policy,
    kora_policy_calls: PolicyCalls,
    kora_capture: Capture,
    layer: str,
) -> None:
    calls = kora_policy_calls
    targets = list(rule_targets(calls))
    grid = [
        (
            target.request,
            layered(layer, policy(allow=(calls.other,)), policy(allow=other)),
            protected,
        )
        for target in targets
        for other in ((), (target.matching,))
        for protected in PROTECTIONS
    ] + [
        (
            target.request,
            layered(layer, policy(allow=(calls.other, calls.other)), policy()),
            (),
        )
        for target in targets
    ]

    decisions = decided(kora_policy_provider, kora_capture, grid)

    check(
        all(not decision.allow for decision in decisions),
        "an allow list that does not match denies",
    )


@pytest.mark.kora_conformance
def test_policy_allows_what_no_rule_refuses(
    kora_policy_provider: Policy, kora_policy_calls: PolicyCalls, kora_capture: Capture
) -> None:
    calls = kora_policy_calls
    grid = [
        (target.request, chosen, protected)
        for target in rule_targets(calls)
        for chosen in (
            PolicyLayers(namespace=policy(), node=policy()),
            PolicyLayers(
                namespace=policy(allow=(target.matching,)),
                node=policy(allow=(target.matching,)),
            ),
            PolicyLayers(
                namespace=policy(allow=(target.narrow,)),
                node=policy(allow=(target.narrow,)),
            ),
            PolicyLayers(
                namespace=policy(deny=(calls.other,)), node=policy(deny=(calls.other,))
            ),
        )
        for protected, _ in PROTECTED_SETS
    ]

    decisions = decided(kora_policy_provider, kora_capture, grid)

    check(
        all(decision.allow for decision in decisions),
        "a call no rule refuses is allowed",
    )


def path_forms(root: str, path: str) -> tuple[str, ...]:
    trimmed = root.rstrip("/")
    name = trimmed.rsplit("/", 1)[-1]
    head, separator, tail = path.partition("/")
    return (
        path,
        f"{trimmed}/{path}",
        f"./{path}",
        f"{head}/./{tail}" if separator else f"{trimmed}/./{path}",
        f"src/../{path}",
        f"{trimmed}//{path}",
        f"{trimmed}/../{name}/{path}",
        f"../{name}/{path}",
    )


def protected_requests(edit: EditCall, path: str) -> Iterator[ToolCallRequest]:
    root = edit.build(path).workspace.root
    for form in path_forms(root, path):
        yield from variants(edit.build(form))


@pytest.mark.kora_conformance
def test_policy_protected_path_is_denied(
    kora_policy_provider: Policy, kora_policy_calls: PolicyCalls, kora_capture: Capture
) -> None:
    calls = kora_policy_calls
    built = [(edit, edit.build(UNPROTECTED_PATH)) for edit in calls.edits]
    harnesses = {request.harness for _, request in built}
    covered = {
        harness: {request.tool for _, request in built if request.harness == harness}
        for harness in harnesses
    }
    grid = [
        (request, PolicyLayers(namespace=namespace, node=node), protected)
        for edit in calls.edits
        for protected, path in PROTECTED_SETS
        for namespace, node in (
            (policy(), policy()),
            (policy(deny=(calls.other,)), policy()),
            (policy(allow=(edit.pattern,)), policy(allow=(edit.pattern,))),
        )
        for request in protected_requests(edit, path)
    ] + [
        (
            request,
            PolicyLayers(
                namespace=policy(allow=(edit.naming(path),)),
                node=policy(allow=(edit.naming(path),)),
            ),
            protected,
        )
        for edit in calls.edits
        for protected, path in PROTECTED_SETS
        for request in variants(edit.build(path))
    ]

    check(bool(calls.edits), "the adopter supplies at least one file-edit call")
    check(
        all(request.tool == edit.tool for edit, request in built),
        "each edit builder makes a call of its own tool",
    )
    check(
        all(harness in EDIT_TOOLS for harness in harnesses),
        "every named harness is in the coverage matrix",
    )
    check(
        all(EDIT_TOOLS[harness] <= covered[harness] for harness in harnesses),
        "every file-edit tool of each named harness is covered",
    )
    decisions = decided(kora_policy_provider, kora_capture, grid)
    check(
        all(not decision.allow for decision in decisions), "a protected path is denied"
    )
    check(
        all(RULE_ID.fullmatch(decision.reason) is None for decision in decisions),
        "a protected path is not attributed to a rule",
    )


def expected_decisions(calls: PolicyCalls) -> list[tuple[Decision, bool]]:
    empty = PolicyLayers(namespace=policy(), node=policy())
    denying = PolicyLayers(namespace=policy(deny=(calls.matching,)), node=policy())
    expected: list[tuple[Decision, bool]] = []
    for denied, allowed in zip(
        variants(calls.shell), variants(calls.shell_other), strict=True
    ):
        expected += [((denied, denying, ()), False), ((allowed, denying, ()), True)]
    for edit in calls.edits:
        edit_deny = PolicyLayers(namespace=policy(deny=(edit.pattern,)), node=policy())
        for plain in variants(edit.build(UNPROTECTED_PATH)):
            expected += [((plain, edit_deny, ()), False), ((plain, empty, ()), True)]
        for globs, path in PROTECTED_SETS:
            for guarded, plain in zip(
                variants(edit.build(path)),
                variants(edit.build(UNPROTECTED_PATH)),
                strict=True,
            ):
                expected += [
                    ((guarded, empty, ()), True),
                    ((guarded, empty, globs), False),
                    ((plain, empty, globs), True),
                ]
    return expected


@pytest.mark.kora_conformance
def test_policy_decisions_are_deterministic(
    kora_policy_provider: Policy, kora_policy_calls: PolicyCalls, kora_capture: Capture
) -> None:
    expected = [
        entry for _ in range(3) for entry in expected_decisions(kora_policy_calls)
    ]
    base = len(expected) // 3

    decisions = decided(
        kora_policy_provider, kora_capture, [call for call, _ in expected]
    )

    check(
        [decision.allow for decision in decisions] == [allow for _, allow in expected],
        "interleaved requests keep their own decisions",
    )
    check(
        all(
            decision.reason == decisions[index % base].reason
            for index, decision in enumerate(decisions)
        ),
        "identical requests give identical reasons",
    )
