import json
from pathlib import Path

import pytest

from kora.cel_eval.binding import PINNED_VERSION
from kora.observability.catalog import RESERVED_KEYS
from tests.cel_eval.children import (
    LOAD_OUTCOME,
    full_path,
    install_fake,
    isolated_path,
    isolated_site,
    run_child,
)
from tests.cel_eval.test_cel_binding_loader import fake

MARKER: str = "UNREGISTERED-MARKER-7731"

CAPTURE_RECORDS: str = """
import logging
records = []
class Keep(logging.Handler):
    def emit(self, record):
        records.append((record.name, record.getMessage()))
logging.getLogger().addHandler(Keep(logging.DEBUG))
logging.getLogger().setLevel(logging.DEBUG)
"""
FAIL_IN_CALLABLE: str = f"""
import cel
def failing():
    raise RuntimeError({MARKER!r})
try:
    cel.compile("failing()").execute({{"failing": failing}})
except RuntimeError:
    pass
"""
RECONFIGURE: str = """
import logging.config
logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})
logging.getLogger().addHandler(Keep(logging.DEBUG))
"""
LOAD: str = "from kora.cel_eval.binding import load_binding\nload_binding()\n"
REPORT: str = "print(json.dumps(records))\n"


def logged(code: str) -> list[list[str]]:
    result = run_child(code, full_path())
    assert result.code == 0, result.stderr
    records: list[list[str]] = json.loads(result.stdout)
    return records


def carries_marker(records: list[list[str]]) -> bool:
    return any(MARKER in message for _, message in records)


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        (CAPTURE_RECORDS + FAIL_IN_CALLABLE, True),
        (CAPTURE_RECORDS + LOAD + FAIL_IN_CALLABLE, False),
        (CAPTURE_RECORDS + LOAD + RECONFIGURE + FAIL_IN_CALLABLE, False),
    ],
    ids=["without_loader", "with_loader", "after_dict_config"],
)
def test_cel_binding_logger_never_carries_content(steps: str, expected: bool) -> None:
    code = steps + REPORT

    records = logged(code)

    assert carries_marker(records) is expected


CONFIGURED: str = """
from datetime import UTC, datetime
from kora.cel_eval import CEL_COMPONENT
from kora.observability import LoggingSettings, configure_process
diagnostics = configure_process(
    CEL_COMPONENT,
    LoggingSettings(),
    "test-instance",
    lambda: datetime(2026, 10, 21, tzinfo=UTC),
)
"""
SHUTDOWN: str = "diagnostics.shutdown(5.0)\n"


def loaded_events(stderr: str) -> list[dict[str, object]]:
    records = [json.loads(line) for line in stderr.splitlines()]
    return [record for record in records if record.get("event") == "cel.binding.loaded"]


def test_cel_binding_loaded_event_is_safe(tmp_path: Path) -> None:
    refused_site = install_fake(isolated_site(tmp_path / "site"), fake(version="0.9.0"))

    loaded = run_child(CONFIGURED + LOAD_OUTCOME + LOAD_OUTCOME + SHUTDOWN, full_path())
    refused = run_child(
        CONFIGURED + LOAD_OUTCOME + SHUTDOWN, isolated_path(refused_site)
    )

    events = loaded_events(loaded.stderr)
    assert loaded.code == 0, loaded.stderr
    assert loaded.stdout.splitlines() == [f"loaded {PINNED_VERSION}"] * 2
    assert len(events) == 1
    assert events[0]["level"] == "INFO"
    assert events[0]["component"] == "cel"
    assert events[0]["logger"] == "kora.cel_eval"
    assert events[0]["version"] == PINNED_VERSION
    assert set(events[0]) - RESERVED_KEYS == {"version"}
    assert refused.code == 0, refused.stderr
    assert refused.stdout.splitlines() == ["version_mismatch"]
    assert loaded_events(refused.stderr) == []
