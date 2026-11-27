import ast
import tokenize
from dataclasses import dataclass
from io import StringIO
from typing import cast

from pydantic import TypeAdapter, ValidationError

from kora.dsl.source import SourceRefusal
from kora.ir.scalars import Duration, HarnessName, NodeId
from kora.ir.text import utf8_length

MAX_TOKENS = 256
MAX_NESTING = 16
OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = frozenset(OPENERS.values())
RESERVED_NAMES = frozenset(
    {"approval", "graph", "node", "StructuredOutput", "__debug__"}
)
PLACEHOLDERS = ("{{", "{%", "{#", "{resource_root}", "{input_root}")
NODE_ID: TypeAdapter[NodeId] = TypeAdapter(NodeId)
HARNESS_NAME: TypeAdapter[HarnessName] = TypeAdapter(HarnessName)
DURATION: TypeAdapter[Duration] = TypeAdapter(Duration)


@dataclass(frozen=True, slots=True)
class Agent:
    name: str
    harness: str
    prompt: str
    line: int


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    arguments: tuple[str, ...]
    line: int


@dataclass(frozen=True, slots=True)
class Approval:
    name: str
    timeout: str
    input_name: str
    line: int


@dataclass(frozen=True, slots=True)
class GraphSyntax:
    name: str
    namespace: str
    repo: str
    rev: str
    agent: Agent
    command: Command
    approval: Approval
    line: int


def preflight(source: str) -> None:
    closing: list[str] = []
    count = 0
    try:
        for token in tokenize.generate_tokens(StringIO(source).readline):
            kind, spelling = token.type, token.string
            line = token.start[0]
            if kind in (tokenize.COMMENT, tokenize.NL, tokenize.ENDMARKER):
                continue
            count += 1
            if count > MAX_TOKENS:
                raise SourceRefusal("Python source exceeds the 256-token limit.", line)
            if kind == tokenize.STRING:
                if not spelling.startswith(("'", '"')) or "\\" in spelling:
                    raise SourceRefusal(
                        "Use unprefixed Python strings without escapes.", line
                    )
            elif kind == tokenize.OP:
                if spelling in OPENERS:
                    closing.append(OPENERS[spelling])
                    if len(closing) > MAX_NESTING:
                        raise SourceRefusal(
                            "Python source exceeds the delimiter nesting limit of 16.",
                            line,
                        )
                elif spelling in CLOSERS and (not closing or closing.pop() != spelling):
                    raise SourceRefusal(
                        "Python source must have balanced delimiters.", line
                    )
            elif kind not in (
                tokenize.NAME,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
            ):
                raise SourceRefusal(
                    "Python source contains an unsupported token.", line
                )
    except tokenize.TokenError as error:
        line, _ = cast("tuple[int, int]", error.args[1])
        raise SourceRefusal(
            "Python source does not tokenize as supported syntax.", line
        ) from None
    except SyntaxError as error:
        raise SourceRefusal(
            "Python source does not tokenize as supported syntax.", error.lineno
        ) from None
    if closing:
        raise SourceRefusal("Python source must have balanced delimiters.")


def is_name(value: ast.expr | None, spelling: str) -> bool:
    return isinstance(value, ast.Name) and value.id == spelling


def literal(value: ast.expr | None, line: int) -> str:
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        raise SourceRefusal(
            "Use a literal Python string for this value.",
            value.lineno if value is not None else line,
        )
    text = value.value
    if "\x00" in text or utf8_length(text) is None:
        raise SourceRefusal("A literal contains invalid IR text.", value.lineno)
    if any(marker in text for marker in PLACEHOLDERS):
        raise SourceRefusal(
            "Templates and command placeholders are unavailable in this mode.",
            value.lineno,
        )
    return text


def ir_value(value: str, adapter: TypeAdapter[str], line: int) -> str:
    try:
        return adapter.validate_python(value)
    except ValidationError:
        raise SourceRefusal(
            "A declared value does not satisfy its IR field constraints.", line
        ) from None


