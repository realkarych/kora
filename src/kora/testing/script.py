from typing import Annotated

from pydantic import Field, ValidationError

from kora.plugins.harness.fake.script import (
    MAX_STEPS,
    ListedScript,
    PauseStep,
    ScriptHeader,
    Step,
)
from kora.plugins.runtime.local.agent import SCRIPT_LIMIT
from kora.plugins.runtime.local.channel import delivery_error


class FakeScript(ScriptHeader):
    steps: Annotated[tuple[Step, ...], Field(max_length=MAX_STEPS)]

    def listed(self) -> ListedScript:
        return ListedScript(
            ScriptHeader(end=self.end, on_interrupt=self.on_interrupt), self.steps
        )

    def require_real_time(self) -> None:
        if any(isinstance(step, PauseStep) for step in self.steps):
            raise delivery_error("requires_virtual_time")

    def serialise(self) -> bytes:
        self.require_real_time()
        buffer = self.model_dump_json().encode("utf-8")
        if len(buffer) > SCRIPT_LIMIT:
            raise delivery_error("transfer_refused")
        return buffer


class ScriptIntake:
    def __init__(self) -> None:
        self.script: ListedScript | None = None

    def receive(self, buffer: bytes) -> None:
        self._parse(buffer).require_real_time()

    def verified(self, buffer: bytes) -> None:
        parsed = self._parse(buffer)
        parsed.require_real_time()
        self.script = parsed.listed()

    def _parse(self, buffer: bytes) -> FakeScript:
        try:
            return FakeScript.model_validate_json(buffer)
        except ValidationError:
            raise delivery_error("transfer_refused") from None
