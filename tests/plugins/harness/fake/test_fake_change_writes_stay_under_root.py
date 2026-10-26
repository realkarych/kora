import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir.json import JsonObject
from kora.plugins.harness.fake import ScriptHeader
from kora.plugins.harness.fake.script import (
    MAX_CHANGE_FILE_BYTES,
    MAX_STEPS,
    STEP_ADAPTER,
    BoundedSteps,
    Step,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import SessionResult
from tests.plugins.harness.fake.fake_support import (
    ScriptedSink,
    harness,
    header,
    played,
    result,
    scripted,
    turn,
)

FILE: Final[str] = "x" * MAX_CHANGE_FILE_BYTES
WIDE: Final[str] = "\u00e9" * (MAX_CHANGE_FILE_BYTES // 2 + 1)


def change(files: dict[str, str]) -> JsonObject:
    return {"kind": "change", "files": dict(files)}


def run_change(root: Path, files: dict[str, str], capture: Capture) -> SessionResult:
    adapter = harness(
        scripted([turn("m1"), change(files), result()]),
        context_for(capture),
        str(root),
    )
    return asyncio.run(played(adapter, ScriptedSink()))


def refusal(root: Path, files: dict[str, str], capture: Capture) -> str:
    with pytest.raises(KoraProtocolError) as raised:
        run_change(root, files, capture)
    return raised.value.code


def test_fake_change_writes_stay_under_root_nested_write(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    outcome = run_change(root, {"src/pkg/module.py": "value = 1\n"}, kora_capture)

    assert (root / "src/pkg/module.py").read_text() == "value = 1\n"
    assert outcome.error_class is None


@pytest.mark.parametrize(
    "path", ["../escape", "/etc/escape", "a/../escape", "a//b", "./x", "x/", "a\0b"]
)
def test_fake_change_writes_stay_under_root_refuses_path_forms(
    path: str, tmp_path: Path, kora_capture: Capture
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    code = refusal(root, {path: "escaped"}, kora_capture)

    assert code == "script_change_path_invalid"
    assert not (tmp_path / "escape").exists()


def test_fake_change_writes_stay_under_root_refuses_links(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("kept")
    (root / "linked").symlink_to(outside)
    (root / "file-link").symlink_to(victim)
    os.link(victim, root / "hard")
    os.mkfifo(root / "pipe")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root)

    codes = [
        refusal(root, {"linked/victim": "escaped"}, kora_capture),
        refusal(root, {"file-link": "escaped"}, kora_capture),
        refusal(root, {"hard": "escaped"}, kora_capture),
        refusal(root, {"pipe": "escaped"}, kora_capture),
        refusal(linked_root, {"new": "escaped"}, kora_capture),
    ]

    assert set(codes) == {"script_change_path_invalid"}
    assert victim.read_text() == "kept"
    assert not (outside / "new").exists()


def test_fake_change_writes_stay_under_root_caps(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    per_script = {f"file-{index}": FILE for index in range(64)}

    at_cap = run_change(root, {"one": FILE}, kora_capture)
    over_file = refusal(root, {"one": WIDE}, kora_capture)
    over_script = refusal(root, {**per_script, "last": "x"}, kora_capture)

    assert at_cap.error_class is None
    assert over_file == "script_change_too_large"
    assert over_script == "script_change_too_large"


class CountedSource:
    def __init__(self, steps: int) -> None:
        self.header: ScriptHeader = header("completed")
        self.count = steps
        self.turn: Step = STEP_ADAPTER.validate_python(turn("m1"))
        self.result: Step = STEP_ADAPTER.validate_python(result())
        self.pulled = 0

    async def steps(self) -> AsyncGenerator[Step]:
        for _ in range(self.count - 1):
            self.pulled += 1
            yield self.turn
        self.pulled += 1
        yield self.result


async def consumed(source: CountedSource) -> int:
    seen = 0
    async for _ in BoundedSteps(source).steps():
        seen += 1
    return seen


def test_fake_change_writes_stay_under_root_step_cap() -> None:
    at_cap = CountedSource(MAX_STEPS)
    over_cap = CountedSource(MAX_STEPS + 1)

    seen = asyncio.run(consumed(at_cap))
    with pytest.raises(KoraProtocolError) as raised:
        asyncio.run(consumed(over_cap))

    assert seen == MAX_STEPS
    assert raised.value.code == "script_too_long"
    assert over_cap.pulled == MAX_STEPS + 1
