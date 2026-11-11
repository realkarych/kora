import copy
from functools import cache
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

Workflows = dict[str, dict[str, object]]
JobEntry = tuple[str, dict[str, object]]


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


def repository_workflows() -> Workflows:
    return {name: workflow_document(name) for name in workflow_names()}


def jobs_of(workflows: Workflows) -> list[JobEntry]:
    return [
        (f"{name}:{identity}", table(job))
        for name, document in workflows.items()
        for identity, job in table(document["jobs"]).items()
    ]


def job_of(workflows: Workflows, name: str, identity: str) -> dict[str, object]:
    return table(table(workflows[name]["jobs"])[identity])


def step_list(job: dict[str, object]) -> list[dict[str, object]]:
    return [table(entry) for entry in cast(list[object], job.get("steps", []))]


def action_name(uses: object) -> str:
    assert isinstance(uses, str)
    return uses.partition("@")[0]
