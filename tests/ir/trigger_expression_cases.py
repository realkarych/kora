from kora.ir import GraphDocument
from tests.ir.inline_schema_cases import document_for


def sensor(text: str) -> dict[str, object]:
    return {
        "kind": "sensor",
        "poll": {"sensor": "http", "until": text},
        "interval": "0s",
    }


def triggers(count: int, text: str = "observation.ready == true") -> GraphDocument:
    return document_for(
        triggers=[sensor(text) for _ in range(count)],
        source={"file": "graph.py", "line": 3},
    )


def expensive() -> str:
    eight = "+".join(["{v}"] * 8)
    return ".".join(
        ["observation.items"]
        + [f"map({name}, {eight.format(v=name)})" for name in ("a", "b", "c", "d")]
        + ["size() > 0"]
    )
