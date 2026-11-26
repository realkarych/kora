from collections.abc import Awaitable
from typing import Literal, cast

from mcp import types
from mcp.server import ServerRequestContext
from pydantic import StrictInt, StrictStr, ValidationError

from kora.client import ApiError, ClientError, KoraClient
from kora.client.transport import IDEMPOTENCY_KEY, ListedRunEvents, TriggeredRun
from kora.ir.json import JsonObject
from kora.mcp.results import MCP_COMPONENT, TOOL_FINISHED, tool_result, usage_error
from kora.models.errors import WireRecord
from kora.models.runs import TriggerRequest
from kora.observability import ProcessDiagnostics

TRIGGER_TOOL = "kora_trigger"
RUNS_TOOL = "kora_runs"
TRIGGER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "namespace": {"type": "string", "description": "Namespace of the graph."},
        "graph": {"type": "string", "description": "Name of the deployed graph."},
        "graph_version": {
            "type": "string",
            "description": "A gv_ version identifier; the latest version if omitted.",
        },
        "idempotency_key": {
            "type": "string",
            "description": "Key that makes a repeated start return the saved outcome.",
        },
        "allow_unenforced_secrets": {"type": "boolean"},
        "params": {
            "type": "object",
            "description": "Original graph parameters; defaults to an empty object.",
        },
    },
    "required": ["namespace", "graph"],
    "additionalProperties": False,
}
TRIGGER_DESCRIPTION = (
    "Start a deployed graph with validated parameters. Returns at once the accepted "
    "pending run or a saved overlap skip; a refusal is a tool error with the "
    "API error object."
)
OPTIONAL = frozenset(
    {"graph_version", "idempotency_key", "allow_unenforced_secrets", "params"}
)


class EventArguments(WireRecord):
    mode: Literal["events"]
    namespace: StrictStr
    run_id: StrictStr
    limit: StrictInt | None = None
    cursor: StrictStr | None = None


class OperatingTools:
    def __init__(self, client: KoraClient, diagnostics: ProcessDiagnostics) -> None:
        self.client = client
        self.logger = diagnostics.logger(MCP_COMPONENT)

    def finished(self, tool: str, outcome: str, code: str) -> None:
        self.logger.emit(TOOL_FINISHED, tool=tool, outcome=outcome, code=code)

    async def list_tools(
        self,
        context: ServerRequestContext[object],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=TRIGGER_TOOL,
                    description=TRIGGER_DESCRIPTION,
                    input_schema=TRIGGER_SCHEMA,
                ),
                types.Tool(
                    name=RUNS_TOOL,
                    description="Read one finite page of a run’s saved events.",
                    input_schema=EventArguments.model_json_schema(),
                ),
            ]
        )

    async def call_tool(
        self,
        context: ServerRequestContext[object],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        if params.name == RUNS_TOOL:
            try:
                events = EventArguments.model_validate(arguments)
            except ValidationError:
                return usage_error(
                    "Call kora_runs in events mode with a namespace and run ID."
                )
            return await self.invoke(
                RUNS_TOOL,
                self.client.list_run_events(
                    events.namespace,
                    events.run_id,
                    limit=None if events.limit is None else str(events.limit),
                    cursor=events.cursor,
                ),
            )
        namespace, graph = arguments.get("namespace"), arguments.get("graph")
        key = arguments.get("idempotency_key")
        if params.name != TRIGGER_TOOL or (set(arguments) - OPTIONAL) != {
            "namespace",
            "graph",
        }:
            return usage_error(f"Call {TRIGGER_TOOL} with a namespace and a graph.")
        if not isinstance(namespace, str) or not isinstance(graph, str):
            return usage_error("The namespace and graph arguments must be strings.")
        if key is not None and (
            not isinstance(key, str) or IDEMPOTENCY_KEY.fullmatch(key) is None
        ):
            return usage_error("The idempotency key must be 1-255 visible ASCII.")
        try:
            request = TriggerRequest(
                params=arguments.get("params", {}),
                graph_version=arguments.get("graph_version"),
                allow_unenforced_secrets=arguments.get(
                    "allow_unenforced_secrets", False
                ),
            )
        except ValidationError:
            return usage_error("The trigger arguments are invalid.")
        return await self.invoke(
            TRIGGER_TOOL, self.client.trigger(namespace, graph, request, key)
        )

    async def invoke(
        self, tool: str, request: Awaitable[ListedRunEvents | TriggeredRun]
    ) -> types.CallToolResult:
        try:
            result = await request
        except ApiError as error:
            self.finished(tool, "api_error", error.code)
            return tool_result(error.body, is_error=True)
        except ClientError as error:
            self.finished(tool, "client_error", error.code)
            return tool_result(cast("JsonObject", error.envelope()), is_error=True)
        self.finished(tool, "ok", "none")
        return tool_result(result.document)
