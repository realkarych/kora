import json
import os
import stat
import sys
from pathlib import Path

from tests.measurements.storage_evidence_common import read_json
from tests.plugins.storage.pressure_controller import read_manifest, recover
from tests.plugins.storage.pressure_models import PressureFailure, PressureReport


def preserve_owner(manifest: Path, output: Path, prefix: str) -> None:
    record = read_manifest(manifest)
    (output / f"{prefix}.manifest.json").write_text(
        record.model_dump_json(), encoding="utf-8"
    )
    child = manifest.parent / "child-report.json"
    if child.exists():
        report = PressureReport.model_validate(read_json(child, 16384))
        (output / f"{prefix}.child.json").write_text(
            report.model_dump_json(), encoding="utf-8"
        )
    failure = manifest.parent / "child-stderr.txt"
    if failure.exists():
        descriptor = os.open(failure, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > 1048576
            ):
                raise PressureFailure("failure_output_identity")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(1048577)
            if len(encoded) > 1048576:
                raise PressureFailure("failure_output_limit")
        finally:
            os.close(descriptor)
        (output / f"{prefix}.stderr.txt").write_bytes(encoded)


def recover_fixtures(root: Path, output: Path) -> None:
    root = root.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    manifests = tuple(sorted(root.rglob("pressure-manifest.json")))
    for index, manifest in enumerate(manifests):
        preserve_owner(manifest, output, f"before-{index}")
    for index, manifest in enumerate(manifests):
        primary = manifest.parent / "primary-report.json"
        if primary.exists():
            report = PressureReport.model_validate(read_json(primary, 16384))
            (output / f"primary-{index}.json").write_text(
                report.model_dump_json(), encoding="utf-8"
            )
        try:
            recover(manifest)
        except PressureFailure as error:
            failures.append(error.code)
        finally:
            preserve_owner(manifest, output, f"after-{index}")
        final = manifest.parent / "report.json"
        if final.exists():
            report = PressureReport.model_validate(read_json(final, 16384))
            (output / f"final-{index}.json").write_text(
                report.model_dump_json(), encoding="utf-8"
            )
            if not report.resources_closed:
                failures.append("fixture_resources_not_closed")
        else:
            failures.append("fixture_report_missing")
    (output / "cleanup.json").write_text(
        json.dumps(
            {
                "manifests": len(manifests),
                "failures": failures,
            }
        ),
        encoding="utf-8",
    )
    if failures:
        raise PressureFailure("fixture_cleanup_incomplete")


if __name__ == "__main__":
    recover_fixtures(Path(sys.argv[1]), Path(sys.argv[2]))
