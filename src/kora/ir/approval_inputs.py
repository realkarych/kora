import json
from dataclasses import dataclass, replace
from enum import Enum
from importlib.resources import files

from pydantic import MISSING

from kora.cel import SchemaError, SchemaTypes
from kora.cel.cost import DATA_BOUND, ValueBound, Variable
from kora.cel.schema import SchemaView
from kora.cel.types import Record, RecordField
from kora.ir.contracts import BuiltinTypeContract, TypeContract
from kora.ir.data import validate_ir_json_object
from kora.ir.declarations import InputRef, OutputDecl
from kora.ir.json import JsonObject
from kora.ir.output_schemas import StructuredAncestry
from kora.ir.schema_equality import compare_json
from kora.ir.topology import (
    AgentNode,
    CommandNode,
    GraphBody,
    HumanInputNode,
    WaitEventNode,
)


class InputFailure(Enum):
    CONTEXT = "context"
    SCHEMA = "schema"


@dataclass(frozen=True)
class InputView:
    schema: JsonObject
    view: SchemaView = SchemaView.PLAIN


Producer = AgentNode | CommandNode | HumanInputNode | WaitEventNode


class DirectInputContexts:
    def __init__(
        self,
        schemas: SchemaTypes,
        contracts: dict[str, TypeContract],
        roots: dict[str, JsonObject],
        default_form: JsonObject,
    ) -> None:
        self.schemas = schemas
        self.contracts = contracts
        self.roots = roots
        self.default_form = default_form
        self.ancestry: StructuredAncestry = StructuredAncestry(contracts)
        self.sources: dict[tuple[int, str, str], InputView | None] = {}
        self.producers: dict[int, bool] = {}
        self.variables: dict[tuple[int, SchemaView], Variable | InputFailure] = {}
        self.session_schema: JsonObject | None = None
        self.source_steps: int = 0

    def agent_roles(self, node: AgentNode) -> bool:
        known = self.producers.get(id(node))
        if known is not None:
            return known
        structured = vcs = 0
        available = True
        for declaration in node.outputs.values():
            self.source_steps += 1
            inherited = self.ancestry.contains(declaration.type)
            available &= inherited is not None
            structured += inherited is True
            vcs += declaration.type == "VcsRef"
        result = available and structured <= 1 and vcs <= 1
        self.producers[id(node)] = result
        return result

    def matching(self, left: JsonObject, right: JsonObject | MISSING) -> bool:
        if right is MISSING:
            return True
        comparison = compare_json(left, right)
        self.source_steps += comparison.values_checked
        return comparison.equal

    def structured(self, node: Producer, output: OutputDecl) -> InputView | None:
        schema = output.schema_
        if isinstance(node, HumanInputNode):
            form = (
                self.default_form if node.config.form is MISSING else node.config.form
            )
            if not self.matching(form, schema):
                return None
            schema = form
        elif isinstance(node, AgentNode):
            configured = node.config.output_schema
            if configured is MISSING:
                if schema is not MISSING:
                    return None
            elif not self.matching(configured, schema):
                return None
            else:
                schema = configured
        if schema is MISSING:
            schema = self.roots["urn:kora:artifact:StructuredOutput"]
        return InputView(schema, SchemaView.STRUCTURED_OUTPUT)

    def output_view(self, node: Producer, output: OutputDecl) -> InputView | None:
        self.source_steps += 1
        contract = self.contracts.get(output.type)
        if contract is None:
            return None
        if isinstance(node, AgentNode) and not self.agent_roles(node):
            return None
        if not isinstance(contract, BuiltinTypeContract):
            if (
                "/" in output.type
                and contract.parent is None
                and output.schema_ is MISSING
            ):
                return InputView(contract.schema_)
            return None
        if contract.builtin_revision != "1":
            return None
        if output.type == "StructuredOutput":
            return self.structured(node, output)
        if output.schema_ is not MISSING:
            return None
        if output.type == "AgentSession":
            if self.session_schema is None:
                resource = files("kora.ir").joinpath(
                    "artifact_schemas", "AgentSession.cel.schema.json"
                )
                value: object = json.loads(resource.read_text(encoding="utf-8"))
                self.session_schema = validate_ir_json_object(value)
            return InputView(self.session_schema)
        if output.type in {"VcsRef", "CommandResult"}:
            return InputView(self.roots["urn:kora:artifact:" + output.type])
        return None

    def source(self, body: GraphBody, binding: InputRef) -> InputView | None:
        if (
            binding.from_ is MISSING
            or binding.output is MISSING
            or binding.path is not MISSING
            or binding.from_.startswith("$")
        ):
            return None
        producer = body.nodes.get(binding.from_)
        if not isinstance(
            producer, AgentNode | CommandNode | HumanInputNode | WaitEventNode
        ):
            return None
        output = producer.outputs.get(binding.output)
        if (
            output is None
            or output.type != binding.type
            or output.collect is not MISSING
        ):
            return None
        key = (id(body), binding.from_, binding.output)
        if key not in self.sources:
            self.sources[key] = self.output_view(producer, output)
        return self.sources[key]

    def mapped(self, source: InputView) -> Variable | InputFailure:
        key = (id(source.schema), source.view)
        if key not in self.variables:
            try:
                self.variables[key] = self.schemas.variable(source.schema, source.view)
            except SchemaError:
                self.variables[key] = InputFailure.SCHEMA
        return self.variables[key]

    def context(self, body: GraphBody, node: HumanInputNode) -> Variable | InputFailure:
        inputs = {} if node.inputs is MISSING else node.inputs
        sources: dict[str, InputView] = {}
        for name, binding in inputs.items():
            source = self.source(body, binding)
            if source is None:
                return InputFailure.CONTEXT
            sources[name] = source
        fields: dict[str, RecordField] = {}
        bounds: dict[str, ValueBound] = {}
        for name, source in sources.items():
            variable = self.mapped(source)
            if isinstance(variable, InputFailure):
                return variable
            fields[name] = RecordField(variable.type, inputs[name].optional is not True)
            bounds[name] = variable.bound
        return Variable(Record(fields, extra=None), replace(DATA_BOUND, fields=bounds))
