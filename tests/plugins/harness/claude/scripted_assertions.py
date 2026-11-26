import json
from pathlib import Path
from typing import Final

from tests.plugins.harness.claude.scripted_model import KEY, mapping, objects

PROMPT: Final[str] = "conformance"
SCRIPTED: Final[str] = "scripted-bytes\n"
SEEDED: Final[str] = "seeded-bytes\n"


def prepare_scenario(root: Path) -> tuple[Path, Path, Path]:
    workspace, account, markers = (
        root / name for name in ("workspace", "account", "markers")
    )
    for path in (workspace, account, markers):
        path.mkdir()
    (workspace / "seeded.txt").write_text(SEEDED)
    return workspace, account, markers


def event_data(report: dict[str, object], kind: str) -> list[dict[str, object]]:
    events = objects(report["events"])
    assert events is not None
    found: list[dict[str, object]] = []
    for value in events:
        event = mapping(value)
        assert event is not None
        if event["kind"] == kind:
            data = mapping(event["data"])
            assert data is not None
            found.append(data)
    return found


def assert_diagnostics(report: dict[str, object], outcome: str, target: Path) -> None:
    records = objects(report["diagnostics"])
    assert records is not None
    finished = [
        record
        for value in records
        if (record := mapping(value)) is not None
        and record.get("event") == "harness.claude.session_finished"
    ]
    assert len(finished) == 1
    assert finished[0]["outcome"] == outcome
    rendered = json.dumps(records)
    for secret in (KEY, PROMPT, SCRIPTED, str(target)):
        assert json.dumps(secret)[1:-1] not in rendered
