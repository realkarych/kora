import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictStr

from kora.protocols.common import BoundaryModel
from tests.measurements.storage_evidence_common import (
    EvidenceFailure,
    junit_nodes,
    read_json,
)

ENVIRONMENTS: tuple[tuple[str, str, str], ...] = (
    ("ubuntu-24.04", "3.11.17", "sqlite,postgresql"),
    ("ubuntu-24.04", "3.15.0", "sqlite,postgresql"),
    ("macos-15", "3.11.17", "sqlite"),
)
LANES = ("general", "storage")
GENERAL_ARGUMENTS = (
    "--ignore=tests/plugins/storage",
    "--ignore=tests/conformance/test_artifact_storage_conformance.py",
)
STORAGE_ARGUMENTS = (
    "tests/plugins/storage",
    "tests/conformance/test_artifact_storage_conformance.py",
)
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")]
Identifier = Annotated[StrictStr, Field(pattern=r"^[1-9][0-9]*$")]


class LaneReceipt(BoundaryModel):
    version: Literal[1]
    run_id: Identifier
    run_attempt: Identifier
    checkout_revision: Revision
    requested_revision: Revision
    operating_system: Literal["ubuntu-24.04", "macos-15"]
    python: Literal["3.11.17", "3.15.0"]
    databases: Literal["sqlite", "sqlite,postgresql"]
    lane: Literal["general", "storage"]
    arguments: tuple[StrictStr, ...]
    files: dict[StrictStr, Digest]


def evidence_bytes(path: Path, maximum: int = 16777216) -> bytes:
    information = path.lstat()
    if not stat.S_ISREG(information.st_mode) or information.st_size > maximum:
        raise EvidenceFailure("scaffold evidence is not a bounded regular file")
    with path.open("rb") as stream:
        encoded = stream.read(maximum + 1)
    if len(encoded) > maximum:
        raise EvidenceFailure("scaffold evidence grew beyond its input bound")
    return encoded


def collection_nodes(path: Path) -> frozenset[str]:
    nodes: set[str] = set()
    for line in evidence_bytes(path).decode().splitlines():
        if not line:
            continue
        if re.fullmatch(r"[0-9]+(?:/[0-9]+)? tests? collected.*", line):
            continue
        filename, delimiter, test = line.partition("::")
        if (
            not delimiter
            or not filename.startswith("tests/")
            or not filename.endswith(".py")
        ):
            raise EvidenceFailure("scaffold collection contains unexpected output")
        name, parameter, suffix = test.partition("[")
        identity = filename[:-3].replace("/", ".") + "." + name.replace("::", ".")
        if parameter:
            identity += parameter + suffix
        if identity in nodes:
            raise EvidenceFailure("scaffold collection contains duplicate tests")
        nodes.add(identity)
    if not nodes:
        raise EvidenceFailure("scaffold collection is empty")
    return frozenset(nodes)


def expected_files(lane: str) -> frozenset[str]:
    files = {
        "selected.txt",
        "selected.stderr",
        "collection.exit",
        "tests.stdout",
        "tests.stderr",
        "tests.exit",
        "junit.xml",
    }
    if lane == "general":
        files.update({"complete.txt", "complete.stderr", "complete.exit"})
    return frozenset(files)


def validate_lane(directory: Path, receipt: LaneReceipt) -> frozenset[str]:
    expected = expected_files(receipt.lane)
    if receipt.files.keys() != expected:
        raise EvidenceFailure("scaffold receipt has unexpected file coverage")
    if frozenset(path.name for path in directory.iterdir()) != expected | {
        "receipt.json"
    }:
        raise EvidenceFailure("scaffold artifact has unexpected file coverage")
    arguments = GENERAL_ARGUMENTS if receipt.lane == "general" else STORAGE_ARGUMENTS
    if receipt.arguments != arguments:
        raise EvidenceFailure("scaffold receipt has different pytest arguments")
    for filename, digest in receipt.files.items():
        encoded = evidence_bytes(directory / filename)
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise EvidenceFailure("scaffold evidence hash differs from its receipt")
        if filename.endswith(".exit") and encoded != b"0\n":
            raise EvidenceFailure("scaffold execution did not succeed")
    selected = collection_nodes(directory / "selected.txt")
    if junit_nodes(directory / "junit.xml", 16777216) != selected:
        raise EvidenceFailure("scaffold JUnit differs from its collected inventory")
    return selected


