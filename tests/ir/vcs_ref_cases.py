import json
from pathlib import Path
from typing import TypeVar, cast

from pydantic import BaseModel

from kora.ir import VcsRef
from kora.ir.json import JsonObject

ARTIFACTS: Path = Path(__file__).parents[2] / "knowledge/specs/artifacts"
Model = TypeVar("Model", bound=BaseModel)


def construct(model: type[Model], fields: dict[str, object]) -> Model:
    return model(**fields)


def unchecked(fields: dict[str, object]) -> VcsRef:
    return VcsRef.model_construct().model_copy(update=fields)


def example() -> JsonObject:
    return cast(
        "JsonObject", json.loads((ARTIFACTS / "examples/vcs-ref.json").read_bytes())
    )


def document() -> dict[str, object]:
    return dict(example())


def principal() -> dict[str, object]:
    return dict(cast("dict[str, object]", document()["pushed_by"]))


def wire(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def exact_size(size: int, unit: str = "a") -> dict[str, object]:
    value = {**document(), "diff_stat": ""}
    remaining = size - len(wire(value))
    width = len(wire(unit)) - 2
    value["diff_stat"] = unit * (remaining // width) + "a" * (remaining % width)
    return value


def oversized_objects() -> dict[str, dict[str, object]]:
    paths = [f"{index:05d}" + "a" * 251 for index in range(10_000)]
    late_control = [*paths[:-1], paths[-1][:-1] + "\u0001"]
    prefix = "https://example.test/"
    return {
        "wide": {**document(), **{f"extra{index}": "x" for index in range(100_000)}},
        "path_count": {**document(), "changed_paths": ["a"] * 10_001},
        "long_paths": {**document(), "changed_paths": paths},
        "last_control": {**document(), "changed_paths": late_control},
        "principal_before_bytes": {
            **document(),
            "changed_paths": paths,
            "pushed_by": {**principal(), "kind": "invalid"},
        },
        "display": {**document(), "diff_stat": "a" * 8_388_608},
        "url": {
            **document(),
            "commit_url": prefix + "a" * (8_388_608 - len(prefix)),
        },
    }
