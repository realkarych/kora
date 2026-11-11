import copy
from functools import cache
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


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


def step_list(job: dict[str, object]) -> list[dict[str, object]]:
    return [table(entry) for entry in cast(list[object], job.get("steps", []))]


def action_name(uses: object) -> str:
    assert isinstance(uses, str)
    return uses.partition("@")[0]
