from typing import cast

from mcp import types
from mcp.server import ServerRequestContext
from pydantic import ValidationError

from kora.client import ApiError, ClientError, KoraClient
from kora.client.transport import IDEMPOTENCY_KEY
from kora.ir.json import JsonObject
from kora.mcp.results import MCP_COMPONENT, TOOL_FINISHED, tool_result, usage_error
from kora.models.graphs import OpaqueObject
from kora.models.runs import TriggerRequest
from kora.observability import ProcessDiagnostics

TRIGGER_TOOL = "kora_trigger"
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
    },
    "required": ["namespace", "graph"],
    "additionalProperties": False,
}
TRIGGER_DESCRIPTION = (
    "Start a deployed graph without parameters. Returns at once the accepted "
    "pending run or a saved overlap skip; a refusal is a tool error with the "
    "API error object."
)
OPTIONAL = frozenset({"graph_version", "idempotency_key", "allow_unenforced_secrets"})


class OperatingTools:
    def __init__(self, client: KoraClient, diagnostics: ProcessDiagnostics) -> None:
        self.client = client
        self.logger = diagnostics.logger(MCP_COMPONENT)

    def finished(self, outcome: str, code: str) -> None:
        self.logger.emit(TOOL_FINISHED, tool=TRIGGER_TOOL, outcome=outcome, code=code)

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
                )
            ]
        )

    async def call_tool(
        self,
        context: ServerRequestContext[object],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
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
                params=OpaqueObject(),
                graph_version=arguments.get("graph_version"),
                allow_unenforced_secrets=arguments.get(
                    "allow_unenforced_secrets", False
                ),
            )
        except ValidationError:
            return usage_error("The graph version or the secrets flag is invalid.")
        try:
            triggered = await self.client.trigger(namespace, graph, request, key)
        except ApiError as error:
            self.finished("api_error", error.code)
            return tool_result(error.body, is_error=True)
        except ClientError as error:
            self.finished("client_error", error.code)
            return tool_result(cast("JsonObject", error.envelope()), is_error=True)
        self.finished("ok", "none")
        return tool_result(triggered.document)
