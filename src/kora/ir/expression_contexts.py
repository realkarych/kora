from pydantic import MISSING

from kora.cel import SchemaError, SchemaTypes
from kora.cel.cost import Variable
from kora.cel.meter import Meter
from kora.cel.parser import parse
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
    Node,
    Select,
    Unary,
)
from kora.ir.approval_form import default_approval_form
from kora.ir.approval_inputs import DirectInputContexts, InputFailure
from kora.ir.contracts import TypeContract
from kora.ir.json import JsonObject
from kora.ir.schema_catalogue import contract_schema
from kora.ir.topology import GraphBody, NodeEnvelope


def required_context(
    source: str,
    meter: Meter,
    names: frozenset[str] = frozenset({"inputs", "params"}),
) -> frozenset[str]:
    pending: list[Node] = [parse(source, meter=meter)]
    required: set[str] = set()
    while pending:
        node = pending.pop()
        meter.tick()
        if isinstance(node, Literal):
            continue
        if isinstance(node, Identifier):
            if node.name in names:
                required.add(node.name)
        elif isinstance(node, Comprehension):
            if node.variable in names:
                required.add(node.variable)
            pending.extend((node.range, *node.arguments))
        elif isinstance(node, Select | Unary):
            pending.append(node.operand)
        elif isinstance(node, Has):
            pending.append(node.target)
        elif isinstance(node, Index):
            pending.extend((node.operand, node.index))
        elif isinstance(node, Binary):
            pending.extend((node.left, node.right))
        elif isinstance(node, Conditional):
            pending.extend((node.condition, node.then, node.otherwise))
        elif isinstance(node, Call):
            pending.extend(node.arguments)
            if node.receiver is not None:
                pending.append(node.receiver)
        elif isinstance(node, ListLiteral):
            pending.extend(node.elements)
        else:
            pending.extend(
                part for entry in node.entries for part in (entry.key, entry.value)
            )
    return frozenset(required)


class InputParameterContexts:
    def __init__(
        self, contracts: dict[str, TypeContract], parameters: JsonObject | MISSING
    ) -> None:
        roots = {
            "urn:kora:artifact:" + name.replace("/", ":"): root
            for name, contract in contracts.items()
            if (root := contract_schema(name, contract)) is not None
        }
        self.schemas: SchemaTypes = SchemaTypes(roots.get)
        self.parameters: JsonObject | MISSING = parameters
        self.parameter_variable: Variable | None | MISSING = MISSING
        self.meter: Meter = Meter()
        self.default_form: JsonObject = default_approval_form()
        self.inputs: DirectInputContexts = DirectInputContexts(
            self.schemas, contracts, roots, self.default_form
        )

    def parameter_context(self) -> Variable | None:
        if self.parameter_variable is MISSING:
            parameters: JsonObject = (
                {"type": "object", "properties": {}, "additionalProperties": False}
                if self.parameters is MISSING
                else self.parameters
            )
            try:
                self.parameter_variable = self.schemas.variable(parameters)
            except SchemaError:
                self.parameter_variable = None
        return self.parameter_variable

    def context(
        self,
        body: GraphBody,
        node: NodeEnvelope,
        source: str,
    ) -> dict[str, Variable] | InputFailure:
        return self.variables(body, node, required_context(source, self.meter))

    def variables(
        self, body: GraphBody, node: NodeEnvelope, required: frozenset[str]
    ) -> dict[str, Variable] | InputFailure:
        variables: dict[str, Variable] = {}
        if "inputs" in required:
            inputs = self.inputs.context(body, node)
            if isinstance(inputs, InputFailure):
                return inputs
            variables["inputs"] = inputs
        if "params" in required:
            parameter = self.parameter_context()
            if parameter is None:
                return InputFailure.SCHEMA
            variables["params"] = parameter
        return variables
