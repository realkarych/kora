from collections.abc import Iterator, Mapping

from pydantic import MISSING

from kora.cel.diagnostics import DiagnosticError
from kora.cel.paths import Field, parse_path
from kora.ir.declarations import InputRef
from kora.ir.topology import CommandNode, HumanInputNode, Node

Path = tuple[str | int, ...]


def _context_is_supported(context: str, inputs: Mapping[str, InputRef]) -> bool:
    try:
        steps = parse_path(context).steps
    except DiagnosticError:
        return True
    names_declared_input = (
        len(steps) >= 2
        and steps[0] == Field("inputs")
        and isinstance(steps[1], Field)
        and steps[1].name in inputs
    )
    return not names_declared_input or len(steps) == 2


def cli_approval_installation_findings(
    node: HumanInputNode, nodes: Mapping[str, Node]
) -> Iterator[tuple[Path, str]]:
    inputs = {} if node.inputs is MISSING else node.inputs
    if len(inputs) != 1:
        yield ("inputs",), "CLI approval requires exactly one input."
    else:
        name, reference = next(iter(inputs.items()))
        if (
            reference.type != "CommandResult"
            or reference.from_ is MISSING
            or not isinstance(nodes.get(reference.from_), CommandNode)
            or reference.output != "result"
            or reference.path is not MISSING
            or reference.optional is True
        ):
            yield ("inputs", name), "CLI approval requires a direct command result."
    config = node.config
    if config.context is not MISSING and not _context_is_supported(
        config.context, inputs
    ):
        yield (("config", "context"), "CLI approval requires its exact input context.")
    if config.channel != "cli":
        yield (
            ("config", "channel"),
            "Only CLI approval is available in this installation.",
        )
    if config.binds is not MISSING:
        yield ("config", "binds"), "Bound approval is unavailable in this installation."
