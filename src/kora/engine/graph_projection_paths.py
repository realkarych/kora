from dataclasses import dataclass
from typing import Never, cast

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    quoted,
    render_object,
)
from kora.engine.graph_cel_facts import GraphCelReadLimits, checked_saved_facts
from kora.engine.graph_output_inventory import inventory_paths
from kora.engine.graph_retry_policies import read_graph_retry_policies
from kora.ir.graph_projection_paths import (
    GraphProjectionPaths,
    OutputPath,
    ProjectionAdmission,
    ProjectionFailure,
    ProjectionRejected,
    compose_graph_projection_paths,
)

INTERPRETATION = "kora.cel-projection-paths.v1"
PREFIX = '{"interpretation":"' + INTERPRETATION + '","parameters":'
OUTPUTS = ',"outputs":['
ENDING = "]}"
OUTPUT_SOURCE_PASSES = 8
PARAMETER_SOURCE_PASSES = 3
SECTION_RENDER_COPIES = 4
SECTION_MEMBERS = frozenset({"interpretation", "parameters", "outputs"})
ENTRY_MEMBERS = frozenset({"path", "paths"})
SET_MEMBERS = frozenset({"whole", "presence"})


class GraphCelProjectionPaths:
    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text: str = text

    @property
    def text(self) -> str:
        return self._text

    def __getstate__(self) -> Never:
        raise TypeError("graph CEL projection paths require explicit access")


@dataclass(frozen=True, slots=True)
class GraphProjectionReadLimits:
    cel: GraphCelReadLimits
    retry_bytes: int
    source_bytes: int
    section_bytes: int
    work: int
    scratch_bytes: int

    def __post_init__(self) -> None:
        if any(
            type(limit) is not int or limit <= 0
            for limit in (
                self.retry_bytes,
                self.source_bytes,
                self.section_bytes,
                self.work,
                self.scratch_bytes,
            )
        ):
            raise GraphValueRejected(GraphValueFailure.BUDGET)


class ProjectionTextBudget(TextBudget):
    def __init__(self, max_bytes: int, admission: ProjectionAdmission) -> None:
        super().__init__(min(max_bytes, admission.max_work - admission.steps))
        self.admission: ProjectionAdmission = admission

    def reserve(self, count: int) -> None:
        super().reserve(count)
        self.admission.tick(count)


def reserve_source_passes(
    ir_text: str, limits: GraphProjectionReadLimits, admission: ProjectionAdmission
) -> None:
    source = TextBudget(limits.source_bytes)
    source.reserve(2 * limits.cel.ir_bytes)
    source.reserve(OUTPUT_SOURCE_PASSES * limits.cel.output_context_bytes)
    source.reserve(PARAMETER_SOURCE_PASSES * limits.cel.parameter_context_bytes)
    source.reserve(limits.cel.comparison_bytes)
    source.reserve(2 * limits.retry_bytes)
    source.reserve(len(ir_text))
    admission.reserve(
        limits.cel.ir_bytes
        + limits.cel.typed_bytes
        + len(ir_text)
        + SECTION_RENDER_COPIES * limits.section_bytes
    )


def encode_set(
    whole: tuple[str, ...], presence: tuple[str, ...], budget: TextBudget
) -> str:
    budget.reserve(len(whole) + len(presence))
    return render_object({"whole": list(whole), "presence": list(presence)}, budget)


def encode_paths(
    paths: GraphProjectionPaths, budget: TextBudget, admission: ProjectionAdmission
) -> str:
    budget.reserve(len(PREFIX) + len(OUTPUTS) + len(ENDING))
    parameters = encode_set(paths.parameters.whole, paths.parameters.presence, budget)
    by_key: dict[str, OutputPath] = {}
    for path in paths.outputs:
        budget.reserve(2 * len(path) + 1)
        encoded_path = "[" + ",".join(quoted(segment, budget) for segment in path) + "]"
        by_key[encoded_path] = path
    keys = admission.order(by_key)
    entries: list[str] = []
    for key in keys:
        budget.reserve(len('{"path":,"paths":}') + len(key) + int(bool(entries)))
        selected = paths.outputs[by_key[key]]
        encoded = encode_set(selected.whole, selected.presence, budget)
        entries.append('{"path":' + key + ',"paths":' + encoded + "}")
    return PREFIX + parameters + OUTPUTS + ",".join(entries) + ENDING


