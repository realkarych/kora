from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from kora.cel_eval.binding import PINNED_VERSION, Refusal
from tests.cel_eval.children import (
    LOAD_OUTCOME,
    MARKER_NAME,
    FakeDistribution,
    full_path,
    install_fake,
    isolated_path,
    isolated_site,
    run_child,
)

WRITE_MARKER: str = "import pathlib, sys\npathlib.Path(sys.argv[2]).write_text('x')\n"
PANIC_DECOY: str = (
    "decoy = type('PanicException', (BaseException,), {'__module__': 'pyo3_runtime'})\n"
)
GHOST_WITH_FILE: str = (
    "import sys, types\n"
    "ghost = types.ModuleType('cel.ghost')\n"
    "ghost.__file__ = '/elsewhere/ghost.py'\n"
    "sys.modules['cel.ghost'] = ghost\n"
)
GHOST_WITHOUT_FILE: str = (
    "import sys, types\nsys.modules['cel.ghost'] = types.ModuleType('cel.ghost')\n"
)


@dataclass(frozen=True)
class FakeLeg:
    name: str
    fakes: tuple[FakeDistribution, ...]
    refusal: Refusal


def fake(init: str = "", version: str = PINNED_VERSION) -> FakeDistribution:
    return FakeDistribution(version, {"cel/__init__.py": init})


FOREIGN_FILE: str = "__file__ = '/elsewhere/__init__.py'\n"
SIBLING_FILE: str = (
    "import os\n__file__ = os.path.join(os.path.dirname(__file__), 'cli.py')\n"
)
PIP_CACHE: dict[str, str] = {"cel/__pycache__/__init__.cpython-311.pyc": ""}

MISMATCHED: list[FakeLeg] = [
    FakeLeg("version", (fake(version="0.9.0"),), Refusal.VERSION_MISMATCH),
    FakeLeg("ambiguous", (fake(), fake()), Refusal.AMBIGUOUS),
    FakeLeg(
        "stale_dist_info",
        (replace(fake(), stale_versions=("9.9.9",)),),
        Refusal.AMBIGUOUS,
    ),
    FakeLeg("weak_hash", (replace(fake(), hash_mode="md5"),), Refusal.FILE_MISMATCH),
    FakeLeg("ghost_file", (fake(GHOST_WITH_FILE),), Refusal.MODULE_OUTSIDE_RECORD),
    FakeLeg(
        "ghost_without_file", (fake(GHOST_WITHOUT_FILE),), Refusal.MODULE_OUTSIDE_RECORD
    ),
    FakeLeg("foreign_file", (fake(FOREIGN_FILE),), Refusal.MODULE_OUTSIDE_RECORD),
    FakeLeg(
        "non_module_entry",
        (fake("import sys\nsys.modules['cel'] = 5\n"),),
        Refusal.MODULE_OUTSIDE_RECORD,
    ),
    FakeLeg(
        "import_failed",
        (fake("raise ImportError('UNREGISTERED-MARKER-7731')\n"),),
        Refusal.IMPORT_FAILED,
    ),
    FakeLeg("no_panic_class", (fake(),), Refusal.PANIC_CLASS_UNRESOLVED),
    FakeLeg(
        "two_panic_classes",
        (fake(PANIC_DECOY + PANIC_DECOY.replace("decoy", "other")),),
        Refusal.PANIC_CLASS_UNRESOLVED,
    ),
    FakeLeg(
        "same_version_metadata",
        (replace(fake(), egg_info=True),),
        Refusal.AMBIGUOUS,
    ),
    FakeLeg(
        "missing_recorded_file",
        (replace(fake(), missing={"cel/extra.py": ""}),),
        Refusal.FILE_MISMATCH,
    ),
    FakeLeg(
        "another_recorded_file",
        (
            replace(
                fake(SIBLING_FILE),
                files={"cel/__init__.py": SIBLING_FILE, "cel/cli.py": ""},
            ),
        ),
        Refusal.MODULE_OUTSIDE_RECORD,
    ),
    FakeLeg(
        "pip_bytecode_cache",
        (replace(fake(), cached=PIP_CACHE),),
        Refusal.PANIC_CLASS_UNRESOLVED,
    ),
]
UNCHECKED_CODE: list[FakeLeg] = [
    FakeLeg(
        "tampered",
        (replace(fake(WRITE_MARKER), tampered=frozenset({"cel/__init__.py"})),),
        Refusal.FILE_MISMATCH,
    ),
    FakeLeg(
        "stray",
        (
            replace(
                fake("from . import cli\n"),
                files={"cel/__init__.py": "from . import cli\n", "cel/cli.py": ""},
                unrecorded={"cel/cli/__init__.py": WRITE_MARKER},
            ),
        ),
        Refusal.FILE_MISMATCH,
    ),
    FakeLeg(
        "linked_file",
        (
            replace(
                fake("from . import sub\n"),
                linked_files={"cel/sub.py": WRITE_MARKER},
            ),
        ),
        Refusal.FILE_MISMATCH,
    ),
    FakeLeg(
        "linked_package",
        (replace(fake(WRITE_MARKER), linked_package=True),),
        Refusal.FILE_MISMATCH,
    ),
    FakeLeg(
        "linked_bytecode",
        (
            replace(
                fake(WRITE_MARKER),
                linked_files={"cel/__pycache__/__init__.cpython-311.pyc": ""},
            ),
        ),
        Refusal.FILE_MISMATCH,
    ),
    FakeLeg(
        "unlistable_directory",
        (replace(fake("from . import sub\n"), unlistable={"cel/sub": WRITE_MARKER}),),
        Refusal.FILE_MISMATCH,
    ),
    FakeLeg(
        "linked_directory",
        (replace(fake("from . import sub\n"), linked={"cel/sub": WRITE_MARKER}),),
        Refusal.FILE_MISMATCH,
    ),
]


