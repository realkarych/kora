from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict

from kora.ir import ContractViolation
from kora.ir.globs import (
    Fuel,
    FuelExhausted,
    GlobInvalid,
    PathGlob,
    compile_globs,
    star_match,
)
from kora.ir.json import JsonObject, parse_json_object
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.plugins.policy.allow_deny.arguments import (
    ArgumentInvalid,
    EditedPath,
    edited_path,
    named_argument,
    request_paths,
    workspace_root,
)
from kora.plugins.policy.allow_deny.patterns import (
    FILE_EDIT_TOOLS,
    MAX_ARRAY_BYTES,
    MAX_PATTERN_CHARACTERS,
    MAX_PATTERNS,
    DiagnosticsLimit,
    Layer,
    LayerInvalid,
    ToolPattern,
    allow_governs,
    allow_governs_edits,
    deny_governs,
    deny_governs_edits,
    is_edit_tool,
    parsed_layer,
    validated,
)
from kora.plugins.policy.allow_deny.shell import (
    Command,
    allow_argument_matches,
    deny_argument_matches,
    mentions_patch,
    parsed_command,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.policy import (
    PolicyCapabilities,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)

VERSION: Final[str] = "1"
DEPLOYMENT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "additionalProperties": False,
}
DECISION_FUEL: Final[int] = 32_000_000
PREPARED_ENTRIES: Final[int] = 4
ConfigurationKey = tuple[
    tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]
]
SHELL_TOOL: Final[str] = "Bash"
REFUSALS: Final[dict[str, str]] = {
    "policy_harness_unsupported": "the policy does not support this harness",
    "tool_policy_invalid": "a tool policy layer is invalid",
    "protected_paths_invalid": "the protected paths are invalid",
    "diagnostics_limit": "the policy has too many violations",
}
COMPONENT: Final[str] = CATALOG.register_component("policy.allow_deny")
ALLOWED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "policy.allow_deny.allowed",
        COMPONENT,
        Level.DEBUG,
        "a tool call was allowed",
        {"reason": FieldKind.STRING},
    )
)
DENIED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "policy.allow_deny.denied",
        COMPONENT,
        Level.INFO,
        "a tool call was denied",
        {"reason": FieldKind.STRING, "cause": FieldKind.STRING},
    )
)
REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "policy.allow_deny.refused",
        COMPONENT,
        Level.WARNING,
        "a tool call could not be decided",
        {"code": FieldKind.STRING},
    )
)
PATTERN_ARRAY: Final[JsonObject] = {
    "type": "array",
    "maxItems": MAX_PATTERNS,
    "items": {"type": "string", "minLength": 1, "maxLength": MAX_PATTERN_CHARACTERS},
}
CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    {
        "type": "object",
        "properties": {"allow": PATTERN_ARRAY, "deny": PATTERN_ARRAY},
        "patternProperties": {"^x-": {}},
        "additionalProperties": False,
        "x-kora-array-bytes": MAX_ARRAY_BYTES,
    }
)


@dataclass(frozen=True)
class Outcome:
    allow: bool
    reason: str


@dataclass(frozen=True)
class Prepared:
    layers: tuple[Layer, Layer]
    globs: tuple[PathGlob, ...]


ALLOWED_OUTCOME: Final[Outcome] = Outcome(True, "allowed")
NOT_ALLOWED: Final[Outcome] = Outcome(False, "not_allowed")
ARGUMENT_INVALID: Final[Outcome] = Outcome(False, "argument_invalid")
PROTECTED: Final[Outcome] = Outcome(False, "protected_path")


def denied_by(layer: Layer, pattern: ToolPattern) -> Outcome:
    return Outcome(False, f"{layer.name}.deny.{pattern.index}")


def prepared_configuration(
    layers: PolicyLayers, protected_paths: tuple[str, ...]
) -> Prepared | str:
    try:
        parsed = (
            parsed_layer("namespace", layers.namespace),
            parsed_layer("node", layers.node),
        )
    except LayerInvalid:
        return "tool_policy_invalid"
    try:
        globs = compile_globs(protected_paths)
    except GlobInvalid:
        return "protected_paths_invalid"
    return Prepared(parsed, globs)


