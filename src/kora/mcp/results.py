import json

from mcp import types

from kora.ir.json import JsonObject


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
