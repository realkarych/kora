import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from kora.models.graphs import DeployRequest

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class DocumentProblem(StrEnum):
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    NOT_JSON = "not_json"
    NOT_A_GRAPH = "not_a_graph"


class GraphDocumentError(ValueError):
    def __init__(self, problem: DocumentProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


@dataclass(frozen=True, slots=True)
class GraphDeployment:
    namespace: str
    request: DeployRequest


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise GraphDocumentError(DocumentProblem.NOT_JSON)
    return document


def _finite_only(token: str) -> object:
    raise GraphDocumentError(DocumentProblem.NOT_JSON)


def _parsed(path: Path) -> object:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError:
        raise GraphDocumentError(DocumentProblem.UNREADABLE) from None
    if len(data) > MAX_DOCUMENT_BYTES:
        raise GraphDocumentError(DocumentProblem.TOO_LARGE)
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_finite_only,
        )
    except (ValueError, RecursionError):
        raise GraphDocumentError(DocumentProblem.NOT_JSON) from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GraphDocumentError(DocumentProblem.NOT_A_GRAPH)
    return cast("dict[str, object]", value)


def read_graph_deployment(path: Path) -> GraphDeployment:
    document = _object(_parsed(path))
    graph = _object(document.get("graph"))
    identifier = graph.get("id")
    namespace = graph.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise GraphDocumentError(DocumentProblem.NOT_A_GRAPH)
    try:
        request = DeployRequest.model_validate(
            {"graph": identifier, "ir": document, "resources": []}
        )
    except ValidationError:
        raise GraphDocumentError(DocumentProblem.NOT_A_GRAPH) from None
    return GraphDeployment(namespace, request)