def configuration_key(
    layers: PolicyLayers, protected_paths: tuple[str, ...]
) -> ConfigurationKey:
    return (
        layers.namespace.allow,
        layers.namespace.deny,
        layers.node.allow,
        layers.node.deny,
        protected_paths,
    )


def by_name(
    request: ToolCallRequest, layers: tuple[Layer, Layer], fuel: Fuel
) -> Outcome:
    for layer in layers:
        for pattern in layer.deny:
            if deny_governs(pattern, request.harness, request.tool, fuel):
                return denied_by(layer, pattern)
    for layer in layers:
        if layer.allow and not any(
            pattern.argument is None
            and allow_governs(pattern, request.harness, request.tool, fuel)
            for pattern in layer.allow
        ):
            return NOT_ALLOWED
    return ALLOWED_OUTCOME


def shell_condition(request: ToolCallRequest, ready: Prepared, fuel: Fuel) -> bool:
    if ready.globs:
        return True
    return any(
        deny_governs_edits(pattern, request.harness, fuel)
        for layer in ready.layers
        for pattern in layer.deny
    ) or any(
        allow_governs_edits(pattern, request.harness, fuel)
        for layer in ready.layers
        for pattern in layer.allow
    )


def command_needed(request: ToolCallRequest, ready: Prepared, fuel: Fuel) -> bool:
    return any(
        allow_governs(pattern, request.harness, SHELL_TOOL, fuel)
        for layer in ready.layers
        for pattern in layer.allow
    ) or any(
        pattern.folded_argument is not None
        and deny_governs(pattern, request.harness, SHELL_TOOL, fuel)
        for layer in ready.layers
        for pattern in layer.deny
    )


def shell_denial(
    command: Command, ready: Prepared, harness: str, fuel: Fuel
) -> Outcome | None:
    for layer in ready.layers:
        for pattern in layer.deny:
            if not deny_governs(pattern, harness, SHELL_TOOL, fuel):
                continue
            if pattern.folded_argument is None or deny_argument_matches(
                pattern.folded_argument, pattern.tail_free, command, fuel
            ):
                return denied_by(layer, pattern)
    return None


def shell_outcome(request: ToolCallRequest, ready: Prepared, fuel: Fuel) -> Outcome:
    guarded = shell_condition(request, ready, fuel)
    if not guarded and not command_needed(request, ready, fuel):
        return by_name(request, ready.layers, fuel)
    raw = named_argument(request)
    command = parsed_command(raw)
    if guarded and (not command.complete or mentions_patch(raw)):
        return ARGUMENT_INVALID
    denial = shell_denial(command, ready, request.harness, fuel)
    if denial is not None:
        return denial
    for layer in ready.layers:
        if layer.allow and not any(
            allow_governs(pattern, request.harness, SHELL_TOOL, fuel)
            and allow_argument_matches(pattern.argument, command, fuel)
            for pattern in layer.allow
        ):
            return NOT_ALLOWED
    return ALLOWED_OUTCOME


def governed(request: ToolCallRequest, layers: tuple[Layer, Layer], fuel: Fuel) -> bool:
    return any(
        deny_governs(pattern, request.harness, request.tool, fuel)
        for layer in layers
        for pattern in layer.deny
    ) or any(
        allow_governs(pattern, request.harness, request.tool, fuel)
        for layer in layers
        for pattern in layer.allow
    )


def path_matches_deny(pattern: ToolPattern, path: EditedPath, fuel: Fuel) -> bool:
    argument = pattern.folded_argument
    if argument is None:
        return True
    if star_match(argument, path.folded_lexical, fuel):
        return True
    return path.folded_resolved != path.folded_lexical and star_match(
        argument, path.folded_resolved, fuel
    )


def path_matches_allow(pattern: ToolPattern, path: EditedPath, fuel: Fuel) -> bool:
    argument = pattern.argument
    if argument is None:
        return True
    if not star_match(argument, path.lexical, fuel):
        return False
    return path.resolved == path.lexical or star_match(argument, path.resolved, fuel)


def protected(paths: tuple[EditedPath, ...], ready: Prepared, fuel: Fuel) -> bool:
    return any(
        glob.matches(path.lexical_subject, fuel)
        or (
            path.resolved_subject != path.lexical_subject
            and glob.matches(path.resolved_subject, fuel)
        )
        for glob in ready.globs
        for path in paths
    )