def exact_members(
    value: object, names: frozenset[str], admission: ProjectionAdmission
) -> dict[object, object]:
    admission.tick(len(names))
    if type(value) is not dict:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    members = cast(dict[object, object], value)
    if members.keys() != names:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return members


def saved_items(value: object, admission: ProjectionAdmission) -> list[object]:
    if type(value) is not list:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    items = cast(list[object], value)
    admission.tick(1 + len(items))
    return items


def check_string_items(value: object, admission: ProjectionAdmission) -> None:
    if any(type(item) is not str for item in saved_items(value, admission)):
        raise GraphValueRejected(GraphValueFailure.INVALID)


def check_saved_set(value: object, admission: ProjectionAdmission) -> None:
    paths = exact_members(value, SET_MEMBERS, admission)
    check_string_items(paths["whole"], admission)
    check_string_items(paths["presence"], admission)


def read_saved_paths(value: object, budget: ProjectionTextBudget) -> str:
    admission = budget.admission
    saved = exact_members(value, SECTION_MEMBERS, admission)
    interpretation = saved.get("interpretation")
    if type(interpretation) is not str or interpretation != INTERPRETATION:
        raise GraphValueRejected(GraphValueFailure.VERSION)
    check_saved_set(saved["parameters"], admission)
    for output in saved_items(saved["outputs"], admission):
        entry = exact_members(output, ENTRY_MEMBERS, admission)
        check_string_items(entry["path"], admission)
        check_saved_set(entry["paths"], admission)
    return render_object(saved, budget, reject_aliases=True)


def checked_projection_paths(
    ir_text: str,
    execution_contract: object,
    limits: GraphProjectionReadLimits,
    *,
    read_saved: bool,
) -> GraphCelProjectionPaths:
    if type(ir_text) is not str or type(execution_contract) is not dict:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    contract = cast(dict[object, object], execution_contract)
    admission = ProjectionAdmission(limits.work, limits.scratch_bytes)
    reserve_source_passes(ir_text, limits, admission)
    facts, document, sites = checked_saved_facts(ir_text, contract, limits.cel)
    del facts
    read_graph_retry_policies(
        ir_text, contract.get("retry_policies"), max_encoded_bytes=limits.retry_bytes
    )
    admission.tick(len(ir_text))
    paths = compose_graph_projection_paths(
        document, sites, inventory_paths(ObjectReader(ir_text).object()), admission
    )
    del document, sites
    budget = ProjectionTextBudget(limits.section_bytes, admission)
    saved = (
        read_saved_paths(contract.get("cel_projection_paths"), budget)
        if read_saved
        else None
    )
    expected = encode_paths(paths, budget, admission)
    if saved is not None:
        admission.tick(len(saved) + len(expected))
        if saved != expected:
            raise GraphValueRejected(GraphValueFailure.INVALID)
    return GraphCelProjectionPaths(expected)


def projection_paths_result(
    ir_text: str,
    execution_contract: object,
    limits: GraphProjectionReadLimits,
    *,
    read_saved: bool,
) -> GraphCelProjectionPaths | GraphValueFailure:
    try:
        return checked_projection_paths(
            ir_text, execution_contract, limits, read_saved=read_saved
        )
    except ProjectionRejected as rejection:
        return (
            GraphValueFailure.BUDGET
            if rejection.reason is ProjectionFailure.CAPACITY
            else GraphValueFailure.VERSION
        )
    except GraphValueRejected as rejection:
        return rejection.reason


def build_graph_cel_projection_paths(
    ir_text: str, execution_contract: object, *, limits: GraphProjectionReadLimits
) -> GraphCelProjectionPaths:
    result = projection_paths_result(
        ir_text, execution_contract, limits, read_saved=False
    )
    if isinstance(result, GraphCelProjectionPaths):
        return result
    del ir_text, execution_contract
    raise_safe(GraphValueRejected(result))


def read_graph_cel_projection_paths(
    ir_text: str, execution_contract: object, *, limits: GraphProjectionReadLimits
) -> GraphCelProjectionPaths:
    result = projection_paths_result(
        ir_text, execution_contract, limits, read_saved=True
    )
    if isinstance(result, GraphCelProjectionPaths):
        return result
    del ir_text, execution_contract
    raise_safe(GraphValueRejected(result))
