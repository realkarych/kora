import json
from collections.abc import Mapping

from kora.observability.settings import RenderFormat

HEADER_KEYS: tuple[str, ...] = (
    "schema_version",
    "timestamp",
    "level",
    "component",
    "event",
    "message",
    "logger",
    "instance_id",
)


def render_json(record: Mapping[str, object]) -> bytes:
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return f"{text}\n".encode()


def render_text(record: Mapping[str, object]) -> bytes:
    head = " ".join(
        f"{record[key]}" for key in ("timestamp", "level", "component", "event")
    )
    details = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in record.items()
        if key not in HEADER_KEYS
    )
    line = f"{head} {record['message']}"
    if details:
        line = f"{line} {details}"
    return f"{line}\n".encode()


def render(record: Mapping[str, object], render_format: RenderFormat) -> bytes:
    if render_format is RenderFormat.TEXT:
        return render_text(record)
    return render_json(record)
