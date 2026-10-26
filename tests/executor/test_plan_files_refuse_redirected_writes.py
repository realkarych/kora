import os
from pathlib import Path

import pytest

from kora.executor.failures import ExecutorError, Phase
from kora.executor.files import write_plan_files
from kora.protocols.vcs import PlanFile


def plan_file(path: str) -> PlanFile:
    return PlanFile(area="control", path=path, content=b"private", mode=0o600)


def test_plan_files_refuse_redirected_writes(tmp_path: Path) -> None:
    control = tmp_path / "control"
    outside = tmp_path / "outside"
    control.mkdir()
    outside.mkdir()
    (outside / "victim").write_text("untouched")
    (control / "parent").symlink_to(outside)
    (control / "link").symlink_to(outside / "victim")
    os.link(outside / "victim", control / "hard")
    cases = ["parent/victim", "link", "hard"]

    refused: list[str] = []
    for path in cases:
        with pytest.raises(ExecutorError) as raised:
            write_plan_files((plan_file(path),), control, None, Phase.CHECKOUT)
        refused.append(raised.value.code)

    assert refused == ["plan_file_refused"] * 3
    assert (outside / "victim").read_text() == "untouched"


def test_plan_files_are_written_privately(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir()

    write_plan_files((plan_file("nested/helper"),), control, None, Phase.CHECKOUT)

    written = control / "nested" / "helper"
    assert written.read_bytes() == b"private"
    assert written.stat().st_mode & 0o777 == 0o600
    assert (control / "nested").stat().st_mode & 0o777 == 0o700


def test_publication_files_need_a_publication_area(tmp_path: Path) -> None:
    file = PlanFile(area="publication", path="helper", content=b"x", mode=0o600)

    with pytest.raises(ExecutorError) as raised:
        write_plan_files((file,), tmp_path, None, Phase.CHECKOUT)

    assert raised.value.code == "plan_file_area_refused"
