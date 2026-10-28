import copy
import re
import shlex
from functools import cache
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}")
OPAQUE_CHARACTER = re.compile(r"[^A-Za-z0-9_.-]")
WORD_BREAKS = frozenset(" \t;&|()<>")


class YamlLoader(Protocol):
    def load(self, stream: Path) -> object: ...


def table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@cache
def parsed_workflow(filename: str) -> object:
    return cast(YamlLoader, YAML(typ="safe")).load(WORKFLOWS / filename)


def workflow_document(filename: str = "ci.yml") -> dict[str, object]:
    return table(copy.deepcopy(parsed_workflow(filename)))


def workflow_names() -> tuple[str, ...]:
    paths = [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
    return tuple(sorted(path.name for path in paths))


def workflow_jobs() -> dict[str, object]:
    return {
        **table(workflow_document()["jobs"]),
        **table(workflow_document("conformance-lifetime.yml")["jobs"]),
    }


def step_list(job: dict[str, object]) -> list[dict[str, object]]:
    return [table(entry) for entry in cast(list[object], job["steps"])]


def steps(job: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(step["id"]): step for step in step_list(job) if "id" in step}


def matrix_rows(job: dict[str, object]) -> list[dict[str, object]]:
    unexpanded: list[object] = [{}]
    strategy = table(job.get("strategy", {}))
    rows = table(strategy.get("matrix", {})).get("include", unexpanded)
    return [table(row) for row in cast(list[object], rows)]


def rendered(text: object, row: dict[str, object]) -> str:
    result = str(text)
    for key, value in row.items():
        result = result.replace(f"${{{{ matrix.{key} }}}}", str(value))
    return result


def action_name(uses: object) -> str:
    assert isinstance(uses, str)
    return uses.partition("@")[0]


def opaque_expression(match: re.Match[str]) -> str:
    return "${{" + OPAQUE_CHARACTER.sub("_", "".join(match[1].split())) + "}}"


def without_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = "" if character == quote else quote
        elif character in "'\"":
            quote = character
        elif character == "#" and (index == 0 or line[index - 1] in WORD_BREAKS):
            return line[:index]
    return line


def command_lines(script: object) -> list[tuple[str, ...]]:
    assert isinstance(script, str)
    text = EXPRESSION.sub(opaque_expression, script)
    commands: list[tuple[str, ...]] = []
    for line in text.replace("\\\n", " ").splitlines():
        lexer = shlex.shlex(without_comment(line), posix=True, punctuation_chars=True)
        lexer.commenters = ""
        lexer.whitespace_split = True
        words = tuple(lexer)
        if words:
            commands.append(words)
    return commands
