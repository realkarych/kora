from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    MISSING,
    AfterValidator,
    BeforeValidator,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    StrictBool,
    StrictStr,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from kora.ir.config import (
    AgentConfig,
    BranchConfig,
    CommandConfig,
    FanOutConfig,
    HumanInputConfig,
    NodeLimits,
    Retry,
    Strings,
    SubgraphNodeConfig,
    TimerConfig,
    WaitEventConfig,
    environment_schema,
)
from kora.ir.data import IrJsonObjectField
from kora.ir.declarations import (
    InputRef,
    IrSourceLocation,
    OutputDecl,
    SubgraphOutput,
    TargetConfig,
)
from kora.ir.record import IrRecord, require_array
from kora.ir.scalars import NodeId
from kora.ir.trigger import Trigger


def unique_dependencies(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise PydanticCustomError("ir_dependencies_unique", "dependencies repeat")
    return values


NodeNames = Annotated[tuple[NodeId, ...], BeforeValidator(require_array)]
Inputs = Annotated[dict[NodeId, InputRef], Field(json_schema_extra=environment_schema)]
Outputs = Annotated[
    dict[NodeId, OutputDecl], Field(json_schema_extra=environment_schema)
]


@dataclass(frozen=True)
class OutputRole:
    name: str
    type_name: str
    exclusive: bool

    def validate(self, outputs: dict[str, OutputDecl]) -> dict[str, OutputDecl]:
        role = outputs.get(self.name)
        if role is None or role.type != self.type_name:
            raise PydanticCustomError(
                "ir_output_role", "the fixed output role is invalid"
            )
        for name, output in outputs.items():
            if name != self.name and (self.exclusive or output.type == self.type_name):
                raise PydanticCustomError(
                    "ir_output_role", "the fixed output role is invalid"
                )
        return outputs

    def __get_pydantic_core_schema__(
        self, source: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            self.validate, handler(source)
        )

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        result = handler(schema)
        environment_schema(result)
        declaration = result["additionalProperties"]
        result["required"] = [self.name]
        result["properties"] = {
            self.name: {
                "allOf": [
                    declaration,
                    {"properties": {"type": {"const": self.type_name}}},
                ]
            }
        }
        additional = result.get("additionalProperties", {})
        if self.exclusive:
            result["additionalProperties"] = False
        else:
            result["additionalProperties"] = {
                "allOf": [
                    additional,
                    {"properties": {"type": {"not": {"const": self.type_name}}}},
                ]
            }
        return result


class NodeEnvelope(IrRecord):
    description: StrictStr | MISSING = MISSING
    source: IrSourceLocation | MISSING = MISSING
    inputs: Inputs | MISSING = MISSING
    depends_on: (
        Annotated[
            NodeNames,
            AfterValidator(unique_dependencies),
            Field(json_schema_extra={"uniqueItems": True}),
        ]
        | MISSING
    ) = MISSING
    trigger_rule: (
        Literal[
            "all_success",
            "all_done",
            "one_success",
            "one_failed",
            "none_failed",
            "all_skipped",
            "none_failed_min_one_success",
        ]
        | MISSING
    ) = MISSING
    retry: Retry | MISSING = MISSING
    limits: NodeLimits | MISSING = MISSING
    attempt_invariant: StrictBool | MISSING = MISSING
    independent_of: NodeNames | MISSING = MISSING
    protected_paths: Strings | MISSING = Field(
        default=MISSING,
        description=(
            "node-level addition to runtime.protected_paths (DD-34), "
            "path globs in the syntax of ir.md section 4.1"
        ),
    )
    preset: StrictStr | MISSING = MISSING


class AgentNode(NodeEnvelope):
    kind: Literal["agent"]
    outputs: Annotated[
        dict[NodeId, OutputDecl], OutputRole("session", "AgentSession", False)
    ]
    config: AgentConfig


class CommandNode(NodeEnvelope):
    kind: Literal["command"]
    outputs: Annotated[
        dict[NodeId, OutputDecl], OutputRole("result", "CommandResult", False)
    ]
    config: CommandConfig


class HumanInputNode(NodeEnvelope):
    kind: Literal["human_input"]
    outputs: Annotated[
        dict[NodeId, OutputDecl], OutputRole("response", "StructuredOutput", True)
    ]
    config: HumanInputConfig


class WaitEventNode(NodeEnvelope):
    kind: Literal["wait_event"]
    outputs: Annotated[
        dict[NodeId, OutputDecl], OutputRole("event", "StructuredOutput", True)
    ]
    config: WaitEventConfig


class TimerNode(NodeEnvelope):
    outputs: Outputs | MISSING = MISSING
    kind: Literal["timer"]
    config: TimerConfig


class BranchNode(NodeEnvelope):
    outputs: Outputs | MISSING = MISSING
    kind: Literal["branch"]
    config: BranchConfig


class FanOutNode(NodeEnvelope):
    outputs: Outputs | MISSING = MISSING
    kind: Literal["fan_out"]
    config: FanOutConfig


class SubgraphNode(NodeEnvelope):
    outputs: Outputs | MISSING = MISSING
    kind: Literal["subgraph"]
    config: SubgraphNodeConfig


Node = Annotated[
    AgentNode
    | CommandNode
    | HumanInputNode
    | WaitEventNode
    | TimerNode
    | BranchNode
    | FanOutNode
    | SubgraphNode,
    Field(discriminator="kind"),
]


class GraphBody(IrRecord):
    description: StrictStr | MISSING = MISSING
    nodes: Annotated[
        dict[NodeId, Node], Field(min_length=1, json_schema_extra=environment_schema)
    ]


class Subgraph(GraphBody):
    inputs: (
        Annotated[dict[NodeId, OutputDecl], Field(json_schema_extra=environment_schema)]
        | MISSING
    ) = MISSING
    outputs: (
        Annotated[
            dict[NodeId, SubgraphOutput], Field(json_schema_extra=environment_schema)
        ]
        | MISSING
    ) = MISSING
    retry: Retry | MISSING = MISSING
    limits: NodeLimits | MISSING = MISSING
    source: IrSourceLocation | MISSING = MISSING


class Graph(GraphBody):
    id: NodeId
    version: StrictStr | MISSING = MISSING
    namespace: NodeId
    params: IrJsonObjectField | MISSING = MISSING
    triggers: (
        Annotated[tuple[Trigger, ...], BeforeValidator(require_array)] | MISSING
    ) = MISSING
    overlap: Literal["allow", "skip", "queue"] | MISSING = MISSING
    target: TargetConfig | MISSING = MISSING
    subgraphs: (
        Annotated[dict[NodeId, Subgraph], Field(json_schema_extra=environment_schema)]
        | MISSING
    ) = MISSING
    source: IrSourceLocation | MISSING = MISSING
