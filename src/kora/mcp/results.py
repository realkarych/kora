import json

from mcp import types

from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level

MCP_COMPONENT: str = CATALOG.register_component("mcp")
TOOL_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        "mcp.tool.finished",
        MCP_COMPONENT,
        Level.DEBUG,
        "an MCP tool call finished",
        {
            "tool": FieldKind.STRING,
            "outcome": FieldKind.STRING,
            "code": FieldKind.STRING,
        },
    )
)


def tool_result(
    document: JsonObject, *, is_error: bool = False
) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(document))],
        structured_content=document,
        is_error=is_error,
    )


def usage_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=message)], is_error=True
    )
