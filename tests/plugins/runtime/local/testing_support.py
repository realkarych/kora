import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from kora.conformance.capture import Capture
from kora.protocols.runtime import RawEvent
from kora.testing.local import TestingLocal as LocalComposition
from kora.testing.script import FakeScript
from tests.plugins.runtime.local.test_command import attempt_directory, journal_events
from tests.support.local_runners import reap_runners, short_directory
from tests.support.local_runtime import runtime_context


def completed_script() -> FakeScript:
    return FakeScript.model_validate(
        {
            "end": "completed",
            "on_interrupt": "acknowledge",
            "steps": [{"kind": "result", "status": "success"}],
        }
    )


@asynccontextmanager
async def composition(root: Path, capture: Capture) -> AsyncGenerator[LocalComposition]:
    with short_directory() as base:
        local = await LocalComposition.open(
            root, base / "sockets", runtime_context(capture)
        )
        try:
            yield local
        finally:
            await local.close()
            reap_runners(root)


def attempt(local: LocalComposition, attempt_id: str = "attempt-1") -> Path:
    return attempt_directory(local.root / "state", local.runtime, attempt_id)


def records(local: LocalComposition) -> tuple[RawEvent, ...]:
    path = attempt(local) / "journal.jsonl"
    return journal_events(path) if path.exists() else ()


def transfer_outcomes(local: LocalComposition) -> list[tuple[str, str]]:
    path = attempt(local) / "runner.log"
    found: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        record: dict[str, object] = json.loads(line)
        if record.get("event") == "runtime.local.transfer":
            assert (
                not {"name", "sha256", "path", "attempt_id", "script"} & record.keys()
            )
            found.append((str(record["outcome"]), str(record["level"])))
    return found
