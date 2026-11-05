from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from pydantic import TypeAdapter, ValidationError
from pydantic_core import PydanticCustomError

from kora.api.exchange import COMPONENT
from kora.cel import CheckedExpression, Reason
from kora.cel.diagnostics import Diagnostic as CelDiagnostic
from kora.cel.limits import MAX_RENDERING_DEPTH
from kora.cel.tree import (
    Binary,
    Call,
    Comprehension,
    Conditional,
    Has,
    Identifier,
    Index,
    ListLiteral,
    Literal,
    MapLiteral,
    Node,
    Select,
    StringLiteral,
    Unary,
)
from kora.cel_eval.evaluate import PatternCompilation
from kora.cel_eval.thread import Evaluator
from kora.ir.approval_compilation import ApprovalPrerequisite, checked_approval_sites
from kora.ir.branch_expressions import BranchExpressionResult, checked_branch_sites
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import DiagnosticPath, from_cel, path_sequence
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath
from kora.ir.trigger_expressions import (
    SensorTriggerExpressionResult,
    checked_sensor_trigger_sites,
)
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.cel_patterns.completed",
        COMPONENT,
        Level.DEBUG,
        "CEL pattern verification completed",
        {"status": FieldKind.STRING},
    )
)
PATH_TYPE: TypeAdapter[FieldPath] = TypeAdapter(DiagnosticPath)


@dataclass(frozen=True, slots=True, repr=False)
class CheckedPatternSite:
    path: FieldPath
    checked: CheckedExpression
    authored: bool


@dataclass(frozen=True, slots=True)
class CompletePatternCheck:
    diagnostics: DiagnosticCollection


class IncompletePatternCheck(StrEnum):
    INVALID_INVENTORY = "invalid_inventory"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    NATIVE_INCOMPLETE = "native_incomplete"
    GENERATED_REJECTED = "generated_rejected"


PatternCheck = CompletePatternCheck | IncompletePatternCheck
PatternOccurrence = tuple[str, int]


class InventoryRefused(Exception):
    def __init__(self, reason: IncompletePatternCheck) -> None:
        super().__init__(reason.value)
        self.reason: IncompletePatternCheck = reason


@dataclass(slots=True)
class WorkAllowance:
    remaining: int

    def reserve(self, units: int) -> None:
        if units > self.remaining:
            raise InventoryRefused(IncompletePatternCheck.CAPACITY_EXHAUSTED)
        self.remaining -= units


@dataclass(slots=True, repr=False)
class FieldPatterns:
    authored: bool
    occurrences: list[PatternOccurrence]


@dataclass(slots=True, repr=False)
class PreparedPatterns:
    probes: dict[str, PatternCompilation | None]
    fields: dict[FieldPath, FieldPatterns]


def node_children(node: Node) -> Iterator[Node]:
    if isinstance(node, Literal | Identifier):
        return
    match node:
        case Select(operand=operand) | Unary(operand=operand):
            yield operand
        case Index(operand=operand, index=index):
            yield operand
            yield index
        case Call(receiver=receiver, arguments=arguments):
            if receiver is not None:
                yield receiver
            yield from arguments
        case ListLiteral(elements=elements):
            yield from elements
        case MapLiteral(entries=entries):
            for entry in entries:
                yield entry.key
                yield entry.value
        case Binary(left=left, right=right):
            yield left
            yield right
        case Conditional(condition=condition, then=then, otherwise=otherwise):
            yield condition
            yield then
            yield otherwise
        case Has(target=target):
            yield target
        case Comprehension(range=range_node, arguments=arguments):
            yield range_node
            yield from arguments
        case _:
            raise InventoryRefused(IncompletePatternCheck.INVALID_INVENTORY)


def checked_nodes(root: Node) -> Iterator[Node]:
    yield root
    stack = [node_children(root)]
    while stack:
        child = next(stack[-1], None)
        if child is None:
            stack.pop()
            continue
        if len(stack) > MAX_RENDERING_DEPTH:
            raise InventoryRefused(IncompletePatternCheck.INVALID_INVENTORY)
        yield child
        stack.append(node_children(child))


def field_path(path: FieldPath, allowance: WorkAllowance) -> FieldPath:
    if type(path) is not tuple or not path:
        raise InventoryRefused(IncompletePatternCheck.INVALID_INVENTORY)
    path_sequence(path)
    allowance.reserve(len(path))
    for segment in path:
        if type(segment) is str:
            allowance.reserve(len(segment))
        elif type(segment) is not int:
            raise InventoryRefused(IncompletePatternCheck.INVALID_INVENTORY)
    return PATH_TYPE.validate_python(path)


def pattern_literal(node: Call) -> StringLiteral:
    if node.receiver is not None and len(node.arguments) == 1:
        argument = node.arguments[0]
        if isinstance(argument, StringLiteral):
            return argument
    raise InventoryRefused(IncompletePatternCheck.INVALID_INVENTORY)


