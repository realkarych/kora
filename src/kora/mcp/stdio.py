import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

from anyio import CapacityLimiter, to_thread
from anyio.lowlevel import checkpoint_if_cancelled
from mcp import MCPError, types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from kora.client import ApiError, ClientError, KoraClient
from kora.client.documents import GraphDocumentError, read_graph_deployment
from kora.client.errors import (
    COMPILE_ARGUMENT_CODE,
    INTERNAL_FAILURE_MESSAGE,
    LOCAL_VALIDATION_CODE,
    CompileArgument,
    compile_argument_findings,
    local_validation_error,
    validation_findings,
)
from kora.client.settings import ClientSettings
from kora.dsl import CompiledGraph, CompileFindings, CompileIncomplete, compile_graph
from kora.ir.diagnostic_collection import DiagnosticCollectionError
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
COMPILE_TOOL = "kora_compile"
COMPILE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path of a local graph.py source."},
        "format": {"type": "string", "const": "json"},
    },
    "required": ["path", "format"],
    "additionalProperties": False,
}
COMPILE_DESCRIPTION = (
    "Compile a local Python graph to JSON IR. Returns the compiled document or "
    "validation findings. The tool parses the source without running it or "
    "contacting the API."
)
GraphCompiler = Callable[[Path], CompiledGraph | CompileFindings]


class AuthoringTools:
    def __init__(
        self,
        client: KoraClient,
        diagnostics: ProcessDiagnostics,
        compile_source: GraphCompiler,
    ) -> None:
        self.client = client
        self.logger = diagnostics.logger(MCP_COMPONENT)
        self._compile_source: GraphCompiler = compile_source
        self._compile_limiter: CapacityLimiter = CapacityLimiter(1)
        self._compiling: bool = False

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
                    name=DEPLOY_TOOL,
                    description=DEPLOY_DESCRIPTION,
                    input_schema=DEPLOY_SCHEMA,
                ),
                types.Tool(
                    name=COMPILE_TOOL,
                    description=COMPILE_DESCRIPTION,
                    input_schema=COMPILE_SCHEMA,
                ),
            ]
        )

    async def call_tool(
        self,
        context: ServerRequestContext[object],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        if params.name == COMPILE_TOOL:
            return await self.compile(arguments)
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
            self.finished(DEPLOY_TOOL, "document_error", error.problem.value)
            return usage_error(f"The graph document is invalid: {error.problem.value}.")
        except ApiError as error:
            self.finished(DEPLOY_TOOL, "api_error", error.code)
            return tool_result(error.body, is_error=True)
        except ClientError as error:
            self.finished(DEPLOY_TOOL, "client_error", error.code)
            return tool_result(cast("JsonObject", error.envelope()), is_error=True)
        self.finished(DEPLOY_TOOL, "ok", "none")
        return tool_result(deployed.document)

    async def compile(self, arguments: dict[str, object]) -> types.CallToolResult:
        path = arguments.get("path")
        if set(arguments) - {"path", "format"}:
            return self._compile_argument_error("arguments")
        if not isinstance(path, str) or not path:
            return self._compile_argument_error("path")
        if arguments.get("format") != "json":
            return self._compile_argument_error("format")
        if self._compiling:
            self.finished(COMPILE_TOOL, "busy", "busy")
            return usage_error("A graph compilation is already in progress.")
        self._compiling = True
        try:
            try:
                outcome, code, result = await to_thread.run_sync(
                    self._compile_result,
                    path,
                    abandon_on_cancel=False,
                    limiter=self._compile_limiter,
                )
            except Exception as error:
                await checkpoint_if_cancelled()
                self.finished(COMPILE_TOOL, "internal_error", "internal_error")
                raise MCPError(
                    types.INTERNAL_ERROR, INTERNAL_FAILURE_MESSAGE
                ) from error
            await checkpoint_if_cancelled()
            self.finished(COMPILE_TOOL, outcome, code)
            return result
        finally:
            self._compiling = False

    def _compile_argument_error(self, reason: CompileArgument) -> types.CallToolResult:
        self.finished(COMPILE_TOOL, "usage_error", COMPILE_ARGUMENT_CODE)
        return tool_result(compile_argument_findings(reason), is_error=True)

    def _compile_result(self, path: str) -> tuple[str, str, types.CallToolResult]:
        try:
            result = self._compile_source(Path(path))
        except DiagnosticCollectionError:
            return (
                "client_error",
                LOCAL_VALIDATION_CODE,
                tool_result(
                    local_validation_error("diagnostic_count_limit"), is_error=True
                ),
            )
        except CompileIncomplete:
            return (
                "internal_error",
                "internal_error",
                usage_error(INTERNAL_FAILURE_MESSAGE),
            )
        if isinstance(result, CompileFindings):
            return (
                "invalid",
                "findings",
                tool_result(validation_findings(result.diagnostics), is_error=True),
            )
        return "ok", "none", tool_result(result.document)


def create_authoring_server(
    client: KoraClient,
    diagnostics: ProcessDiagnostics,
    *,
    compile_source: GraphCompiler = compile_graph,
) -> Server[object]:
    tools = AuthoringTools(client, diagnostics, compile_source)
    return Server(
        SERVER_NAMES["authoring"],
        on_list_tools=tools.list_tools,
        on_call_tool=tools.call_tool,
    )


SERVER_NAMES: dict[str, str] = {
    "authoring": "kora-authoring",
    "operating": "kora-operating",
}


async def serve_stdio(
    server_name: str, settings: ClientSettings, diagnostics: ProcessDiagnostics
) -> None:
    async with settings.client(diagnostics) as client:
        if server_name == "authoring":
            server = create_authoring_server(client, diagnostics)
        else:
            tools = OperatingTools(client, diagnostics)
            server = Server(
                SERVER_NAMES[server_name],
                on_list_tools=tools.list_tools,
                on_call_tool=tools.call_tool,
            )
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