def ir_literal(value: ast.expr, adapter: TypeAdapter[str]) -> str:
    return ir_value(literal(value, value.lineno), adapter, value.lineno)


def fields(
    call: ast.Call, required: set[str], unknown_code: str = "DSL011"
) -> dict[str, ast.expr]:
    result: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        name = keyword.arg
        if name is None or name in result:
            raise SourceRefusal("Use each explicit keyword once.", keyword.lineno)
        if name not in required:
            raise SourceRefusal(
                "This declaration has an unsupported keyword.",
                keyword.lineno,
                unknown_code,
            )
        result[name] = keyword.value
    if result.keys() != required:
        raise SourceRefusal(
            "This declaration is missing a required keyword.", call.lineno
        )
    return result


def assignment(statement: ast.stmt, kind: str) -> tuple[str, ast.Call]:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        call = statement.value
        if (
            isinstance(target, ast.Name)
            and target.id not in RESERVED_NAMES
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and is_name(call.func.value, "node")
            and call.func.attr == kind
            and len(call.args) == 1
        ):
            return target.id, call
    raise SourceRefusal(
        "Declare the agent and command with distinct local assignments.",
        statement.lineno,
    )


def agent(call: ast.Call) -> Agent:
    config = fields(call, {"harness", "prompt", "output"})
    output = config["output"]
    if not (
        isinstance(output, ast.Dict)
        and len(output.keys) == 1
        and literal(output.keys[0], output.lineno) == "summary"
        and is_name(output.values[0], "StructuredOutput")
    ):
        raise SourceRefusal(
            "Declare the agent summary as StructuredOutput.", output.lineno
        )
    return Agent(
        ir_literal(call.args[0], NODE_ID),
        ir_literal(config["harness"], HARNESS_NAME),
        literal(config["prompt"], call.lineno),
        call.lineno,
    )


def command(call: ast.Call) -> Command:
    arguments = fields(call, {"cmd"})["cmd"]
    if not isinstance(arguments, ast.List) or not arguments.elts:
        raise SourceRefusal(
            "Declare a nonempty list of command argument strings.", arguments.lineno
        )
    return Command(
        ir_literal(call.args[0], NODE_ID),
        tuple(literal(item, call.lineno) for item in arguments.elts),
        call.lineno,
    )


def control_edge(statement: ast.stmt, first: str, second: str) -> None:
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.BinOp):
        edge = statement.value
        if isinstance(edge.op, ast.RShift):
            if is_name(edge.left, first) and is_name(edge.right, second):
                return
            raise SourceRefusal(
                "Connect the agent and command through their declared local bindings.",
                statement.lineno,
                "DSL003",
            )
    raise SourceRefusal(
        "Declare one agent-to-command control edge using >>.", statement.lineno
    )


def approval(statement: ast.stmt, command_binding: str) -> Approval:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        raise SourceRefusal(
            "Declare a CLI approval with a direct command input.", statement.lineno
        )
    bind = statement.value
    call = bind.func
    if (
        not isinstance(call, ast.Call)
        or not is_name(call.func, "approval")
        or len(call.args) != 1
    ):
        raise SourceRefusal(
            "Declare a CLI approval with a direct command input.", statement.lineno
        )
    if bind.args or len(bind.keywords) != 1:
        raise SourceRefusal(
            "Bind one approval input to the command by keyword.", bind.lineno, "DSL003"
        )
    binding = bind.keywords[0]
    if binding.arg is None or not is_name(binding.value, command_binding):
        raise SourceRefusal(
            "Bind the approval input to the declared command.", binding.lineno, "DSL003"
        )
    config = fields(call, {"channel", "timeout", "on_expiry"})
    fixed_value(config["channel"], "cli")
    fixed_value(config["on_expiry"], "fail")
    return Approval(
        ir_literal(call.args[0], NODE_ID),
        ir_literal(config["timeout"], DURATION),
        ir_value(binding.arg, NODE_ID, binding.lineno),
        call.lineno,
    )


