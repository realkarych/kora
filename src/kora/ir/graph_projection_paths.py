from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass
from enum import Enum

from pydantic import MISSING

from kora.cel.cost import DATA_DEPTH
from kora.cel.diagnostics import DiagnosticError
from kora.cel.meter import Meter
from kora.cel.paths import Field
from kora.cel.projection import (
    Key,
    PathSet,
    ProjectionStep,
    Trie,
    ascii_sorted,
    parse_projection_path,
)
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import CheckedCelSite, FieldPath
from kora.ir.topology import (
    AgentNode,
    CommandNode,
    GraphBody,
    HumanInputNode,
    Node,
    WaitEventNode,
)

OutputPath = tuple[str, ...]
LOCAL_VARIABLES = frozenset({"response", "payload", "observation"})
STRUCTURE_UNIT_BYTES = 8
ASCII_BUCKET_UNITS = 8


class ProjectionFailure(Enum):
    CAPACITY = "capacity"
    ROUTE = "route"


class ProjectionRejected(Exception):
    def __init__(self, reason: ProjectionFailure) -> None:
        self.reason: ProjectionFailure = reason
        super().__init__(reason.value)


class ProjectionAdmission(Meter):
    def __init__(self, max_work: int, max_scratch_bytes: int) -> None:
        super().__init__()
        if any(
            type(limit) is not int or limit <= 0
            for limit in (max_work, max_scratch_bytes)
        ):
            raise ProjectionRejected(ProjectionFailure.CAPACITY)
        self.max_work: int = max_work
        self.scratch_remaining: int = max_scratch_bytes

    def tick(self, count: int = 1) -> None:
        if count > self.max_work - self.steps:
            raise ProjectionRejected(ProjectionFailure.CAPACITY)
        super().tick(count)

    def reserve(self, size: int) -> None:
        if size > self.scratch_remaining:
            raise ProjectionRejected(ProjectionFailure.CAPACITY)
        self.scratch_remaining -= size

    def parsed_path(self, text: str) -> tuple[ProjectionStep, ...]:
        characters = len(text)
        depth = min(DATA_DEPTH, characters)
        longest_prefix = characters + 3 * depth
        self.tick(characters + 1)
        self.reserve(
            STRUCTURE_UNIT_BYTES * (characters + depth + 2)
            + ASCII_BUCKET_UNITS * (depth + 1) * (longest_prefix + 1)
        )
        try:
            return parse_projection_path(text, self)
        except DiagnosticError:
            raise ProjectionRejected(ProjectionFailure.ROUTE) from None

    def order(self, keys: Collection[str]) -> tuple[str, ...]:
        units = 0
        for key in keys:
            self.tick()
            units += len(key) + 1
        self.reserve(ASCII_BUCKET_UNITS * units)
        return ascii_sorted(list(keys), self)


@dataclass(slots=True, repr=False)
class GraphProjectionPaths:
    parameters: PathSet
    outputs: dict[OutputPath, PathSet]


def path_reads(paths: PathSet) -> Iterator[tuple[bool, str]]:
    for path in paths.whole:
        yield True, path
    for path in paths.presence:
        yield False, path


def node_context(
    document: GraphDocument, path: FieldPath
) -> tuple[GraphBody, Node, OutputPath]:
    if path[:2] == ("graph", "nodes") and len(path) >= 4:
        body: GraphBody = document.graph
        prefix = ("graph",)
        node_name = path[2]
    elif path[:2] == ("graph", "subgraphs") and len(path) >= 6:
        name = path[2]
        bodies = document.graph.subgraphs
        if not isinstance(name, str) or bodies is MISSING or name not in bodies:
            raise ProjectionRejected(ProjectionFailure.ROUTE)
        body = bodies[name]
        prefix = ("graph", "subgraphs", name)
        node_name = path[4]
    else:
        raise ProjectionRejected(ProjectionFailure.ROUTE)
    if not isinstance(node_name, str) or node_name not in body.nodes:
        raise ProjectionRejected(ProjectionFailure.ROUTE)
    return body, body.nodes[node_name], prefix


