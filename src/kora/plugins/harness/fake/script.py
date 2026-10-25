from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from typing import Annotated, Final, Literal, Protocol

from pydantic import (
    ConfigDict,
    Discriminator,
    Field,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    WithJsonSchema,
)

from kora.ir.json import JsonObjectField
from kora.protocols.common import BoundaryModel
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import BoundedJsonValue, BoundedText, UsageData
from kora.protocols.identity import Identifier

MAX_STEPS: Final[int] = 100_001
MAX_CHANGE_FILE_BYTES: Final[int] = 1_048_576
MAX_CHANGE_SCRIPT_BYTES: Final[int] = 64 * 1_048_576
MAX_CHANGE_FILES: Final[int] = 1_024
ChangeContent = Annotated[
    str, StringConstraints(strict=True, max_length=MAX_CHANGE_FILE_BYTES)
]
ChangePath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=4_096),
]


class ToolOutcome(BoundaryModel):
    is_error: StrictBool
    content: BoundedText | None


class EventStep(BoundaryModel):
    kind: Literal["event"]
    event: Literal["turn", "provider_limited", "provider_retrying", "error"]
    data: JsonObjectField


class ToolCallStep(BoundaryModel):
    kind: Literal["tool_call"]
    tool: Identifier
    tool_use_id: Identifier
    agent_id: Identifier | None
    input_shape: Annotated[object, WithJsonSchema({"type": "object"})]
    outcome: ToolOutcome | None


class UndecidedToolResultStep(BoundaryModel):
    kind: Literal["undecided_tool_result"]
    tool: Identifier
    tool_use_id: Identifier
    is_error: StrictBool


class HarnessDenialStep(BoundaryModel):
    kind: Literal["harness_denial"]
    tool: Identifier
    tool_use_id: Identifier


class UsageStep(BoundaryModel):
    kind: Literal["usage"]
    data: UsageData
    closes: StrictBool


class OutputStep(BoundaryModel):
    kind: Literal["output"]
    source: Literal["structured_output", "result_text"]
    value: BoundedJsonValue


class ChangeStep(BoundaryModel):
    kind: Literal["change"]
    files: dict[ChangePath, ChangeContent] = Field(max_length=MAX_CHANGE_FILES)


class PauseStep(BoundaryModel):
    kind: Literal["pause"]


class ResultStep(BoundaryModel):
    kind: Literal["result"]
    status: Literal["success", "error"]


Step = Annotated[
    EventStep
    | ToolCallStep
    | UndecidedToolResultStep
    | HarnessDenialStep
    | UsageStep
    | OutputStep
    | ChangeStep
    | PauseStep
    | ResultStep,
    Discriminator("kind"),
]


STEP_ADAPTER: Final[TypeAdapter[Step]] = TypeAdapter(
    Step, config=ConfigDict(hide_input_in_errors=True)
)


class ScriptHeader(BoundaryModel):
    end: Literal["completed", "cli_exit_without_result", "oversized_message"]
    on_interrupt: Literal["acknowledge", "ignore"]


class ScriptSource(Protocol):
    header: ScriptHeader

    def steps(self) -> AsyncGenerator[Step]: ...


class ListedScript:
    def __init__(self, header: ScriptHeader, steps: Sequence[Step]) -> None:
        self.header = header
        self.listed = tuple(steps)

    async def steps(self) -> AsyncGenerator[Step]:
        for step in self.listed:
            yield step


class ScriptRefused(KoraProtocolError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, False, {})


def change_bytes(step: ChangeStep) -> int:
    total = 0
    for content in step.files.values():
        size = len(content.encode("utf-8"))
        if size > MAX_CHANGE_FILE_BYTES:
            raise ScriptRefused(
                "script_change_too_large", "a changed file is too large"
            )
        total += size
    return total


class BoundedSteps:
    def __init__(self, source: ScriptSource) -> None:
        self.source = source
        self.consumed = 0
        self.changed = 0

    async def steps(self) -> AsyncGenerator[Step]:
        last: Step | None = None
        result_seen = False
        async with aclosing(self.source.steps()) as steps:
            async for step in steps:
                self.consumed += 1
                self.check(step)
                result_seen = result_seen or isinstance(step, ResultStep)
                last = step
                yield step
        if self.source.header.end == "completed":
            contradicts = not isinstance(last, ResultStep)
        else:
            contradicts = result_seen
        if contradicts:
            raise ScriptRefused(
                "script_end_invalid", "the script end contradicts its steps"
            )

    def check(self, step: Step) -> None:
        if self.consumed > MAX_STEPS:
            raise ScriptRefused("script_too_long", "the script has too many steps")
        if isinstance(step, ChangeStep):
            self.changed += change_bytes(step)
            if self.changed > MAX_CHANGE_SCRIPT_BYTES:
                raise ScriptRefused(
                    "script_change_too_large", "the script changes too much"
                )
