import hashlib
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tests.support.workflows import action_name, workflow_document

ROOT = Path(__file__).resolve().parents[2]
COPY_DIRECTORY = ROOT / ".github" / "osv" / "common-expression-language"
CRATE_LOCK = COPY_DIRECTORY / "Cargo.lock"
OVERRIDE = COPY_DIRECTORY / "osv-scanner.toml"
CRATE_LOCK_SHA256 = "7115bab240a98243c913e2c97c1ba79ba94b501c8f1370f10820ad155ca107cd"
OVERRIDE_KEYS = frozenset({"name", "version", "ecosystem", "ignore", "reason"})
SCAN_ARGUMENTS = (
    "--lockfile=uv.lock",
    "--lockfile=web/pnpm-lock.yaml",
    "--lockfile=.github/osv/common-expression-language/Cargo.lock",
)
SCANNER_WORKFLOWS = "google/osv-scanner-action/.github/workflows"
SCANNING_JOBS = {
    "pull-request": (
        "github.event_name == 'pull_request'",
        f"{SCANNER_WORKFLOWS}/osv-scanner-reusable-pr.yml",
    ),
    "full-scan": (
        "github.event_name != 'pull_request'",
        f"{SCANNER_WORKFLOWS}/osv-scanner-reusable.yml",
    ),
}
UNSCANNED_DIRECTORIES = frozenset({".git", ".venv", "web/node_modules"})


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    has_source: bool


def as_table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def as_tables(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [as_table(item) for item in cast(list[object], value)]


def as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        return as_table(tomllib.load(stream))


def locked_packages(path: Path) -> list[LockedPackage]:
    return [
        LockedPackage(
            as_text(entry["name"]), as_text(entry["version"]), "source" in entry
        )
        for entry in as_tables(read_toml(path)["package"])
    ]


def is_unscanned(root: Path, directory: Path) -> bool:
    return directory.relative_to(root).as_posix() in UNSCANNED_DIRECTORIES


def scanner_configurations(root: Path) -> Iterator[Path]:
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            if entry.name == OVERRIDE.name:
                yield entry
            elif entry.is_dir() and not entry.is_symlink():
                if not is_unscanned(root, entry):
                    pending.append(entry)


def scan_configuration(value: object) -> tuple[object, ...]:
    job = as_table(value)
    inputs = as_table(job["with"])
    return (
        job["if"],
        action_name(job["uses"]),
        frozenset(inputs),
        tuple(as_text(inputs["scan-args"]).split()),
        inputs["fail-on-vuln"] is True,
    )


def test_osv_scans_pass_only_the_three_lockfiles_and_fail_on_findings() -> None:
    jobs = as_table(workflow_document("osv-scanner.yml")["jobs"])

    scans = {identity: scan_configuration(job) for identity, job in jobs.items()}

    assert scans == {
        identity: (
            condition,
            workflow,
            frozenset({"scan-args", "fail-on-vuln"}),
            SCAN_ARGUMENTS,
            True,
        )
        for identity, (condition, workflow) in SCANNING_JOBS.items()
    }


def test_osv_override_is_scoped_to_the_binding_root_crate() -> None:
    document = read_toml(OVERRIDE)
    packages = locked_packages(CRATE_LOCK)

    overrides = as_tables(document["PackageOverrides"])
    entry = overrides[0]
    matching = [
        package
        for package in packages
        if package.name == entry.get("name") and package.version == entry.get("version")
    ]
    digest = hashlib.sha256(CRATE_LOCK.read_bytes()).hexdigest()
    configurations = list(scanner_configurations(ROOT))
    copy_files = sorted(entry.name for entry in COPY_DIRECTORY.iterdir())

    assert set(document) == {"PackageOverrides"}
    assert len(overrides) == 1
    assert set(entry) == OVERRIDE_KEYS
    assert entry["name"] == "cel"
    assert entry["version"] == "0.10.0"
    assert entry["ecosystem"] == "crates.io"
    assert entry["ignore"] is True
    assert as_text(entry["reason"]).strip()
    assert matching == [LockedPackage("cel", "0.10.0", has_source=False)]
    assert digest == CRATE_LOCK_SHA256
    assert configurations == [OVERRIDE]
    assert not OVERRIDE.is_symlink()
    assert not CRATE_LOCK.is_symlink()
    assert copy_files == [CRATE_LOCK.name, OVERRIDE.name]
