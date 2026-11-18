from typing import Final, cast

from kora.plugins.harness.claude.configuration import CLI_VERSION
from kora.protocols.harness import RunnerCause

REVIEWED_PLUGINS: Final[frozenset[str]] = frozenset(
    {"cc-plugin-agents-md", "cc-plugin-plugin-authoring"}
)
REVIEWED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "Task",
        "Bash",
        "CronCreate",
        "CronDelete",
        "CronList",
        "Edit",
        "EnterWorktree",
        "ExitWorktree",
        "ListAgents",
        "NotebookEdit",
        "Read",
        "ReportFindings",
        "ScheduleWakeup",
        "SendMessage",
        "Skill",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskStop",
        "TaskUpdate",
        "WebFetch",
        "WebSearch",
        "Workflow",
        "Write",
    }
)
REVIEWED_FILE_EDIT_TOOLS: Final[frozenset[str]] = frozenset(
    {"Edit", "NotebookEdit", "Write"}
)


def builtin_plugins(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for entry in cast("list[object]", value):
        if not isinstance(entry, dict):
            return None
        fields = cast("dict[object, object]", entry)
        name = fields.get("name")
        if not isinstance(name, str) or fields.get("path") != "builtin":
            return None
        names.append(name)
    return names


def surface_cause(data: object, *, output_format: bool) -> RunnerCause | None:
    if not isinstance(data, dict):
        return "plugin_mismatch"
    fields = cast("dict[object, object]", data)
    version = fields.get("claude_code_version")
    if not isinstance(version, str):
        return "plugin_mismatch"
    if version != CLI_VERSION:
        return "version_mismatch"
    plugins = builtin_plugins(fields.get("plugins"))
    if (
        plugins is None
        or len(plugins) != len(set(plugins))
        or frozenset(plugins) != REVIEWED_PLUGINS
    ):
        return "plugin_mismatch"
    tools = fields.get("tools")
    if not isinstance(tools, list):
        return "plugin_mismatch"
    names: list[str] = []
    for tool in cast("list[object]", tools):
        if not isinstance(tool, str):
            return "plugin_mismatch"
        names.append(tool)
    if len(names) != len(set(names)):
        return "plugin_mismatch"
    builtins = {name for name in names if not name.startswith("mcp__")}
    if output_format:
        builtins.discard("StructuredOutput")
    if not builtins <= REVIEWED_TOOLS:
        return "plugin_mismatch"
    servers = fields.get("mcp_servers")
    if not isinstance(servers, list):
        return "plugin_mismatch"
    sources: list[str] = []
    for server in cast("list[object]", servers):
        if not isinstance(server, dict):
            return "plugin_mismatch"
        source = cast("dict[object, object]", server).get("source")
        if source is not None:
            if not isinstance(source, str):
                return "plugin_mismatch"
            sources.append(source)
    return "config_check_failed" if "claudeai" in sources else None
