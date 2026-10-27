from dataclasses import replace
from pathlib import Path

from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.policy import EditCall, PolicyCalls, PolicyConformanceInputs
from kora.ir import ToolPolicy
from kora.ir.json import JsonObject
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.policy import Policy, PolicyLayers, ToolCallRequest
from kora.protocols.vcs import WorkspaceRef

WORKSPACE: WorkspaceRef = WorkspaceRef(
    attempt_id="fixture",
    root=str(Path.cwd()),
    repo="example/repo",
    commit="c0ffee",
    base_commit="c0ffee",
    branch=None,
)
EDIT_FIELDS: dict[str, str] = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def call(tool: str, field: str, value: str) -> ToolCallRequest:
    return ToolCallRequest(
        harness="claude",
        tool=tool,
        tool_use_id="fixture-call",
        agent_id=None,
        arguments={field: value},
        workspace=WORKSPACE,
        node_path="node",
    )


def edit(tool: str, field: str) -> EditCall:
    return EditCall(
        tool=tool,
        pattern=tool,
        build=lambda path: call(tool, field, path),
        naming=lambda path: f"{tool}({path})",
    )


def calls() -> PolicyCalls:
    return PolicyCalls(
        shell=call("Bash", "command", "git status"),
        shell_other=call("Bash", "command", "ls"),
        matching="Bash(git *)",
        narrow="Bash(git status)",
        other="Bash(rm *)",
        edits=tuple(edit(tool, field) for tool, field in EDIT_FIELDS.items()),
        malformed={"allow": ["Bash(git *"]},
    )


async def failure(
    provider: Policy, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    request = replace(calls().shell, harness="unsupported", arguments={"input": marker})
    layers = PolicyLayers(ToolPolicy(), ToolPolicy())
    return await provider.decide(request, layers, (), context)


async def effect(
    provider: Policy, config: JsonObject, context: ProtocolContext
) -> None:
    assert provider.validate(config) == []
    configured = ToolPolicy.model_validate(config)
    result = await provider.decide(
        calls().shell, PolicyLayers(configured, ToolPolicy()), (), context
    )
    assert not result.allow


class Adapter:
    exercises: PolicyConformanceInputs = PolicyConformanceInputs(
        failure, calls(), effect
    )

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        (Path.cwd() / "src").mkdir(mode=0o700)
        (Path.cwd() / "tests").mkdir(mode=0o700)
        (Path.cwd() / "tests" / "unit").mkdir(mode=0o700)
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        return ConstructorArguments()

    async def cleanup(self, instance: object | None) -> None:
        assert instance is None or isinstance(instance, AllowDenyPolicy)


def configured() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.POLICY,
        "configured",
        AllowDenyPolicy,
        AllowDenyPolicy.config_schema,
        "1",
    )
