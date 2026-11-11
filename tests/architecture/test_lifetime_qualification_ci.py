import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MEASUREMENT_SCRIPT = ROOT / "scripts/ci/measure_conformance_lifetime.sh"
INVENTORY_SCRIPT = ROOT / "scripts/ci/check_evidence_inventory.sh"
MODULE = "tests/measurements/test_conformance_lifetime.py"
SHARDS = ("fixture", "listing", "command")
REVISION = "a" * 40
RECEIPT = "source-revision.txt"
SHARD_FILES = {
    "fixture": ("report.json", "publication.json", "tests.xml", RECEIPT),
    "listing": ("listing.json", "tests.xml", RECEIPT),
    "command": (
        "command.json",
        "command-artifacts/publication.json",
        "tests.xml",
        RECEIPT,
    ),
}
REQUIRED_PATHS = [
    (shard, name) for shard, names in SHARD_FILES.items() for name in names
]
INVENTORY_DAMAGE = [
    ("complete", "", ""),
    ("extra_artifact", "", ""),
    ("foreign_revision", "fixture", RECEIPT),
    *(("missing_shard", shard, "") for shard in SHARD_FILES),
    *(("linked_shard", shard, "") for shard in SHARD_FILES),
    *(
        (damage, shard, name)
        for damage in ("missing", "linked", "empty", "directory")
        for shard, name in REQUIRED_PATHS
    ),
]


def collected_nodes() -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "measurement",
            MODULE,
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    return sorted(
        line
        for line in result.stdout.decode().splitlines()
        if line.startswith(f"{MODULE}::")
    )


def shard_command(commands: str, shard: str) -> list[str]:
    return [
        "/bin/bash",
        "-c",
        f'source "$1"\nselect_lifetime_shard "$2"\n{commands}',
        "bash",
        str(MEASUREMENT_SCRIPT),
        shard,
    ]


def shard_selection(shard: str) -> str:
    result = subprocess.run(
        shard_command('printf "%s" "$selection"', shard),
        capture_output=True,
        check=True,
        timeout=5,
    )
    return result.stdout.decode()


def prepare_evidence(
    workspace: Path, shard: str, checkout: str, requested: str
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        shard_command('prepare_lifetime_evidence\nprintf "%s" "$evidence"', shard),
        cwd=workspace,
        env={
            **os.environ,
            "KORA_LIFETIME_CHECKOUT_REVISION": checkout,
            "KORA_MEASUREMENT_REVISION": requested,
        },
        capture_output=True,
        check=False,
        timeout=5,
    )


def source_receipt(checkout: str) -> str:
    return f"Actual checkout: {checkout}\nRequested measurement revision: {REVISION}\n"


def shard_folder(measurements: Path, shard: str) -> Path:
    return measurements / f"conformance-lifetime-{REVISION}-{shard}"


def write_complete_evidence(root: Path) -> Path:
    measurements = root / "measurements"
    for shard, name in REQUIRED_PATHS:
        path = shard_folder(measurements, shard) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_receipt(REVISION) if name == RECEIPT else "{}\n")
    return measurements


def damage_evidence(measurements: Path, damage: str, shard: str, name: str) -> None:
    folder = shard_folder(measurements, shard)
    path = folder / name
    outside = measurements.parent / "outside"
    if damage == "extra_artifact":
        shard_folder(measurements, "unexpected").mkdir()
    elif damage == "foreign_revision":
        path.write_text(source_receipt("b" * 40))
    elif damage == "missing_shard":
        folder.rename(outside)
    elif damage == "linked_shard":
        folder.rename(outside)
        folder.symlink_to(outside, target_is_directory=True)
    elif damage == "missing":
        path.unlink()
    elif damage == "linked":
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    elif damage == "empty":
        path.write_text("")
    elif damage == "directory":
        path.unlink()
        (path / "content").mkdir(parents=True)


def test_lifetime_shards_select_every_measurement_exactly_once() -> None:
    selected = sorted(shard_selection(shard) for shard in SHARDS)

    nodes = collected_nodes()

    assert selected == nodes


def test_unknown_lifetime_shard_is_refused() -> None:
    result = subprocess.run(
        shard_command('printf "%s" "$selection"', "installed"),
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode != 0
    assert result.stdout == b""


@pytest.mark.parametrize("shard", SHARDS)
def test_lifetime_measurement_prepares_private_evidence_in_a_fresh_checkout(
    tmp_path: Path, shard: str
) -> None:
    fresh = prepare_evidence(tmp_path, shard, "a" * 40, "b" * 40)
    repeated = prepare_evidence(tmp_path, shard, "c" * 40, "d" * 40)

    assert (fresh.returncode, fresh.stderr) == (0, b"")
    assert repeated.returncode != 0
    assert repeated.stderr != b""
    evidence = tmp_path / fresh.stdout.decode()
    assert evidence.parent == tmp_path / "measurements"
    observed = evidence.lstat()
    assert stat.S_ISDIR(observed.st_mode)
    assert stat.S_IMODE(observed.st_mode) == 0o700
    assert (evidence / RECEIPT).read_text() == (
        f"Actual checkout: {'a' * 40}\nRequested measurement revision: {'b' * 40}\n"
    )


@pytest.mark.parametrize(("damage", "shard", "name"), INVENTORY_DAMAGE)
def test_lifetime_inventory_accepts_only_every_shard_with_its_receipt(
    tmp_path: Path, damage: str, shard: str, name: str
) -> None:
    measurements = write_complete_evidence(tmp_path)
    damage_evidence(measurements, damage, shard, name)

    result = subprocess.run(
        ["/bin/bash", str(INVENTORY_SCRIPT), "lifetime"],
        cwd=tmp_path,
        env={**os.environ, "KORA_MEASUREMENT_REVISION": REVISION},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (damage == "complete")