def source_trie(
    body: GraphBody,
    node: Node,
    prefix: OutputPath,
    name: str,
    outputs: dict[OutputPath, Trie],
    admission: ProjectionAdmission,
) -> Trie:
    admission.tick(1 + len(name))
    binding = None if node.inputs is MISSING else node.inputs.get(name)
    if (
        binding is None
        or binding.from_ is MISSING
        or binding.output is MISSING
        or binding.path is not MISSING
        or binding.from_.startswith("$")
    ):
        raise ProjectionRejected(ProjectionFailure.ROUTE)
    admission.tick(len(binding.from_) + len(binding.output))
    producer = body.nodes.get(binding.from_)
    if not isinstance(
        producer, AgentNode | CommandNode | HumanInputNode | WaitEventNode
    ):
        raise ProjectionRejected(ProjectionFailure.ROUTE)
    output = producer.outputs.get(binding.output)
    if output is None or output.collect is not MISSING or output.type != binding.type:
        raise ProjectionRejected(ProjectionFailure.ROUTE)
    result = outputs.get((*prefix, "nodes", binding.from_, "outputs", binding.output))
    if result is None:
        raise ProjectionRejected(ProjectionFailure.ROUTE)
    return result


def compose_inputs(
    document: GraphDocument,
    site: CheckedCelSite,
    paths: PathSet,
    outputs: dict[OutputPath, Trie],
    admission: ProjectionAdmission,
) -> None:
    body, node, prefix = node_context(document, site.path)
    for whole, path in path_reads(paths):
        steps = admission.parsed_path(path)
        if not steps or not isinstance(steps[0], Field | Key):
            raise ProjectionRejected(ProjectionFailure.ROUTE)
        target = source_trie(body, node, prefix, steps[0].name, outputs, admission)
        target.insert(steps[1:], whole, admission)


def admit_direct_output(document: GraphDocument, path: OutputPath) -> None:
    _, producer, _ = node_context(document, path)
    if not isinstance(
        producer, AgentNode | CommandNode | HumanInputNode | WaitEventNode
    ):
        raise ProjectionRejected(ProjectionFailure.ROUTE)
    output = producer.outputs.get(path[-1])
    if output is None or output.collect is not MISSING:
        raise ProjectionRejected(ProjectionFailure.ROUTE)


def compose_graph_projection_paths(
    document: GraphDocument,
    sites: tuple[CheckedCelSite, ...],
    declarations: Iterable[list[str]],
    admission: ProjectionAdmission,
) -> GraphProjectionPaths:
    outputs: dict[OutputPath, Trie] = {}
    for declaration in declarations:
        size = sum(map(len, declaration)) + len(declaration)
        admission.tick(size)
        admission.reserve(STRUCTURE_UNIT_BYTES * (size + 1))
        path = tuple(declaration)
        if path in outputs:
            raise ProjectionRejected(ProjectionFailure.ROUTE)
        admit_direct_output(document, path)
        outputs[path] = Trie()
    admission.reserve(STRUCTURE_UNIT_BYTES)
    params = Trie()
    for site in sites:
        admission.tick(len(site.path))
        if site.path[-2:] in (
            ("retry", "predicate"),
            ("infrastructure_retry", "predicate"),
        ):
            continue
        for variable, paths in site.checked.projections.items():
            admission.tick(1 + len(variable))
            if variable == "params":
                for whole, path in path_reads(paths):
                    params.insert(admission.parsed_path(path), whole, admission)
            elif variable == "inputs":
                compose_inputs(document, site, paths, outputs, admission)
            elif variable not in LOCAL_VARIABLES:
                raise ProjectionRejected(ProjectionFailure.ROUTE)
    return GraphProjectionPaths(
        PathSet.from_trie(params, admission),
        {path: PathSet.from_trie(trie, admission) for path, trie in outputs.items()},
    )
