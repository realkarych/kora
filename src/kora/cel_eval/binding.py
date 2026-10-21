import base64
import enum
import hashlib
import importlib.metadata
import importlib.util
import logging
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol

from kora.cel import CheckedExpression, JsonValue
from kora.cel_eval import BINDING_LOADED, CEL_COMPONENT, CEL_LOGGER_NAME
from kora.observability import current_diagnostics

DISTRIBUTION: str = "common-expression-language"
MODULE: str = "cel"
PINNED_VERSION: str = "0.10.0"
PACKAGE_ENTRY: str = f"{MODULE}/__init__.py"
CACHE_DIRECTORY: str = "__pycache__"
PANIC_MODULE: str = "pyo3_runtime"
PANIC_NAME: str = "PanicException"
RECORD_HASH: str = "sha256"


class Refusal(enum.StrEnum):
    ALREADY_IMPORTED = "already_imported"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    VERSION_MISMATCH = "version_mismatch"
    ORIGIN_OUTSIDE_RECORD = "origin_outside_record"
    FILE_MISMATCH = "file_mismatch"
    IMPORT_FAILED = "import_failed"
    MODULE_OUTSIDE_RECORD = "module_outside_record"
    PANIC_CLASS_UNRESOLVED = "panic_class_unresolved"


class BindingUnavailable(Exception):
    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.value)
        self.refusal: Refusal = refusal


class NativeProgram(Protocol):
    def execute(self, context: dict[str, object]) -> object: ...


class NativeCompile(Protocol):
    def __call__(self, expression: str) -> NativeProgram: ...


@dataclass(frozen=True)
class Program:
    native: NativeProgram
    rendering: str


@dataclass(frozen=True)
class Binding:
    version: str
    panic: type[BaseException]
    native_compile: NativeCompile

    def compile(self, checked: CheckedExpression) -> Program:
        return Program(self.native_compile(checked.rendering), checked.rendering)

    def execute(self, program: Program, variables: Mapping[str, JsonValue]) -> object:
        return program.native.execute(dict(variables))