def record_lane(
    directory: Path,
    run_id: str,
    run_attempt: str,
    checkout_revision: str,
    requested_revision: str,
    operating_system: str,
    python: str,
    databases: str,
    lane: str,
    arguments: tuple[str, ...],
) -> LaneReceipt:
    if (operating_system, python, databases) not in ENVIRONMENTS or lane not in LANES:
        raise EvidenceFailure("scaffold execution has an unknown environment or lane")
    receipt = LaneReceipt.model_validate(
        {
            "version": 1,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "checkout_revision": checkout_revision,
            "requested_revision": requested_revision,
            "operating_system": operating_system,
            "python": python,
            "databases": databases,
            "lane": lane,
            "arguments": arguments,
            "files": {
                filename: hashlib.sha256(
                    evidence_bytes(directory / filename)
                ).hexdigest()
                for filename in expected_files(lane)
            },
        }
    )
    (directory / "receipt.json").write_text(receipt.model_dump_json(indent=2))
    validate_lane(directory, receipt)
    return receipt


def verify_scaffold(
    root: Path,
    run_id: str,
    run_attempt: str,
    checkout_revision: str,
    requested_revision: str,
) -> dict[str, int]:
    expected = {
        f"scaffold-tests-{operating_system}-{python}-{lane}"
        for operating_system, python, _ in ENVIRONMENTS
        for lane in LANES
    }
    if {path.name for path in root.iterdir()} != expected:
        raise EvidenceFailure("scaffold evidence does not cover exactly six lanes")
    total = 0
    for operating_system, python, databases in ENVIRONMENTS:
        inventories: dict[str, frozenset[str]] = {}
        complete: frozenset[str] = frozenset()
        for lane in LANES:
            directory = root / f"scaffold-tests-{operating_system}-{python}-{lane}"
            if directory.is_symlink() or not directory.is_dir():
                raise EvidenceFailure("scaffold lane is not an owned directory")
            evidence_bytes(directory / "receipt.json", 16384)
            receipt = LaneReceipt.model_validate(
                read_json(directory / "receipt.json", 16384)
            )
            actual = (
                receipt.run_id,
                receipt.run_attempt,
                receipt.checkout_revision,
                receipt.requested_revision,
                receipt.operating_system,
                receipt.python,
                receipt.databases,
                receipt.lane,
            )
            if actual != (
                run_id,
                run_attempt,
                checkout_revision,
                requested_revision,
                operating_system,
                python,
                databases,
                lane,
            ):
                raise EvidenceFailure("scaffold receipt has mismatched provenance")
            inventories[lane] = validate_lane(directory, receipt)
            if lane == "general":
                complete = collection_nodes(directory / "complete.txt")
        general, storage = inventories["general"], inventories["storage"]
        if general & storage or general | storage != complete:
            raise EvidenceFailure("scaffold lanes do not partition the complete suite")
        total += len(complete)
    return {"environments": len(ENVIRONMENTS), "lanes": len(expected), "tests": total}


def main() -> None:
    operation, directory, run_id, attempt, checkout, requested, *environment = sys.argv[
        1:
    ]
    if operation == "record" and len(environment) >= 4:
        operating_system, python, databases, lane, *arguments = environment
        record_lane(
            Path(directory),
            run_id,
            attempt,
            checkout,
            requested,
            operating_system,
            python,
            databases,
            lane,
            tuple(arguments),
        )
    elif operation == "verify" and not environment:
        result = verify_scaffold(Path(directory), run_id, attempt, checkout, requested)
        sys.stdout.write(json.dumps(result) + "\n")
    else:
        raise EvidenceFailure("unknown scaffold evidence operation")


if __name__ == "__main__":
    main()
