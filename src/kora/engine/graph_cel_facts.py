from dataclasses import dataclass
from operator import itemgetter
from typing import Never, cast

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    TextBudget,
    quoted,
    render_object,
)
from kora.engine.graph_output_inventory import read_graph_output_inventory
from kora.engine.graph_parameters import read_graph_parameter_binding
from kora.engine.graph_type_references import array_text
from kora.engine.graph_types import read_document
from kora.ir.cel_inventory_admission import (
    CheckedInventoryAdmission,
    InventoryAdmissionExhausted,
)
from kora.ir.expression_fields import CheckedCelSite
from kora.ir.graph_cel_inventory import checked_graph_sites
from kora.ir.json import JsonObject, JsonValue

EXECUTION_SEMANTICS = "kora.execution.v1"
FACTS_PREFIX = '{"interpretation":"kora.cel-expression-facts.v1","fields":['
FACTS_SUFFIX = "]}"


class GraphCelFacts:
    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text: str = text

    @property
    def text(self) -> str:
        return self._text

    def __getstate__(self) -> Never:
        raise TypeError("graph CEL facts require explicit access")


def local_reads(site: CheckedCelSite, budget: TextBudget) -> JsonObject:
    reads: JsonObject = {}
    projections = site.checked.projections
    names = sorted(projections, key=lambda name: quoted(name, budget))
    for name in names:
        paths = projections[name]
        budget.reserve(len(paths.whole) + len(paths.presence))
        reads[name] = {"whole": list(paths.whole), "presence": list(paths.presence)}
    return reads


def fact_entry(site: CheckedCelSite, budget: TextBudget) -> tuple[str, str]:
    budget.reserve(len(site.path))
    path: list[JsonValue] = list(site.path)
    key = array_text(path, budget)
    estimate = site.checked.estimate
    entry: JsonObject = {
        "path": path,
        "origin": "authored" if site.authored else "approval_default",
        "result": "string" if site.path[-1] in ("reason", "else_reason") else "bool",
        "rendering": site.checked.rendering,
        "cost": {
            "steps": estimate.steps,
            "memory": estimate.memory,
            "compile_steps": estimate.compile_steps,
        },
        "reads": local_reads(site, budget),
    }
    return key, render_object(entry, budget, reject_aliases=True)


def stage_graph_cel_facts(
    sites: tuple[CheckedCelSite, ...], max_bytes: int
) -> GraphCelFacts:
    budget = TextBudget(max_bytes)
    budget.reserve(len(FACTS_PREFIX) + len(FACTS_SUFFIX))
    entries: list[tuple[str, str]] = []
    for site in sites:
        budget.reserve(int(bool(entries)))
        entries.append(fact_entry(site, budget))
    entries.sort(key=itemgetter(0))
    return GraphCelFacts(
        FACTS_PREFIX + ",".join(entry for _, entry in entries) + FACTS_SUFFIX
    )


@dataclass(frozen=True, slots=True)
class GraphCelReadLimits:
    ir_bytes: int
    output_context_bytes: int
    parameter_context_bytes: int
    comparison_bytes: int
    declarations: int
    triggers: int
    typed_work: int
    typed_bytes: int

    def __post_init__(self) -> None:
        if any(
            type(limit) is not int or limit <= 0
            for limit in (
                self.ir_bytes,
                self.output_context_bytes,
                self.parameter_context_bytes,
                self.comparison_bytes,
                self.typed_work,
                self.typed_bytes,
            )
        ) or any(
            type(limit) is not int or limit < 0
            for limit in (self.declarations, self.triggers)
        ):
            raise GraphValueRejected(GraphValueFailure.BUDGET)


def read_graph_cel_facts(
    ir_text: str, execution_contract: object, *, limits: GraphCelReadLimits
) -> GraphCelFacts:
    try:
        return checked_saved_facts(ir_text, execution_contract, limits)
    except GraphValueRejected as rejection:
        reason = rejection.reason
    del ir_text, execution_contract
    raise_safe(GraphValueRejected(reason))


def checked_saved_facts(
    ir_text: str, execution_contract: object, limits: GraphCelReadLimits
) -> GraphCelFacts:
    if type(execution_contract) is not dict:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    contract = cast(dict[object, object], execution_contract)
    semantics = contract.get("semantics")
    if type(semantics) is not str or semantics != EXECUTION_SEMANTICS:
        raise GraphValueRejected(GraphValueFailure.VERSION)
    comparison = TextBudget(limits.comparison_bytes)
    saved = render_object(
        contract.get("cel_expressions"), comparison, reject_aliases=True
    )
    document = read_document(ir_text, TextBudget(limits.ir_bytes))
    outputs = read_graph_output_inventory(
        ir_text,
        contract.get("type_contracts"),
        contract.get("outputs"),
        max_encoded_bytes=limits.output_context_bytes,
    )
    if not isinstance(outputs, str):
        reason = (
            GraphValueFailure.BUDGET
            if outputs.reason == "preparation_exhausted"
            else GraphValueFailure.VERSION
        )
        raise GraphValueRejected(reason)
    del outputs
    read_graph_parameter_binding(
        document,
        contract.get("parameters"),
        max_encoded_bytes=limits.parameter_context_bytes,
    )
    try:
        sites = checked_graph_sites(
            document,
            limits.declarations,
            limits.triggers,
            admission=CheckedInventoryAdmission(limits.typed_work, limits.typed_bytes),
        )
    except InventoryAdmissionExhausted:
        sites = None
    if sites is None:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    if not isinstance(sites, tuple):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    expected = stage_graph_cel_facts(sites, comparison.remaining)
    if saved != expected.text:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return GraphCelFacts(saved)
