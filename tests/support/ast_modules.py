from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceModule:
    path: Path
    name: str


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    reason: str

    def describe(self, source_root: Path) -> str:
        return f"{self.path.relative_to(source_root)}:{self.line}: {self.reason}"


def is_within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def dotted_name(path: Path, source_root: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def source_modules(
    source_root: Path, package: str, suffixes: tuple[str, ...]
) -> list[SourceModule]:
    package_root = source_root / package
    paths = [path for suffix in suffixes for path in package_root.rglob(f"*{suffix}")]
    return [
        SourceModule(path, dotted_name(path, source_root)) for path in sorted(paths)
    ]


def resolve_relative(module: SourceModule, level: int, name: str) -> str:
    parts = module.name.split(".")
    package_parts = parts if module.path.stem == "__init__" else parts[:-1]
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    return ".".join([*base_parts, name] if name else base_parts)


def write_module(source_root: Path, module: str, source: str) -> None:
    path = source_root.joinpath(*module.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
