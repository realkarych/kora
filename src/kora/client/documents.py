import json
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from kora.ir.vcs_ref_json import finite_float, refuse_constant, unique_object
from kora.models.graphs import DeployRequest

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,63}")


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


def _regular_file_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GraphDocumentError(DocumentProblem.UNREADABLE)
        chunks: list[bytes] = []
        remaining = MAX_DOCUMENT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parsed(path: Path) -> object:
    try:
        data = _regular_file_bytes(path)
    except (OSError, ValueError):
        raise GraphDocumentError(DocumentProblem.UNREADABLE) from None
    if len(data) > MAX_DOCUMENT_BYTES:
        raise GraphDocumentError(DocumentProblem.TOO_LARGE)
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=refuse_constant,
            parse_float=finite_float,
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
    if not isinstance(namespace, str) or NAMESPACE.fullmatch(namespace) is None:
        raise GraphDocumentError(DocumentProblem.NOT_A_GRAPH)
    try:
        request = DeployRequest.model_validate(
            {"graph": identifier, "ir": document, "resources": []}
        )
    except ValidationError:
        raise GraphDocumentError(DocumentProblem.NOT_A_GRAPH) from None
    return GraphDeployment(namespace, request)
