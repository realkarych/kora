import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from tests.measurements.lifetime_evidence import (
    PARENT_EXIT,
    ParentResult,
    StepOutcome,
    collect_cohorts,
    collect_full_disk,
)


@pytest.fixture
def short_home() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="kora-ci-") as location:
        yield Path(location).resolve()


def write_parent_exit(folder: Path, *, reaped: bool) -> None:
    result = ParentResult(pid=4242, returncode=0 if reaped else None, reaped=reaped)
    (folder / PARENT_EXIT).write_text(result.model_dump_json())


def evidence_layout(
    tmp_path: Path, short_home: Path
) -> tuple[Path, Path, tuple[Path, ...]]:
    output = tmp_path / "evidence"
    output.mkdir(mode=0o700)
    temporary = short_home
    artifacts = output / "report.artifacts"
    artifacts.mkdir(mode=0o700)
    homes: list[Path] = []
    for index in range(3):
        home = temporary / f"kora-life-{index:08d}"
        home.mkdir(mode=0o700)
        (home / "journal").write_bytes(b"unknown recovery evidence")
        folder = artifacts / str(index)
        folder.mkdir(mode=0o700)
        (folder / "home.txt").write_text(str(home), encoding="ascii")
        write_parent_exit(folder, reaped=True)
        homes.append(home)
    return output, temporary, tuple(homes)


def publication(output: Path) -> dict[str, object]:
    value: object = json.loads((output / "publication.json").read_text())
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def items(output: Path) -> list[dict[str, object]]:
    value = publication(output)["items"]
    assert isinstance(value, list)
    return [cast(dict[str, object], item) for item in cast(list[object], value)]


@pytest.mark.parametrize("outcome", [StepOutcome.FAILURE, StepOutcome.CANCELLED])
def test_failed_lifetime_publication_preserves_unknown_bytes_and_links(
    tmp_path: Path, short_home: Path, outcome: StepOutcome
) -> None:
    output, temporary, homes = evidence_layout(tmp_path, short_home)
    outside = tmp_path / "outside"
    outside.write_bytes(b"not archive content")
    (homes[0] / "link").symlink_to(outside)

    passed = collect_cohorts(output, outcome, temporary)

    assert passed
    assert publication(output)["verified"] is True
    for index, home in enumerate(homes):
        assert (home / "journal").read_bytes() == b"unknown recovery evidence"
        with tarfile.open(output / f"retained-{index}.tar") as archive:
            entry = archive.extractfile("state/journal")
            assert entry is not None
            assert entry.read() == b"unknown recovery evidence"
            if index == 0:
                assert archive.getmember("state/link").issym()
                assert archive.getmember("state/link").linkname == str(outside)


def test_successful_lifetime_publication_requires_removed_homes(
    tmp_path: Path, short_home: Path
) -> None:
    output, temporary, homes = evidence_layout(tmp_path, short_home)
    for home in homes:
        shutil.rmtree(home)

    passed = collect_cohorts(output, StepOutcome.SUCCESS, temporary)

    assert passed
    assert publication(output) == {
        "verified": True,
        "items": [
            {"index": index, "state": "removed", "reason": "complete"}
            for index in range(3)
        ],
    }
    assert not tuple(output.glob("*.tar"))


@pytest.mark.parametrize(
    "case",
    [
        "missing_unknown",
        "foreign_reference",
        "oversized_reference",
        "non_ascii_reference",
        "symlink_reference",
        "public_home",
        "symlink_home",
        "stale_archive",
    ],
)
def test_invalid_lifetime_evidence_keeps_other_cohorts_and_fails(
    tmp_path: Path, short_home: Path, case: str
) -> None:
    output, temporary, homes = evidence_layout(tmp_path, short_home)
    reference = output / "report.artifacts/0/home.txt"
    if case == "missing_unknown":
        shutil.rmtree(homes[0])
    elif case == "foreign_reference":
        reference.write_text(str(tmp_path / homes[0].name))
    elif case == "oversized_reference":
        reference.write_bytes(b"x" * 129)
    elif case == "non_ascii_reference":
        reference.write_bytes(b"\xff")
    elif case == "symlink_reference":
        reference.unlink()
        reference.symlink_to(output / "report.artifacts/1/home.txt")
    elif case == "public_home":
        homes[0].chmod(0o755)
    elif case == "symlink_home":
        retained = temporary / "retained"
        homes[0].rename(retained)
        homes[0].symlink_to(retained, target_is_directory=True)
    else:
        (output / "retained-0.tar").write_bytes(b"prior evidence")

    passed = collect_cohorts(output, StepOutcome.FAILURE, temporary)

    assert not passed
    assert publication(output)["verified"] is False
    assert (output / "retained-1.tar").is_file()
    assert (output / "retained-2.tar").is_file()
    assert all((home / "journal").exists() for home in homes[1:])
    if case == "stale_archive":
        assert (output / "retained-0.tar").read_bytes() == b"prior evidence"


