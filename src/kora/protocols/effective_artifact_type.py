import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from kora.ir import ContractViolation
from kora.ir.json import JsonValue
from kora.ir.standard_output_preparation import (
    StandardOutputInput,
    prepare_standard_output,
)
from kora.ir.standard_schema_policy import (
    Reason,
    SchemaDomain,
    SchemaRefused,
    SourceAccounting,
    refuse,
)
from kora.ir.standard_schema_validation import validate_standard_schema
from kora.ir.structured_output import (
    StructuredOutput,
    StructuredOutputValidationError,
    checked_value,
    parse_structured_output,
    parse_structured_output_json,
)
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.common import ProtocolContext

Validation = Callable[[JsonValue, ProtocolContext], list[ContractViolation]]
Selection = Callable[[JsonValue, ProtocolContext], list[str]]
Summary = Callable[[JsonValue, ProtocolContext], JsonValue]
Role = Literal["validation", "digest_fields", "summary"]
COMPONENT: str = CATALOG.register_component("protocols.effective_artifact_type")
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "protocols.effective_artifact_type.completed",
        COMPONENT,
        Level.DEBUG,
        "effective artifact type operation completed",
        dict.fromkeys(("operation", "outcome", "reason"), FieldKind.STRING),
    )
)


@dataclass(frozen=True, slots=True)
class ValidationBinding:
    identity: str
    operation: Validation = field(repr=False)


@dataclass(frozen=True, slots=True)
class SelectionBinding:
    identity: str
    operation: Selection = field(repr=False)


@dataclass(frozen=True, slots=True)
class SummaryBinding:
    identity: str
    operation: Summary = field(repr=False)


@dataclass(frozen=True, slots=True)
class StandardSchemaBinding:
    identity: Literal["kora.json-schema-2020-12.v1"] = "kora.json-schema-2020-12.v1"


BehaviorBinding = (
    ValidationBinding | SelectionBinding | SummaryBinding | StandardSchemaBinding
)
InstalledBehaviors = Mapping[tuple[Role, str], BehaviorBinding]


@dataclass(frozen=True, slots=True)
class EffectiveRefusal:
    outcome: Literal["refused"] = field(default="refused", init=False)
    reason: Reason


@dataclass(frozen=True, slots=True)
class EffectiveInvalid:
    outcome: Literal["invalid"] = field(default="invalid", init=False)
    reason: Literal["schema_mismatch", "behavior_mismatch", "model_mismatch"]
    violation: ContractViolation | None = None


@dataclass(frozen=True, slots=True)
class EffectiveValid:
    outcome: Literal["valid"] = field(default="valid", init=False)
    reason: Literal["none"] = field(default="none", init=False)
    value: StructuredOutput = field(repr=False)
    selection: Selection = field(repr=False)
    summarize: Summary = field(repr=False)

    def digest_fields(self, ctx: ProtocolContext) -> list[str]:
        result = self.selection(checked_value(self.value), ctx)
        report("digest_fields", "completed", "none")
        return result

    def summary(self, ctx: ProtocolContext) -> JsonValue:
        result = self.summarize(checked_value(self.value), ctx)
        report("summary", "completed", "none")
        return result


EffectiveDecision = EffectiveValid | EffectiveInvalid | EffectiveRefusal


def report(operation: str, outcome: str, reason: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED, operation=operation, outcome=outcome, reason=reason
        )


def mismatch() -> EffectiveInvalid:
    return EffectiveInvalid(
        "schema_mismatch",
        ContractViolation(
            code="SO006",
            message="StructuredOutput value does not satisfy its effective schemas.",
            path=(),
        ),
    )


