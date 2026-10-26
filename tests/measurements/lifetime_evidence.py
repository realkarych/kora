import json
import os
import re
import stat
import sys
import tarfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StepOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    UNKNOWN = ""


class EvidenceState(StrEnum):
    REMOVED = "removed"
    RETAINED = "retained"
    UNVERIFIED = "unverified"


class EvidenceReason(StrEnum):
    COMPLETE = "complete"
    INVALID_REFERENCE = "invalid_reference"
    UNAVAILABLE = "unavailable"
    MISSING_UNKNOWN = "missing_unknown"
    UNEXPECTED_RETENTION = "unexpected_retention"


class EvidenceError(ValueError):
    def __init__(self, reason: EvidenceReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class EvidenceItem:
    index: int
    state: EvidenceState
    reason: EvidenceReason = EvidenceReason.COMPLETE


def private_directory(path: Path) -> None:
    observed = path.lstat()
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or path.resolve(strict=True) != path
    ):
        raise EvidenceError(EvidenceReason.INVALID_REFERENCE)


def home_reference(path: Path, temporary_root: Path) -> Path:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or not 1 <= observed.st_size <= 128
        ):
            raise EvidenceError(EvidenceReason.INVALID_REFERENCE)
        encoded = os.read(descriptor, 129)
    finally:
        os.close(descriptor)
    if len(encoded) > 128:
        raise EvidenceError(EvidenceReason.INVALID_REFERENCE)
    try:
        home = Path(encoded.decode("ascii"))
    except UnicodeError as failure:
        raise EvidenceError(EvidenceReason.INVALID_REFERENCE) from failure
    if (
        not temporary_root.is_absolute()
        or temporary_root.resolve(strict=True) != temporary_root
        or home.parent != temporary_root
        or re.fullmatch(r"kora-life-[a-z0-9_]{8}", home.name) is None
        or str(home).encode("ascii") != encoded
    ):
        raise EvidenceError(EvidenceReason.INVALID_REFERENCE)
    return home


def archive_private(root: Path, output: Path) -> None:
    private_directory(root)
    with tarfile.open(output, mode="x", dereference=False) as archive:
        archive.add(root, arcname="state", recursive=True)


def cohort_evidence(
    folder: Path, output: Path, index: int, outcome: StepOutcome, temporary_root: Path
) -> EvidenceItem:
    private_directory(folder)
    home = home_reference(folder / "home.txt", temporary_root)
    try:
        home.lstat()
    except FileNotFoundError:
        if outcome is StepOutcome.SUCCESS:
            return EvidenceItem(index, EvidenceState.REMOVED)
        raise EvidenceError(EvidenceReason.MISSING_UNKNOWN) from None
    archive_private(home, output / f"retained-{index}.tar")
    if outcome is StepOutcome.SUCCESS:
        raise EvidenceError(EvidenceReason.UNEXPECTED_RETENTION)
    return EvidenceItem(index, EvidenceState.RETAINED)


def write_receipt(output: Path, items: tuple[EvidenceItem, ...]) -> bool:
    passed = all(item.state is not EvidenceState.UNVERIFIED for item in items)
    result = {
        "verified": passed,
        "items": [
            {
                "index": item.index,
                "state": item.state.value,
                "reason": item.reason.value,
            }
            for item in items
        ],
    }
    encoded = json.dumps(result, separators=(",", ":")) + "\n"
    with (output / "publication.json").open("x", encoding="utf-8") as destination:
        destination.write(encoded)
    sys.stdout.write(encoded)
    return passed


def collect_cohorts(output: Path, outcome: StepOutcome, temporary_root: Path) -> bool:
    private_directory(output)
    items: list[EvidenceItem] = []
    for index in range(3):
        try:
            item = cohort_evidence(
                output / "report.artifacts" / str(index),
                output,
                index,
                outcome,
                temporary_root,
            )
        except EvidenceError as failure:
            item = EvidenceItem(index, EvidenceState.UNVERIFIED, failure.reason)
        except (OSError, tarfile.TarError):
            item = EvidenceItem(
                index, EvidenceState.UNVERIFIED, EvidenceReason.UNAVAILABLE
            )
        items.append(item)
    return write_receipt(output, tuple(items))


def collect_full_disk(root: Path, output: Path) -> bool:
    private_directory(output)
    try:
        archive_private(root, output / "state.tar")
    except EvidenceError as failure:
        item = EvidenceItem(0, EvidenceState.UNVERIFIED, failure.reason)
    except (OSError, tarfile.TarError):
        item = EvidenceItem(0, EvidenceState.UNVERIFIED, EvidenceReason.UNAVAILABLE)
    else:
        item = EvidenceItem(0, EvidenceState.RETAINED)
    return write_receipt(output, (item,))


def main(arguments: list[str]) -> int:
    if len(arguments) == 4 and arguments[0] == "cohorts":
        passed = collect_cohorts(
            Path(arguments[1]), StepOutcome(arguments[2]), Path(arguments[3])
        )
    elif len(arguments) == 3 and arguments[0] == "full-disk":
        passed = collect_full_disk(Path(arguments[1]), Path(arguments[2]))
    else:
        raise ValueError("invalid lifetime evidence command")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