def edit_outcome(request: ToolCallRequest, ready: Prepared, fuel: Fuel) -> Outcome:
    if not ready.globs and not governed(request, ready.layers, fuel):
        return by_name(request, ready.layers, fuel)
    root = workspace_root(request.workspace.root, fuel)
    paths = tuple(edited_path(raw, root, fuel) for raw in request_paths(request))
    if protected(paths, ready, fuel):
        return PROTECTED
    for layer in ready.layers:
        for pattern in layer.deny:
            if deny_governs(pattern, request.harness, request.tool, fuel) and any(
                path_matches_deny(pattern, path, fuel) for path in paths
            ):
                return denied_by(layer, pattern)
    for layer in ready.layers:
        if not layer.allow:
            continue
        governing = [
            pattern
            for pattern in layer.allow
            if allow_governs(pattern, request.harness, request.tool, fuel)
        ]
        if not all(
            any(path_matches_allow(pattern, path, fuel) for pattern in governing)
            for path in paths
        ):
            return NOT_ALLOWED
    return ALLOWED_OUTCOME


def evaluated(request: ToolCallRequest, ready: Prepared, fuel: Fuel) -> Outcome:
    if request.tool == SHELL_TOOL:
        return shell_outcome(request, ready, fuel)
    if is_edit_tool(request.harness, request.tool):
        return edit_outcome(request, ready, fuel)
    return by_name(request, ready.layers, fuel)


def decided(
    request: ToolCallRequest, ready: Prepared, budget: int
) -> tuple[Outcome, str]:
    try:
        return evaluated(request, ready, Fuel(budget)), "rule"
    except FuelExhausted:
        return ARGUMENT_INVALID, "fuel"
    except ArgumentInvalid:
        return ARGUMENT_INVALID, "rule"


class AllowDenyPolicy:
    name: str = "allow_deny"
    config_schema: JsonObject = CONFIG_SCHEMA

    def __init__(self, fuel_budget: int = DECISION_FUEL) -> None:
        self.fuel_budget: int = fuel_budget
        self.configurations: dict[ConfigurationKey, Prepared] = {}

    def configuration(
        self, layers: PolicyLayers, protected_paths: tuple[str, ...]
    ) -> Prepared | str:
        key = configuration_key(layers, protected_paths)
        known = self.configurations.pop(key, None)
        if known is not None:
            self.configurations[key] = known
            return known
        ready = prepared_configuration(layers, protected_paths)
        if isinstance(ready, Prepared):
            if len(self.configurations) >= PREPARED_ENTRIES:
                self.configurations.pop(next(iter(self.configurations)))
            self.configurations[key] = ready
        return ready

    def capabilities(self) -> PolicyCapabilities:
        return PolicyCapabilities()

    def validate(self, policy: JsonObject) -> list[ContractViolation]:
        violations: list[ContractViolation] | None = None
        try:
            violations = validated(policy)
        except DiagnosticsLimit:
            violations = None
        if violations is None:
            raise KoraProtocolError(
                "diagnostics_limit", REFUSALS["diagnostics_limit"], False, {}
            )
        return violations

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        ready = (
            self.configuration(layers, protected_paths)
            if request.harness in FILE_EDIT_TOOLS
            else "policy_harness_unsupported"
        )
        if isinstance(ready, str):
            ctx.logger.emit(REFUSED, code=ready)
            raise KoraProtocolError(ready, REFUSALS[ready], False, {})
        outcome, cause = decided(request, ready, self.fuel_budget)
        if outcome.allow:
            ctx.logger.emit(ALLOWED, reason=outcome.reason)
        else:
            ctx.logger.emit(DENIED, reason=outcome.reason, cause=cause)
        return PolicyDecision(allow=outcome.allow, reason=outcome.reason)


class DeploymentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def create_provider(configuration: JsonObject) -> AllowDenyPolicy:
    DeploymentConfiguration.model_validate(configuration)
    return AllowDenyPolicy()


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.POLICY,
        "allow_deny",
        create_provider,
        DEPLOYMENT_SCHEMA,
        VERSION,
    )
