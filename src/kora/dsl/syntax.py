import ast
import tokenize
from dataclasses import dataclass
from io import StringIO
from typing import TypeGuard, cast

from pydantic import TypeAdapter, ValidationError

from kora.dsl.source import SourceRefusal
from kora.ir.scalars import Duration, HarnessName, NodeId
from kora.ir.text import utf8_length

MAX_TOKENS = 256
MAX_NESTING = 16
OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = frozenset(OPENERS.values())
RESERVED_NAMES = frozenset({"approval", "graph", "node", "__debug__"})
PLACEHOLDERS = ("{{", "{%", "{#", "{resource_root}", "{input_root}")
CHANGE_OPERAND = "{input_root}/change.json"
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
    commit_reference: bool


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


def literal(
    value: ast.expr | None, line: int, *, allow_change_operand: bool = False
) -> str:
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        raise SourceRefusal(
            "Use a literal Python string for this value.",
            value.lineno if value is not None else line,
        )
    text = value.value
    if "\x00" in text or utf8_length(text) is None:
        raise SourceRefusal("A literal contains invalid IR text.", value.lineno)
    if allow_change_operand and text == CHANGE_OPERAND:
        return text
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
    call: ast.Call,
    required: set[str],
    unknown_code: str = "DSL011",
    *,
    missing_code: str = "DSL016",
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
            "This declaration is missing a required keyword.", call.lineno, missing_code
        )
    return result


def assignment(
    statement: ast.stmt, reserved_names: frozenset[str]
) -> tuple[str, ast.Call]:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        call = statement.value
        if (
            isinstance(target, ast.Name)
            and target.id not in reserved_names
            and isinstance(call, ast.Call)
        ):
            return target.id, call
    raise SourceRefusal(
        "Declare the agent and command with distinct local assignments.",
        statement.lineno,
    )


def is_node_call(value: ast.expr, kind: str) -> TypeGuard[ast.Call]:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and is_name(value.func.value, "node")
        and value.func.attr == kind
        and len(value.args) == 1
    )


def node_call(
    value: ast.Call, kind: str, line: int, *, require_inputs: bool = False
) -> ast.Call:
    if require_inputs and is_node_call(value, kind):
        raise SourceRefusal("Bind the declared command input.", line, "DSL003")
    constructor = value.func if require_inputs else value
    if is_node_call(constructor, kind):
        return constructor
    raise SourceRefusal(
        "Declare the agent and command with distinct local assignments.", line
    )


def agent(call: ast.Call, *, commit_reference: bool) -> Agent:
    required = {"harness", "prompt", "output"}
    if commit_reference:
        required.add("trust_repo_config")
    config = fields(call, required)
    output = config["output"]
    output_name = "change" if commit_reference else "summary"
    output_type = "VcsRef" if commit_reference else "StructuredOutput"
    if not (
        isinstance(output, ast.Dict)
        and len(output.keys) == 1
        and literal(output.keys[0], output.lineno) == output_name
        and is_name(output.values[0], output_type)
    ):
        if commit_reference:
            raise SourceRefusal(
                "Declare the agent change as VcsRef.", output.lineno, "DSL003"
            )
        raise SourceRefusal(
            "Declare the agent summary as StructuredOutput.", output.lineno
        )
    if commit_reference:
        fixed_value(config["harness"], "claude")
        trust = config["trust_repo_config"]
        if not isinstance(trust, ast.Constant) or trust.value is not False:
            raise SourceRefusal("Set trust_repo_config to False.", trust.lineno)
    return Agent(
        ir_literal(call.args[0], NODE_ID),
        ir_literal(config["harness"], HARNESS_NAME),
        literal(config["prompt"], call.lineno),
        call.lineno,
    )


def command(call: ast.Call, binding: ast.Call | None, agent_binding: str) -> Command:
    if binding is None:
        arguments = fields(call, {"cmd"})["cmd"]
    else:
        inputs = bound_fields(binding, {"change"})
        require_change_reference(inputs["change"], agent_binding)
        config = fields(call, {"cmd", "workspace"}, missing_code="DSL003")
        require_change_reference(config["workspace"], agent_binding)
        arguments = config["cmd"]
    if not isinstance(arguments, ast.List) or not arguments.elts:
        raise SourceRefusal(
            "Declare a nonempty list of command argument strings.", arguments.lineno
        )
    return Command(
        ir_literal(call.args[0], NODE_ID),
        tuple(
            literal(
                item,
                call.lineno,
                allow_change_operand=binding is not None and index > 0,
            )
            for index, item in enumerate(arguments.elts)
        ),
        call.lineno,
    )


def bound_fields(call: ast.Call, required: set[str]) -> dict[str, ast.expr]:
    if call.args:
        raise SourceRefusal("Bind inputs by keyword.", call.lineno, "DSL003")
    return fields(call, required, "DSL003", missing_code="DSL003")


