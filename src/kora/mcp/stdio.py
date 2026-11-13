import asyncio
from pathlib import Path
from typing import cast

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from kora.client import ApiError, ClientError, KoraClient
from kora.client.documents import GraphDocumentError, read_graph_deployment
from kora.client.settings import ClientSettings
from kora.ir.json import JsonObject
from kora.mcp.operating import OperatingTools
from kora.mcp.results import MCP_COMPONENT, TOOL_FINISHED, tool_result, usage_error
from kora.observability import ProcessDiagnostics

DEPLOY_TOOL = "kora_deploy"
DEPLOY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path of a compiled graph document in JSON.",
        }
    },
    "required": ["path"],
    "additionalProperties": False,
}
DEPLOY_DESCRIPTION = (
    "Register a graph version from a compiled graph document without command "
    "files. Returns the API response with the version identity and digests; an "
    "equal document returns the existing version with reused true."
)


class AuthoringTools:
    def __init__(self, client: KoraClient, diagnostics: ProcessDiagnostics) -> None:
        self.client = client
        self.logger = diagnostics.logger(MCP_COMPONENT)

    def finished(self, outcome: str, code: str) -> None:
        self.logger.emit(TOOL_FINISHED, tool=DEPLOY_TOOL, outcome=outcome, code=code)

    async def list_tools(
        self,
        context: ServerRequestContext[object],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=DEPLOY_TOOL,
                    description=DEPLOY_DESCRIPTION,
                    input_schema=DEPLOY_SCHEMA,
                )
            ]
        )

    async def call_tool(
        self,
        context: ServerRequestContext[object],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        path: object = arguments.get("path")
        if params.name != DEPLOY_TOOL or set(arguments) != {"path"}:
            return usage_error(f"Call {DEPLOY_TOOL} with exactly one path argument.")
        if not isinstance(path, str) or not path:
            return usage_error("The path argument must be a nonempty string.")
        try:
            deployment = await asyncio.to_thread(read_graph_deployment, Path(path))
            deployed = await self.client.deploy(
                deployment.namespace, deployment.request
            )
        except GraphDocumentError as error:
            self.finished("document_error", error.problem.value)
            return usage_error(f"The graph document is invalid: {error.problem.value}.")
        except ApiError as error:
            self.finished("api_error", error.code)
            return tool_result(error.body, is_error=True)
        except ClientError as error:
            self.finished("client_error", error.code)
            return tool_result(cast("JsonObject", error.envelope()), is_error=True)
        self.finished("ok", "none")
        return tool_result(deployed.document)


SERVER_NAMES: dict[str, str] = {
    "authoring": "kora-authoring",
    "operating": "kora-operating",
}


async def serve_stdio(
    server_name: str, settings: ClientSettings, diagnostics: ProcessDiagnostics
) -> None:
    async with KoraClient(
        settings.url, settings.token, diagnostics=diagnostics
    ) as client:
        tools = (
            AuthoringTools(client, diagnostics)
            if server_name == "authoring"
            else OperatingTools(client, diagnostics)
        )
        server: Server[object] = Server(
            SERVER_NAMES[server_name],
            on_list_tools=tools.list_tools,
            on_call_tool=tools.call_tool,
        )
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