class DropEveryRecord(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


@dataclass(frozen=True)
class Located:
    distribution: importlib.metadata.Distribution
    package: Path
    recorded: Mapping[Path, str]


class LoadedBinding:
    def __init__(self) -> None:
        self.lock: threading.Lock = threading.Lock()
        self.binding: Binding | None = None
        self.refusal: Refusal | None = None


LOADED: LoadedBinding = LoadedBinding()


def is_binding_module(name: str) -> bool:
    return name == MODULE or name.startswith(f"{MODULE}.")


def binding_modules() -> list[str]:
    return sorted(name for name in list(sys.modules) if is_binding_module(name))


def search_path() -> list[str]:
    directories: dict[Path, str] = {}
    for entry in list(sys.path):
        directory = Path(entry or os.curdir).resolve()
        if directory.is_dir():
            directories.setdefault(directory, str(directory))
    return list(directories.values())


def matching_distributions() -> list[importlib.metadata.Distribution]:
    return list(importlib.metadata.distributions(name=DISTRIBUTION, path=search_path()))


def record_digest(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def is_cached(parts: tuple[str, ...]) -> bool:
    return CACHE_DIRECTORY in parts


def recorded_files(
    distribution: importlib.metadata.Distribution,
) -> dict[Path, str] | None:
    files = distribution.files
    if files is None:
        return None
    recorded: dict[Path, str] = {}
    for entry in files:
        if not entry.parts or entry.parts[0] != MODULE or is_cached(entry.parts):
            continue
        if entry.hash is None or entry.hash.mode != RECORD_HASH:
            return None
        recorded[Path(str(distribution.locate_file(entry))).resolve()] = (
            entry.hash.value
        )
    return recorded


def present_files(package: Path) -> list[Path] | None:
    if package.is_symlink():
        return None
    present: list[Path] = []
    for directory, subdirectories, names in os.walk(package):
        base = Path(directory)
        if any((base / name).is_symlink() for name in [*subdirectories, *names]):
            return None
        subdirectories.sort()
        if not is_cached(base.relative_to(package).parts):
            present.extend(base / name for name in sorted(names))
    return present


def files_match(package: Path, recorded: Mapping[Path, str]) -> bool:
    present = present_files(package)
    if present is None:
        return False
    resolved = {path.resolve(): path for path in present}
    if set(resolved) != set(recorded):
        return False
    return all(
        record_digest(path.read_bytes()) == recorded[key]
        for key, path in resolved.items()
    )


def locate() -> Located:
    distributions = matching_distributions()
    if not distributions:
        raise BindingUnavailable(Refusal.MISSING)
    if len(distributions) > 1:
        raise BindingUnavailable(Refusal.AMBIGUOUS)
    distribution = distributions[0]
    if distribution.version != PINNED_VERSION:
        raise BindingUnavailable(Refusal.VERSION_MISMATCH)
    entry = Path(str(distribution.locate_file(PACKAGE_ENTRY)))
    spec = importlib.util.find_spec(MODULE)
    if spec is None or spec.origin is None:
        raise BindingUnavailable(Refusal.ORIGIN_OUTSIDE_RECORD)
    if Path(spec.origin).resolve() != entry.resolve():
        raise BindingUnavailable(Refusal.ORIGIN_OUTSIDE_RECORD)
    recorded = recorded_files(distribution)
    if recorded is None or not files_match(entry.parent, recorded):
        raise BindingUnavailable(Refusal.FILE_MISMATCH)
    return Located(distribution, entry, recorded)


def silence_binding_logger() -> None:
    logger = logging.getLogger(MODULE)
    logger.disabled = True
    if not any(isinstance(entry, DropEveryRecord) for entry in logger.filters):
        logger.addFilter(DropEveryRecord())


def panic_classes() -> set[type[BaseException]]:
    found: set[type[BaseException]] = set()
    pending: list[type[BaseException]] = [BaseException]
    seen: set[type[BaseException]] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current.__module__ == PANIC_MODULE and current.__name__ == PANIC_NAME:
            found.add(current)
        pending.extend(current.__subclasses__())
    return found


def import_binding() -> ModuleType | None:
    try:
        import cel
    except (ImportError, OSError):
        return None
    return cel


def native_compile() -> NativeCompile:
    import cel

    return cel.compile


def module_file(module: ModuleType) -> Path | None:
    loaded = vars(module).get("__file__")
    return Path(loaded).resolve() if isinstance(loaded, str) else None


def loaded_files_match(located: Located, package: ModuleType) -> bool:
    for name in binding_modules():
        loaded = module_file(sys.modules[name])
        if loaded is None or loaded not in located.recorded:
            return False
    return module_file(package) == located.package.resolve()


def announce(version: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is None:
        return
    diagnostics.logger(CEL_COMPONENT, name=CEL_LOGGER_NAME).emit(
        BINDING_LOADED, version=version
    )


def load() -> Binding:
    if binding_modules():
        raise BindingUnavailable(Refusal.ALREADY_IMPORTED)
    located = locate()
    silence_binding_logger()
    before = panic_classes()
    package = import_binding()
    if package is None:
        raise BindingUnavailable(Refusal.IMPORT_FAILED)
    if not loaded_files_match(located, package):
        raise BindingUnavailable(Refusal.MODULE_OUTSIDE_RECORD)
    introduced = panic_classes() - before
    if len(introduced) != 1:
        raise BindingUnavailable(Refusal.PANIC_CLASS_UNRESOLVED)
    binding = Binding(located.distribution.version, introduced.pop(), native_compile())
    announce(binding.version)
    return binding


def load_binding() -> Binding:
    with LOADED.lock:
        if LOADED.refusal is not None:
            raise BindingUnavailable(LOADED.refusal)
        if LOADED.binding is None:
            try:
                LOADED.binding = load()
            except BindingUnavailable as refused:
                LOADED.refusal = refused.refusal
                raise
        return LOADED.binding