def fixed_value(value: ast.expr, expected: str) -> None:
    if literal(value, value.lineno) != expected:
        raise SourceRefusal(
            "This declaration requires the fixed early-mode value.", value.lineno
        )


def graph_function(module: ast.Module) -> ast.FunctionDef:
    allowed = {
        "kora": {"approval", "graph", "node"},
        "kora.artifacts": {"StructuredOutput"},
    }
    for statement in module.body[:2]:
        if isinstance(statement, ast.ImportFrom) and statement.level == 0:
            names = allowed.pop(statement.module or "", None)
            if (
                names is not None
                and len(statement.names) == len(names)
                and {alias.name for alias in statement.names} == names
                and all(alias.asname is None for alias in statement.names)
            ):
                continue
        raise SourceRefusal(
            "Use only the supported unaliased Python DSL imports.", statement.lineno
        )
    if len(module.body) < 3:
        raise SourceRefusal("Declare one graph function after the imports.")
    function = module.body[2]
    if not isinstance(function, ast.FunctionDef):
        raise SourceRefusal("Declare one synchronous graph function.", function.lineno)
    if len(module.body) > 3:
        raise SourceRefusal(
            "Declare exactly one graph function after the imports.",
            module.body[3].lineno,
        )
    return function


def graph_syntax(source: str) -> GraphSyntax:
    preflight(source)
    try:
        module = ast.parse(source, filename="<graph-source>")
    except SyntaxError as error:
        raise SourceRefusal("Python source has invalid syntax.", error.lineno) from None
    except RecursionError:
        raise SourceRefusal(
            "Python source exceeds the parser's structural limit."
        ) from None
    function = graph_function(module)
    args = function.args
    type_params: object = getattr(function, "type_params", ())
    if (
        function.name in RESERVED_NAMES
        or type_params
        or args.posonlyargs
        or args.args
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or (
            function.returns is not None
            and not (
                isinstance(function.returns, ast.Constant)
                and function.returns.value is None
            )
        )
        or len(function.decorator_list) != 1
    ):
        raise SourceRefusal(
            "Declare a parameterless graph function with one graph decorator.",
            function.lineno,
        )
    decorator = function.decorator_list[0]
    if (
        not isinstance(decorator, ast.Call)
        or not is_name(decorator.func, "graph")
        or decorator.args
    ):
        raise SourceRefusal(
            "Use the graph decorator with explicit keyword arguments.", decorator.lineno
        )
    config = fields(decorator, {"namespace", "repo", "rev", "runtime", "vcs"}, "DSL016")
    fixed_value(config["runtime"], "local")
    fixed_value(config["vcs"], "git")
    if len(function.body) < 4:
        raise SourceRefusal(
            "Declare exactly the agent, command, edge and approval statements.",
            function.lineno,
        )
    first, second, edge, last = function.body[:4]
    agent_binding, agent_call = assignment(first, "agent")
    command_binding, command_call = assignment(second, "command")
    if agent_binding == command_binding:
        raise SourceRefusal(
            "Use distinct local bindings for the agent and command.", second.lineno
        )
    agent_node = agent(agent_call)
    command_node = command(command_call)
    control_edge(edge, agent_binding, command_binding)
    approval_node = approval(last, command_binding)
    if len(function.body) > 4:
        raise SourceRefusal(
            "The graph body has an unsupported extra statement.",
            function.body[4].lineno,
        )
    if agent_node.name == command_node.name:
        raise SourceRefusal(
            "Node IDs must be distinct.", command_call.args[0].lineno, "DSL002"
        )
    if approval_node.name in (agent_node.name, command_node.name):
        raise SourceRefusal("Node IDs must be distinct.", approval_node.line, "DSL002")
    return GraphSyntax(
        ir_value(function.name, NODE_ID, function.lineno),
        ir_literal(config["namespace"], NODE_ID),
        literal(config["repo"], decorator.lineno),
        literal(config["rev"], decorator.lineno),
        agent_node,
        command_node,
        approval_node,
        function.lineno,
    )