@dataclass(frozen=True, slots=True)
class EffectiveArtifactType:
    source_bytes: bytes = field(repr=False)
    validations: tuple[Validation, ...] = field(repr=False)
    selection: Selection = field(repr=False)
    summarize: Summary = field(repr=False)
    accounting: SourceAccounting

    def _decide(
        self, model: StructuredOutput, ctx: ProtocolContext
    ) -> EffectiveDecision:
        for operation in self.validations:
            if operation(checked_value(model), ctx):
                return EffectiveInvalid("behavior_mismatch")
        source = cast("dict[str, object]", json.loads(self.source_bytes))
        metadata = cast("dict[str, object]", source["metadata"])
        result = validate_standard_schema(
            checked_value(model),
            source,
            SchemaDomain.OUTPUT,
            metadata["schema_validation"],
        )
        if result.outcome == "refused":
            return EffectiveRefusal(cast("Reason", result.reason))
        if result.outcome == "invalid":
            return mismatch()
        return EffectiveValid(model, self.selection, self.summarize)

    def _validate(
        self, value: object, ctx: ProtocolContext, *, raw: bool
    ) -> EffectiveDecision:
        try:
            model = (
                parse_structured_output_json(cast("bytes", value))
                if raw
                else parse_structured_output(value)
            )
        except StructuredOutputValidationError as error:
            result: EffectiveDecision = EffectiveInvalid(
                "model_mismatch", error.violation
            )
        else:
            result = self._decide(model, ctx)
            del model
        report("validate", result.outcome, result.reason)
        return result

    def validate(self, value: object, ctx: ProtocolContext) -> EffectiveDecision:
        return self._validate(value, ctx, raw=False)

    def validate_json(self, value: bytes, ctx: ProtocolContext) -> EffectiveDecision:
        return self._validate(value, ctx, raw=True)


def required_binding(
    bindings: InstalledBehaviors, role: Role, identity: str
) -> BehaviorBinding:
    binding = bindings.get((role, identity))
    if type(binding) not in (
        ValidationBinding,
        SelectionBinding,
        SummaryBinding,
        StandardSchemaBinding,
    ):
        refuse("unsupported_schema")
    selected = cast("BehaviorBinding", binding)
    if type(selected.identity) is not str or selected.identity != identity:
        refuse("unsupported_schema")
    return selected


def prepared(
    value: StandardOutputInput, bindings: InstalledBehaviors
) -> EffectiveArtifactType:
    source = prepare_standard_output(value)
    validations: list[Validation] = []
    selection: Selection | None = None
    summary: Summary | None = None
    for behavior in source.behaviors:
        validation = required_binding(bindings, "validation", behavior.validation)
        if behavior.validation == "kora.json-schema-2020-12.v1":
            if type(validation) is not StandardSchemaBinding:
                refuse("unsupported_schema")
        elif type(validation) is ValidationBinding:
            if not callable(validation.operation):
                refuse("unsupported_schema")
            validations.append(validation.operation)
        else:
            refuse("unsupported_schema")
        digest = required_binding(bindings, "digest_fields", behavior.digest_fields)
        summarized = required_binding(bindings, "summary", behavior.summary)
        if (
            type(digest) is not SelectionBinding
            or type(summarized) is not SummaryBinding
        ):
            refuse("unsupported_schema")
        if not callable(digest.operation) or not callable(summarized.operation):
            refuse("unsupported_schema")
        selection, summary = digest.operation, summarized.operation
    if selection is None or summary is None:
        refuse("unsupported_schema")
    return EffectiveArtifactType(
        source.source_bytes, tuple(validations), selection, summary, source.accounting
    )


def prepare_effective_artifact_type(
    value: StandardOutputInput, bindings: InstalledBehaviors
) -> EffectiveArtifactType | EffectiveRefusal | EffectiveInvalid:
    try:
        result: EffectiveArtifactType | EffectiveRefusal | EffectiveInvalid = prepared(
            value, bindings
        )
    except SchemaRefused as error:
        result = EffectiveRefusal(error.reason)
    except StructuredOutputValidationError as error:
        result = EffectiveInvalid("model_mismatch", error.violation)
    report(
        "prepare",
        "prepared" if isinstance(result, EffectiveArtifactType) else result.outcome,
        "none" if isinstance(result, EffectiveArtifactType) else result.reason,
    )
    return result
