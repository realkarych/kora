from collections.abc import AsyncGenerator
from copy import deepcopy
from typing import Literal

from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.harness import (
    HarnessCase,
    HarnessConformanceInputs,
    HarnessHost,
    run_harness_case,
)
from kora.ir import ToolPolicy
from kora.ir.json import JsonObject
from kora.plugins.harness.fake import CallScope, FakeHarness, ListedScript, ScriptHeader
from kora.plugins.harness.fake.script import STEP_ADAPTER, EventStep, Step
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import HarnessAdapter, SessionSpec
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.policy import PolicyLayers
from kora.protocols.vcs import WorkspaceRef, WorkspaceTarget


def turn(name: str) -> JsonObject:
    return {
        "kind": "event",
        "event": "turn",
        "data": {"message_id": name, "truncated": False},
    }


def tool(name: str, identifier: str) -> JsonObject:
    return {
        "kind": "tool_call",
        "tool": name,
        "tool_use_id": identifier,
        "agent_id": None,
        "input_shape": {"command": "true"},
        "outcome": {"is_error": False, "content": "done"},
    }


def output(source: Literal["structured_output", "result_text"]) -> JsonObject:
    return {"kind": "output", "source": source, "value": {"answer": 42}}


def script(case: HarnessCase | None) -> ListedScript:
    result: JsonObject = {"kind": "result", "status": "success"}
    if case is None:
        documents = [turn("failure")]
    elif case is HarnessCase.TOOL_CALLS:
        documents = [turn("m1"), tool("Bash", "one"), tool("Forbidden", "two"), result]
    elif case is HarnessCase.MAX_EVENTS:
        documents = [
            turn("m1"),
            turn("m2"),
            tool("Bash", "one"),
            tool("Bash", "two"),
            turn("m3"),
            result,
        ]
    elif case is HarnessCase.USAGE:
        documents = [
            turn("m1"),
            {
                "kind": "usage",
                "closes": True,
                "data": {
                    "observation_id": "usage-1",
                    "observation_sequence": 1,
                    "observed_at": "2026-01-01T00:00:00Z",
                    "message_id": "m1",
                    "model": "fixture",
                    "input_tokens": 11,
                    "cached_input_tokens": 0,
                    "cache_creation_tokens": 0,
                    "output_tokens": 7,
                    "reasoning_tokens": 0,
                    "known_token_fields": [
                        "input_tokens",
                        "cached_input_tokens",
                        "cache_creation_tokens",
                        "output_tokens",
                        "reasoning_tokens",
                    ],
                    "basis": "price_table",
                    "billing_class": "api_key",
                    "coverage": {
                        "version": 1,
                        "observation_id": "coverage-1",
                        "observation_sequence": 1,
                        "completeness": "complete",
                        "observed_at": "2026-01-01T00:00:00Z",
                        "attribution_gap": False,
                        "observation_gap": False,
                    },
                },
            },
            result,
        ]
    elif case is HarnessCase.STRUCTURED_OUTPUT:
        documents = [
            turn("m1"),
            {**tool("StructuredOutput", "output"), "outcome": None},
            output("structured_output"),
            result,
        ]
    elif case is HarnessCase.RESULT_TEXT:
        documents = [turn("m1"), output("result_text"), result]
    else:
        documents = [turn("m1"), result]
    return ListedScript(
        ScriptHeader(end="completed", on_interrupt="acknowledge"),
        tuple(STEP_ADAPTER.validate_python(d) for d in documents),
    )


class Source:
    def __init__(self) -> None:
        self.script = script(HarnessCase.NO_USAGE)
        self.header = self.script.header
        self.prefix = ""

    async def steps(self) -> AsyncGenerator[Step]:
        async for step in self.script.steps():
            if isinstance(step, EventStep) and step.event == "turn":
                yield step.model_copy(
                    update={
                        "data": {
                            **step.data,
                            "message_id": self.prefix + str(step.data["message_id"]),
                        }
                    }
                )
            else:
                yield step


class ConfiguredHarness(FakeHarness):
    config_schema: JsonObject = {
        "type": "object",
        "properties": {"prefix": {"type": "string"}},
        "required": ["prefix"],
    }

    def __init__(self) -> None:
        self.script_source = Source()
        self.scope_value = CallScope(
            harness="claude",
            workspace=WorkspaceRef(
                attempt_id="fixture",
                root="/workspace/fixture",
                repo="example/repo",
                commit="c0ffee",
                base_commit="c0ffee",
                branch=None,
            ),
            node_path="node",
            layers=PolicyLayers(ToolPolicy(), ToolPolicy()),
            protected_paths=(),
            output_format=True,
        )

    def select(self, case: HarnessCase | None, context: ProtocolContext) -> None:
        self.script_source.script = script(case)
        super().__init__(self.script_source, self.scope_value, context)

    def build_session_spec(
        self, config: JsonObject, workspace: WorkspaceTarget, feedback: None
    ) -> SessionSpec:
        prefix = config["prefix"]
        assert isinstance(prefix, str)
        self.script_source.prefix = prefix
        return (
            super()
            .build_session_spec(config, workspace, feedback)
            .model_copy(update={"options": deepcopy(config)})
        )


async def failure(
    provider: HarnessAdapter, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    assert isinstance(provider, ConfiguredHarness)
    provider.select(None, context)
    provider.script_source.script = ListedScript(
        ScriptHeader(end="completed", on_interrupt="acknowledge"),
        (
            STEP_ADAPTER.validate_python(
                {
                    "kind": "event",
                    "event": "turn",
                    "data": {
                        "message_id": "failure",
                        "truncated": False,
                        "text": marker,
                    },
                }
            ),
        ),
    )
    return await HarnessHost().hosted(provider, "Forbidden", None, config)


async def prepare_case(
    provider: HarnessAdapter,
    config: JsonObject,
    case: HarnessCase,
    context: ProtocolContext,
) -> None:
    assert isinstance(provider, ConfiguredHarness)
    provider.select(case, context)


async def effect(
    provider: HarnessAdapter, config: JsonObject, context: ProtocolContext
) -> None:
    result = await run_harness_case(
        HarnessHost(), provider, config, context, INPUTS, HarnessCase.NO_USAGE
    )
    assert any(
        event.kind == "turn" and event.data.message_id == str(config["prefix"]) + "m1"
        for event in result.events
    )


INPUTS: HarnessConformanceInputs = HarnessConformanceInputs(
    failure, prepare_case, "Forbidden", effect
)


class Adapter:
    exercises: HarnessConformanceInputs = INPUTS

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        return ConstructorArguments()

    async def cleanup(self, instance: object | None) -> None:
        assert instance is None or isinstance(instance, ConfiguredHarness)


def configured() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.HARNESS,
        "configured",
        ConfiguredHarness,
        ConfiguredHarness.config_schema,
        "1",
    )
