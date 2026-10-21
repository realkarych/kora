import base64
import hashlib
import importlib.metadata
import json
import tomllib
from pathlib import Path
from typing import cast

from kora.cel_eval.binding import DISTRIBUTION, MODULE, PINNED_VERSION
from tests.cel_eval.binding_baseline import WHEELS

ROOT: Path = Path(__file__).resolve().parents[2]
CRATE_LOCK: Path = (
    ROOT / ".github" / "osv" / "common-expression-language" / "Cargo.lock"
)
SBOM_ENTRY: str = "sboms/cel.cyclonedx.json"
SBOM_HASH: str = "SHA-256"

Crate = tuple[str, str, str]


def as_table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def as_tables(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [as_table(item) for item in cast(list[object], value)]


def record_digest(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def recorded(distribution: importlib.metadata.Distribution) -> dict[str, str]:
    files = distribution.files
    assert files is not None
    return {
        str(entry): f"{entry.hash.mode}={entry.hash.value}"
        for entry in files
        if entry.hash is not None
    }


def wheel_tag(distribution: importlib.metadata.Distribution) -> str:
    metadata = distribution.read_text("WHEEL")
    assert metadata is not None
    tags = [
        line.removeprefix("Tag: ")
        for line in metadata.splitlines()
        if line.startswith("Tag: ")
    ]
    return tags[0]


def sbom_crate(component: dict[str, object]) -> Crate:
    hashes = [
        entry for entry in as_tables(component["hashes"]) if entry["alg"] == SBOM_HASH
    ]
    assert len(hashes) == 1
    return str(component["name"]), str(component["version"]), str(hashes[0]["content"])


def locked_crates() -> set[Crate]:
    with CRATE_LOCK.open("rb") as stream:
        document = tomllib.load(stream)
    return {
        (str(package["name"]), str(package["version"]), str(package["checksum"]))
        for package in as_tables(document["package"])
        if "checksum" in package
    }


def test_cel_bundled_crate_lock_covers_installed_wheel() -> None:
    distribution = importlib.metadata.distribution(DISTRIBUTION)
    records = recorded(distribution)
    baseline = WHEELS[wheel_tag(distribution)]
    sbom_bytes = Path(
        str(distribution.locate_file(f"{distribution_info(distribution)}/{SBOM_ENTRY}"))
    ).read_bytes()
    native_bytes = Path(
        str(distribution.locate_file(baseline.native_module))
    ).read_bytes()

    sbom = as_table(json.loads(sbom_bytes))
    root = as_table(as_table(sbom["metadata"])["component"])
    crates = [sbom_crate(component) for component in as_tables(sbom["components"])]

    assert (
        records[f"{distribution_info(distribution)}/{SBOM_ENTRY}"]
        == baseline.sbom_record
    )
    assert record_digest(sbom_bytes) == baseline.sbom_record
    assert records[baseline.native_module] == baseline.native_record
    assert record_digest(native_bytes) == baseline.native_record
    assert baseline.native_module.startswith(f"{MODULE}/")
    assert (root["name"], root["version"]) == ("cel", PINNED_VERSION)
    assert len(crates) == baseline.sbom_components
    assert set(crates) <= locked_crates()


def distribution_info(distribution: importlib.metadata.Distribution) -> str:
    return f"{DISTRIBUTION.replace('-', '_')}-{distribution.version}.dist-info"