def require_change_reference(value: ast.expr, agent_binding: str) -> None:
    if not (
        isinstance(value, ast.Attribute)
        and is_name(value.value, agent_binding)
        and value.attr == "change"
    ):
        raise SourceRefusal(
            "Use the declared agent's whole change output.", value.lineno, "DSL003"
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


def approval(
    statement: ast.stmt, command_binding: str, agent_binding: str | None
) -> Approval:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        raise SourceRefusal(
            "Declare a CLI approval with a direct command input.", statement.lineno
        )
    bind = statement.value
    if (
        agent_binding is not None
        and is_name(bind.func, "approval")
        and len(bind.args) == 1
    ):
        raise SourceRefusal(
            "Bind the approval change and gate by keyword.", bind.lineno, "DSL003"
        )
    call = bind.func
    if (
        not isinstance(call, ast.Call)
        or not is_name(call.func, "approval")
        or len(call.args) != 1
    ):
        raise SourceRefusal(
            "Declare a CLI approval with a direct command input.", statement.lineno
        )
    if agent_binding is not None:
        return bound_approval(call, bind, agent_binding, command_binding)
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


def bound_approval(
    call: ast.Call, bind: ast.Call, agent_binding: str, command_binding: str
) -> Approval:
    inputs = bound_fields(bind, {"change", "gate"})
    require_change_reference(inputs["change"], agent_binding)
    if not is_name(inputs["gate"], command_binding):
        raise SourceRefusal(
            "Bind the approval gate to the declared command.",
            inputs["gate"].lineno,
            "DSL003",
        )
    config = fields(call, {"channel", "timeout", "on_expiry", "binds"})
    fixed_value(config["channel"], "cli")
    fixed_value(config["timeout"], "1h")
    fixed_value(config["on_expiry"], "fail")
    approval_binds(config["binds"])
    return Approval(ir_literal(call.args[0], NODE_ID), "1h", "gate", call.lineno)


def approval_binds(value: ast.expr) -> None:
    if isinstance(value, ast.Dict) and len(value.keys) == 2:
        bindings = {
            literal(key, value.lineno): item
            for key, item in zip(value.keys, value.values, strict=True)
        }
        vcs_ref = bindings.get("vcs_ref")
        gates = bindings.get("gate_digests")
        if (
            vcs_ref is not None
            and literal(vcs_ref, vcs_ref.lineno) == "inputs.change"
            and isinstance(gates, ast.List)
            and len(gates.elts) == 1
            and literal(gates.elts[0], gates.lineno) == "inputs.gate"
        ):
            return
    raise SourceRefusal(
        "Bind the approval to its declared change and gate inputs.",
        value.lineno,
        "DSL003",
    )


def fixed_value(value: ast.expr, expected: str) -> None:
    if literal(value, value.lineno) != expected:
        raise SourceRefusal(
            "This declaration requires the fixed early-mode value.", value.lineno
        )


def graph_function(module: ast.Module) -> tuple[ast.FunctionDef, bool]:
    allowed = {
        "kora": {"approval", "graph", "node"},
        "kora.artifacts": {"StructuredOutput"},
    }
    commit_reference = False
    for statement in module.body[:2]:
        if isinstance(statement, ast.ImportFrom) and statement.level == 0:
            names = allowed.pop(statement.module or "", None)
            imported = {alias.name for alias in statement.names}
            if (
                names is not None
                and len(statement.names) == len(names)
                and (
                    imported == names
                    or (statement.module == "kora.artifacts" and imported == {"VcsRef"})
                )
                and all(alias.asname is None for alias in statement.names)
            ):
                if imported == {"VcsRef"}:
                    commit_reference = True
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
    return function, commit_reference


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
    function, commit_reference = graph_function(module)
    reserved_names = RESERVED_NAMES | {
        "VcsRef" if commit_reference else "StructuredOutput"
    }
    args = function.args
    type_params: object = getattr(function, "type_params", ())
    if (
        function.name in reserved_names
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
    agent_binding, agent_source = assignment(first, reserved_names)
    agent_call = node_call(agent_source, "agent", first.lineno)
    command_binding, command_source = assignment(second, reserved_names)
    command_input = command_source if commit_reference else None
    command_call = node_call(
        command_source, "command", second.lineno, require_inputs=commit_reference
    )
    if agent_binding == command_binding:
        raise SourceRefusal(
            "Use distinct local bindings for the agent and command.", second.lineno
        )
    agent_node = agent(agent_call, commit_reference=commit_reference)
    command_node = command(command_call, command_input, agent_binding)
    control_edge(edge, agent_binding, command_binding)
    approval_node = approval(
        last, command_binding, agent_binding if commit_reference else None
    )
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
        commit_reference,
    )
