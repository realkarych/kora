import base64
import hashlib
import json
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kora.cel_eval.binding import DISTRIBUTION, MODULE

ROOT: Path = Path(__file__).resolve().parents[2]
SOURCE: Path = ROOT / "src"
SITE_PACKAGES: Path = Path(sysconfig.get_paths()["purelib"])
BASE_PREFIX: Path = Path(sys.base_prefix).resolve()
DIST_INFO_PREFIX: str = DISTRIBUTION.replace("-", "_")
CHILD_TIMEOUT_SECONDS: float = 60.0
MARKER_NAME: str = "imported.marker"
PRELUDE: str = "import json, sys\nsys.path[:] = json.loads(sys.argv[1])\n"


def standard_library_paths() -> list[str]:
    return [
        entry
        for entry in sys.path
        if entry and Path(entry).resolve().is_relative_to(BASE_PREFIX)
    ]


def full_path(*front: Path) -> list[str]:
    return [
        *(str(entry) for entry in front),
        *standard_library_paths(),
        str(SITE_PACKAGES),
        str(SOURCE),
    ]


def isolated_path(site: Path, *front: Path) -> list[str]:
    return [
        *(str(entry) for entry in front),
        *standard_library_paths(),
        str(site),
        str(SOURCE),
    ]


def is_binding_entry(name: str) -> bool:
    return name == MODULE or name.startswith(f"{DIST_INFO_PREFIX}-")


def isolated_site(directory: Path) -> Path:
    directory.mkdir(parents=True)
    for entry in sorted(SITE_PACKAGES.iterdir()):
        if not is_binding_entry(entry.name):
            (directory / entry.name).symlink_to(entry)
    return directory


def record_digest(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class FakeDistribution:
    version: str
    files: Mapping[str, str]
    unrecorded: Mapping[str, str] = field(default_factory=lambda: {})
    tampered: frozenset[str] = frozenset()
    cached: Mapping[str, str] = field(default_factory=lambda: {})
    linked: Mapping[str, str] = field(default_factory=lambda: {})
    hash_mode: str = "sha256"
    stale_versions: tuple[str, ...] = ()


def record_line(relative: str, content: bytes, tampered: bool, mode: str) -> str:
    recorded = content + b"tampered" if tampered else content
    return f"{relative},{mode}={record_digest(recorded)},{len(content)}"


def write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def write_metadata(site: Path, version: str) -> str:
    dist_info = f"{DIST_INFO_PREFIX}-{version}.dist-info"
    write_text(
        site / dist_info / "METADATA",
        f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: {version}\n",
    )
    return dist_info


def install_fake(site: Path, fake: FakeDistribution) -> Path:
    lines: list[str] = []
    for relative, text in sorted(fake.files.items()):
        content = text.encode("utf-8")
        write_text(site / relative, text)
        tampered = relative in fake.tampered
        lines.append(record_line(relative, content, tampered, fake.hash_mode))
    for relative, text in sorted(fake.unrecorded.items()):
        write_text(site / relative, text)
    for relative, text in sorted(fake.cached.items()):
        write_text(site / relative, text)
        lines.append(f"{relative},,")
    for index, (relative, text) in enumerate(sorted(fake.linked.items())):
        target = site.parent / f"linked-{index}"
        write_text(target / "__init__.py", text)
        (site / relative).symlink_to(target, target_is_directory=True)
    dist_info = write_metadata(site, fake.version)
    for version in fake.stale_versions:
        write_metadata(site, version)
    lines.append(f"{dist_info}/METADATA,,")
    lines.append(f"{dist_info}/RECORD,,")
    (site / dist_info / "RECORD").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return site


@dataclass(frozen=True)
class ChildResult:
    code: int
    stdout: str
    stderr: str

    def lines(self) -> list[str]:
        return self.stdout.splitlines()


def run_child(code: str, path: Sequence[str], *arguments: str) -> ChildResult:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            PRELUDE + code,
            json.dumps(list(path)),
            *arguments,
        ],
        capture_output=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
        text=True,
    )
    return ChildResult(completed.returncode, completed.stdout, completed.stderr)


LOAD_OUTCOME: str = """
from kora.cel_eval.binding import BindingUnavailable, load_binding
try:
    binding = load_binding()
except BindingUnavailable as refused:
    print(refused.refusal.value, refused.__cause__ is None, refused.__context__ is None)
else:
    print("loaded", binding.version)
"""