def prepare_patterns(
    sites: tuple[CheckedPatternSite, ...], max_work: int
) -> PreparedPatterns | IncompletePatternCheck:
    if type(sites) is not tuple or type(max_work) is not int or max_work <= 0:
        return IncompletePatternCheck.INVALID_INVENTORY
    allowance = WorkAllowance(max_work)
    prepared = PreparedPatterns({}, {})
    try:
        for site in sites:
            allowance.reserve(1)
            if (
                type(site) is not CheckedPatternSite
                or type(site.checked) is not CheckedExpression
                or type(site.authored) is not bool
            ):
                return IncompletePatternCheck.INVALID_INVENTORY
            path = field_path(site.path, allowance)
            if path in prepared.fields:
                return IncompletePatternCheck.INVALID_INVENTORY
            field = FieldPatterns(site.authored, [])
            prepared.fields[path] = field
            for node in checked_nodes(site.checked.tree):
                allowance.reserve(1)
                if isinstance(node, Call) and node.function == "matches":
                    literal = pattern_literal(node)
                    allowance.reserve(1 + len(literal.value))
                    prepared.probes.setdefault(literal.value, None)
                    field.occurrences.append((literal.value, literal.offset))
    except InventoryRefused as refusal:
        return refusal.reason
    except (ValidationError, PydanticCustomError):
        return IncompletePatternCheck.INVALID_INVENTORY
    return prepared


def collect_pattern_findings(prepared: PreparedPatterns) -> PatternCheck:
    collector = DiagnosticCollector()
    for path, field in prepared.fields.items():
        offset = min(
            (
                offset
                for pattern, offset in field.occurrences
                if prepared.probes[pattern] is PatternCompilation.REJECTED
            ),
            default=None,
        )
        if offset is None:
            continue
        if not field.authored:
            return IncompletePatternCheck.GENERATED_REJECTED
        collector.add(
            from_cel(
                CelDiagnostic(Reason.INVALID_PATTERN, offset),
                path=path,
                exact_expression_field=True,
            )
        )
    return CompletePatternCheck(collector.finish())


def emit_completion(status: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(COMPLETED, status=status)


def announce(result: PatternCheck) -> PatternCheck:
    emit_completion(
        result.value if isinstance(result, IncompletePatternCheck) else "complete"
    )
    return result


async def validate_cel_patterns(
    sites: tuple[CheckedPatternSite, ...], evaluator: Evaluator, *, max_work: int
) -> PatternCheck:
    prepared = prepare_patterns(sites, max_work)
    del sites
    if isinstance(prepared, IncompletePatternCheck):
        return announce(prepared)
    try:
        for pattern in prepared.probes:
            result = await evaluator.validate_pattern(pattern)
            if result is PatternCompilation.INCOMPLETE:
                return announce(IncompletePatternCheck.NATIVE_INCOMPLETE)
            prepared.probes[pattern] = result
        return announce(collect_pattern_findings(prepared))
    finally:
        prepared.probes.clear()
        prepared.fields.clear()


async def validate_sensor_trigger_patterns(
    document: GraphDocument, evaluator: Evaluator, *, max_triggers: int, max_work: int
) -> PatternCheck | SensorTriggerExpressionResult:
    if type(max_triggers) is not int or max_triggers < 0:
        return announce(IncompletePatternCheck.INVALID_INVENTORY)
    inventory = checked_sensor_trigger_sites(document, max_triggers=max_triggers)
    if inventory is None:
        return announce(IncompletePatternCheck.CAPACITY_EXHAUSTED)
    if isinstance(inventory, SensorTriggerExpressionResult):
        return inventory
    verification = validate_cel_patterns(
        tuple(CheckedPatternSite(site.path, site.checked, True) for site in inventory),
        evaluator,
        max_work=max_work,
    )
    del document, inventory
    return await verification


async def validate_approval_patterns(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    max_declarations: int,
    max_work: int,
) -> PatternCheck | DiagnosticCollection | ApprovalPrerequisite:
    if type(max_declarations) is not int or max_declarations < 0:
        return announce(IncompletePatternCheck.INVALID_INVENTORY)
    inventory = checked_approval_sites(document, max_declarations=max_declarations)
    if inventory is None:
        return announce(IncompletePatternCheck.CAPACITY_EXHAUSTED)
    if isinstance(inventory, ApprovalPrerequisite | DiagnosticCollection):
        emit_completion(
            inventory.value
            if isinstance(inventory, ApprovalPrerequisite)
            else "typed_refused"
        )
        return inventory
    verification = validate_cel_patterns(
        tuple(
            CheckedPatternSite(site.path, site.checked, site.authored)
            for site in inventory
        ),
        evaluator,
        max_work=max_work,
    )
    del document, inventory
    return await verification


async def validate_branch_patterns(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    max_declarations: int,
    max_work: int,
) -> PatternCheck | BranchExpressionResult:
    if type(max_declarations) is not int or max_declarations < 0:
        return announce(IncompletePatternCheck.INVALID_INVENTORY)
    inventory = checked_branch_sites(document, max_declarations=max_declarations)
    if inventory is None:
        return announce(IncompletePatternCheck.CAPACITY_EXHAUSTED)
    if isinstance(inventory, BranchExpressionResult):
        return inventory
    verification = validate_cel_patterns(
        tuple(CheckedPatternSite(site.path, site.checked, True) for site in inventory),
        evaluator,
        max_work=max_work,
    )
    del document, inventory
    return await verification
