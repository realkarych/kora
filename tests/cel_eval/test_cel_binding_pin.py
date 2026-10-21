import tomllib
from pathlib import Path
from typing import cast

from kora.cel_eval.binding import DISTRIBUTION, PINNED_VERSION

ROOT: Path = Path(__file__).resolve().parents[2]
PYPROJECT: Path = ROOT / "pyproject.toml"
LOCK: Path = ROOT / "uv.lock"
COPY_DIRECTORY: Path = ROOT / ".github" / "osv" / "common-expression-language"
PURE_WHEEL_TAGS: tuple[str, str] = ("none", "any")
WCWIDTH_CONSTRAINT: str = "wcwidth==0.8.5"


def read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def table(document: dict[str, object], *keys: str) -> dict[str, object]:
    current = document
    for key in keys:
        value = current[key]
        assert isinstance(value, dict)
        current = cast(dict[str, object], value)
    return current


def tables(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    items = cast(list[object], value)
    assert all(isinstance(item, dict) for item in items)
    return [cast(dict[str, object], item) for item in items]


def texts(value: object) -> list[str]:
    assert isinstance(value, list)
    items = cast(list[object], value)
    assert all(isinstance(item, str) for item in items)
    return [str(item) for item in items]


def locked_packages(lock: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(package["name"]): package for package in tables(lock["package"])}


def dependency_closure(
    packages: dict[str, dict[str, object]], root: str
) -> frozenset[str]:
    seen: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for dependency in tables(packages[name].get("dependencies", [])):
            pending.append(str(dependency["name"]))
    return frozenset(seen)


def wheel_names(package: dict[str, object]) -> list[str]:
    return [str(wheel["url"]).rsplit("/", 1)[-1] for wheel in tables(package["wheels"])]


def is_pure_wheel(name: str) -> bool:
    abi, platform = name.removesuffix(".whl").split("-")[-2:]
    return (abi, platform) == PURE_WHEEL_TAGS


def root_crate_versions(lock: dict[str, object]) -> list[str]:
    return [
        str(package["version"])
        for package in tables(lock["package"])
        if package["name"] == "cel" and "source" not in package
    ]


def test_cel_binding_pin_is_consistent() -> None:
    pyproject = read_toml(PYPROJECT)
    lock = read_toml(LOCK)
    crate_lock = read_toml(COPY_DIRECTORY / "Cargo.lock")
    override = tables(
        read_toml(COPY_DIRECTORY / "osv-scanner.toml")["PackageOverrides"]
    )

    packages = locked_packages(lock)
    uv_settings = table(pyproject, "tool", "uv")
    extra = texts(table(pyproject, "project", "optional-dependencies")["cel"])
    closure = dependency_closure(packages, DISTRIBUTION) - {DISTRIBUTION}
    impure = {
        name
        for name in closure
        if not all(is_pure_wheel(wheel) for wheel in wheel_names(packages[name]))
    }

    assert extra == [f"{DISTRIBUTION}=={PINNED_VERSION}"]
    assert packages[DISTRIBUTION]["version"] == PINNED_VERSION
    assert root_crate_versions(crate_lock) == [PINNED_VERSION]
    assert [entry["version"] for entry in override] == [PINNED_VERSION]
    assert DISTRIBUTION in texts(uv_settings["no-build-package"])
    assert texts(uv_settings["constraint-dependencies"]) == [WCWIDTH_CONSTRAINT]
    assert tables(table(lock, "manifest")["constraints"]) == [
        {"name": "wcwidth", "specifier": "==0.8.5"}
    ]
    assert packages["wcwidth"]["version"] == "0.8.5"
    assert "wcwidth" in closure
    assert impure == set()