def test_lifetime_publication_rejects_fifo_reference_without_waiting(
    tmp_path: Path,
    short_home: Path,
) -> None:
    output, temporary, _ = evidence_layout(tmp_path, short_home)
    reference = output / "report.artifacts/0/home.txt"
    reference.unlink()
    os.mkfifo(reference, 0o600)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.measurements.lifetime_evidence",
            "cohorts",
            str(output),
            "failure",
            str(temporary),
        ],
        capture_output=True,
        check=False,
        timeout=3,
    )

    assert result.returncode == 1
    assert publication(output)["verified"] is False
    assert (output / "retained-1.tar").exists()
    assert (output / "retained-2.tar").exists()


def test_retained_success_cannot_be_published_as_removed(
    tmp_path: Path, short_home: Path
) -> None:
    output, temporary, homes = evidence_layout(tmp_path, short_home)

    passed = collect_cohorts(output, StepOutcome.SUCCESS, temporary)

    assert not passed
    assert publication(output)["verified"] is False
    assert all((output / f"retained-{index}.tar").exists() for index in range(3))
    assert all(home.exists() for home in homes)


@pytest.mark.parametrize(
    "record",
    ["unreaped", "missing", "malformed", "linked", "lax", "inconsistent"],
)
def test_unsettled_cohort_parent_keeps_its_archive_but_stays_unverified(
    tmp_path: Path, short_home: Path, record: str
) -> None:
    output, temporary, _ = evidence_layout(tmp_path, short_home)
    folder = output / "report.artifacts/1"
    exit_record = folder / PARENT_EXIT
    if record == "unreaped":
        write_parent_exit(folder, reaped=False)
    elif record == "missing":
        exit_record.unlink()
    elif record == "malformed":
        exit_record.write_text("{}")
    elif record == "lax":
        exit_record.write_text('{"pid": "4242", "returncode": 0, "reaped": "yes"}')
    elif record == "inconsistent":
        exit_record.write_text('{"pid": 4242, "returncode": null, "reaped": true}')
    else:
        exit_record.unlink()
        exit_record.symlink_to(output / "report.artifacts/0" / PARENT_EXIT)

    passed = collect_cohorts(output, StepOutcome.CANCELLED, temporary)

    reason = "parent_unreaped" if record == "unreaped" else "parent_exit_unavailable"
    assert not passed
    assert publication(output) == {
        "verified": False,
        "items": [
            {"index": 0, "state": "retained", "reason": "complete"},
            {"index": 1, "state": "unverified", "reason": reason},
            {"index": 2, "state": "retained", "reason": "complete"},
        ],
    }
    assert all((output / f"retained-{index}.tar").is_file() for index in range(3))


def test_removed_home_of_an_unreaped_parent_is_not_published_as_removed(
    tmp_path: Path, short_home: Path
) -> None:
    output, temporary, homes = evidence_layout(tmp_path, short_home)
    for home in homes:
        shutil.rmtree(home)
    write_parent_exit(output / "report.artifacts/2", reaped=False)

    passed = collect_cohorts(output, StepOutcome.SUCCESS, temporary)

    assert not passed
    assert [item["state"] for item in items(output)] == [
        "removed",
        "removed",
        "unverified",
    ]
    assert items(output)[2]["reason"] == "parent_unreaped"


def test_full_disk_publication_captures_before_root_disposal(tmp_path: Path) -> None:
    output = tmp_path / "evidence"
    output.mkdir(mode=0o700)
    root = tmp_path / "mounted-private-state"
    root.mkdir(mode=0o700)
    (root / "journal").write_bytes(b"unresolved authority")

    passed = collect_full_disk(root, output)

    assert passed
    assert (root / "journal").read_bytes() == b"unresolved authority"
    with tarfile.open(output / "state.tar") as archive:
        entry = archive.extractfile("state/journal")
        assert entry is not None
        assert entry.read() == b"unresolved authority"
    assert publication(output)["verified"] is True
