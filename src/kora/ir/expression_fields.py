from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import MISSING

from kora.ir.config import Retry
from kora.ir.declarations import OutputTarget
from kora.ir.topology import (
    AgentNode,
    BranchNode,
    CommandNode,
    Graph,
    HumanInputNode,
    Node,
    WaitEventNode,
)
from kora.ir.trigger import SensorTrigger

FieldPath = tuple[str | int, ...]


@dataclass(frozen=True)
class SyntaxField:
    text: str
    path: FieldPath
    is_path: bool = False


def authored_field(
    value: str | MISSING, path: FieldPath, *, is_path: bool = False
) -> Iterator[SyntaxField]:
    if value is not MISSING:
        yield SyntaxField(value, path, is_path)


def retry_fields(
    retry: Retry | MISSING, prefix: FieldPath = ("retry",)
) -> Iterator[SyntaxField]:
    if retry is not MISSING:
        yield from authored_field(retry.predicate, (*prefix, "predicate"))


def node_fields(node: Node) -> Iterator[SyntaxField]:
    yield from retry_fields(node.retry)
    if node.inputs is not MISSING:
        for name, value in node.inputs.items():
            yield from authored_field(
                value.path, ("inputs", name, "path"), is_path=True
            )
    if isinstance(node, AgentNode | CommandNode):
        yield from authored_field(
            node.config.workspace.from_ref,
            ("config", "workspace", "from_ref"),
            is_path=True,
        )
    elif isinstance(node, BranchNode):
        for index, case in enumerate(node.config.cases):
            yield SyntaxField(case.when, ("config", "cases", index, "when"))
            yield from authored_field(case.reason, ("config", "cases", index, "reason"))
        yield from authored_field(node.config.else_reason, ("config", "else_reason"))
    elif isinstance(node, HumanInputNode):
        yield from authored_field(node.config.approve_when, ("config", "approve_when"))
        yield from authored_field(
            node.config.context, ("config", "context"), is_path=True
        )
        binds = node.config.binds
        if binds is not MISSING:
            yield from authored_field(
                binds.vcs_ref, ("config", "binds", "vcs_ref"), is_path=True
            )
            if binds.gate_digests is not MISSING:
                for index, value in enumerate(binds.gate_digests):
                    yield SyntaxField(
                        value, ("config", "binds", "gate_digests", index), True
                    )
    elif isinstance(node, WaitEventNode):
        event = node.config.event
        if event is not MISSING:
            yield from authored_field(event.match, ("config", "event", "match"))
        poll = node.config.poll
        if poll is not MISSING:
            yield SyntaxField(poll.until, ("config", "poll", "until"))


def graph_fields(graph: Graph) -> Iterator[SyntaxField]:
    if graph.triggers is not MISSING:
        for index, trigger in enumerate(graph.triggers):
            if isinstance(trigger, SensorTrigger):
                yield SyntaxField(
                    trigger.poll.until, ("triggers", index, "poll", "until")
                )
    target = graph.target
    if target is not MISSING and isinstance(target.key, OutputTarget):
        yield SyntaxField(target.key.path, ("target", "key", "path"), True)
