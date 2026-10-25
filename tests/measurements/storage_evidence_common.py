import json
from pathlib import Path
from typing import Annotated
from xml.parsers.expat import ParserCreate

from pydantic import Field, StrictBool, StrictInt

from kora.protocols.common import BoundaryModel


class EvidenceFailure(ValueError):
    pass


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise EvidenceFailure("duplicate evidence fields")
        result[name] = value
    return result


def nonfinite(value: str) -> None:
    raise EvidenceFailure("nonfinite evidence numbers")


def junit_nodes(path: Path, maximum: int = 4194304) -> frozenset[str]:
    if path.stat().st_size > maximum:
        raise EvidenceFailure("the storage test evidence exceeds its bound")
    nodes: set[str] = set()

    def start(name: str, attributes: dict[str, str]) -> None:
        if name in {"failure", "error", "skipped"}:
            raise EvidenceFailure("the storage measurement test did not pass")
        if name != "testcase":
            return
        classname = attributes.get("classname")
        test = attributes.get("name")
        if not classname or not test:
            raise EvidenceFailure("the storage test identity is missing")
        identity = f"{classname}.{test}"
        if identity in nodes:
            raise EvidenceFailure("the storage test identity is duplicated")
        nodes.add(identity)

    def refuse_doctype(
        name: str, system_id: str | None, public_id: str | None, subset: int
    ) -> None:
        raise EvidenceFailure("storage test evidence cannot declare a document type")

    parser = ParserCreate()
    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.Parse(path.read_bytes(), True)
    return frozenset(nodes)


class Execution(BoundaryModel):
    pid: Annotated[StrictInt, Field(ge=1)]
    status: StrictInt
    timed_out: StrictBool
    overflow: StrictBool


def read_json(path: Path, maximum: int = 16777216) -> object:
    with path.open("rb") as stream:
        encoded = stream.read(maximum + 1)
    if len(encoded) > maximum:
        raise EvidenceFailure("storage evidence exceeds its input bound")
    result: object = json.loads(
        encoded, object_pairs_hook=unique_object, parse_constant=nonfinite
    )
    return result


def validate_execution(path: Path, pid: int | None = None) -> Execution:
    execution = Execution.model_validate(read_json(path, 16384))
    if execution.status != 0 or execution.timed_out or execution.overflow:
        raise EvidenceFailure(
            "the storage evidence child did not complete successfully"
        )
    if pid is not None and execution.pid != pid:
        raise EvidenceFailure(
            "the recorded child differs from its measurement producer"
        )
    return execution