def fake_sites(tmp_path: Path, leg: FakeLeg) -> list[Path]:
    base = isolated_site(tmp_path / "site")
    sites = [install_fake(base, leg.fakes[0])]
    for index, extra in enumerate(leg.fakes[1:]):
        directory = tmp_path / f"extra-{index}"
        directory.mkdir()
        sites.append(install_fake(directory, extra))
    return sites


def refused_line(refusal: Refusal) -> str:
    return f"{refusal.value} True True"


@pytest.mark.parametrize("leg", MISMATCHED, ids=[leg.name for leg in MISMATCHED])
def test_cel_evaluating_entry_points_refuse_mismatched_binding_version(
    tmp_path: Path, leg: FakeLeg
) -> None:
    sites = fake_sites(tmp_path, leg)

    result = run_child(LOAD_OUTCOME + LOAD_OUTCOME, isolated_path(sites[0], *sites[1:]))

    assert result.code == 0, result.stderr
    assert result.lines() == [refused_line(leg.refusal)] * 2


@pytest.mark.parametrize(
    "leg", UNCHECKED_CODE, ids=[leg.name for leg in UNCHECKED_CODE]
)
def test_cel_loader_refuses_unchecked_code_before_it_runs(
    tmp_path: Path, leg: FakeLeg
) -> None:
    sites = fake_sites(tmp_path, leg)
    marker = tmp_path / MARKER_NAME
    control = tmp_path / "control.marker"

    result = run_child(LOAD_OUTCOME, isolated_path(*sites), str(marker))
    imported = run_child("import cel\n", isolated_path(*sites), str(control))

    assert result.code == 0, result.stderr
    assert result.lines() == [refused_line(leg.refusal)]
    assert not marker.exists()
    assert imported.code == 0, imported.stderr
    assert control.exists()


def shadow_package(directory: Path) -> Path:
    package = directory / "cel"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(WRITE_MARKER, encoding="utf-8")
    return directory


SHADOW_RESOLVES: str = (
    "import importlib.util, pathlib\n"
    "origin = importlib.util.find_spec('cel').origin\n"
    "print(pathlib.Path(origin).parent.parent == pathlib.Path(sys.path[0]))\n"
)


def test_cel_loader_refuses_a_shadowing_module_before_it_runs(tmp_path: Path) -> None:
    shadow = shadow_package(tmp_path / "shadow")
    path = full_path(shadow)
    refused_marker = tmp_path / "refused.marker"
    control_marker = tmp_path / "control.marker"

    resolves = run_child(SHADOW_RESOLVES, path)
    refused = run_child(SHADOW_RESOLVES + LOAD_OUTCOME, path, str(refused_marker))
    control = run_child("import cel\n", path, str(control_marker))

    assert resolves.lines() == ["True"]
    assert refused.lines() == ["True", refused_line(Refusal.ORIGIN_OUTSIDE_RECORD)]
    assert not refused_marker.exists()
    assert control.code == 0, control.stderr
    assert control_marker.exists()


WithoutBinding = Callable[[Path], list[str]]


def without_distribution(tmp_path: Path) -> list[str]:
    return isolated_path(isolated_site(tmp_path / "site"))


def with_real_binding(tmp_path: Path) -> list[str]:
    return full_path()


@pytest.mark.parametrize(
    ("prefix", "path_for", "refusal"),
    [
        ("", without_distribution, Refusal.MISSING),
        ("import cel\n", with_real_binding, Refusal.ALREADY_IMPORTED),
        (
            "import types\nsys.modules['cel.x'] = types.ModuleType('cel.x')\n",
            with_real_binding,
            Refusal.ALREADY_IMPORTED,
        ),
    ],
    ids=["missing", "already_imported", "submodule_imported"],
)
def test_cel_evaluating_entry_points_refuse_without_binding(
    tmp_path: Path, prefix: str, path_for: WithoutBinding, refusal: Refusal
) -> None:
    path = path_for(tmp_path)

    result = run_child(prefix + LOAD_OUTCOME, path)

    assert result.code == 0, result.stderr
    assert result.lines() == [refused_line(refusal)]


REAL_PANIC: str = (
    PANIC_DECOY
    + "from kora.cel_eval.binding import load_binding\n"
    + "binding = load_binding()\n"
    + "deep = '(' * 100000 + '1' + ')' * 100000\n"
    + "try:\n"
    + "    binding.native_compile(deep)\n"
    + "except binding.panic as error:\n"
    + "    print(type(error) is binding.panic, binding.panic is not decoy)\n"
    + "print(load_binding() is binding)\n"
)


def test_cel_panic_class_is_the_binding_class() -> None:
    path = full_path()

    result = run_child(REAL_PANIC, path)

    assert result.code == 0, result.stderr
    assert result.lines() == ["True True", "True"]
