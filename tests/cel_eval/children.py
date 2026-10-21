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


def record_line(relative: str, content: bytes, tampered: bool) -> str:
    recorded = content + b"tampered" if tampered else content
    return f"{relative},sha256={record_digest(recorded)},{len(content)}"


def install_fake(site: Path, fake: FakeDistribution) -> Path:
    dist_info = f"{DIST_INFO_PREFIX}-{fake.version}.dist-info"
    lines: list[str] = []
    for relative, text in sorted(fake.files.items()):
        content = text.encode("utf-8")
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        lines.append(record_line(relative, content, relative in fake.tampered))
    for relative, text in sorted(fake.unrecorded.items()):
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    metadata = site / dist_info / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: {fake.version}\n",
        encoding="utf-8",
    )
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
    print(refused.refusal.value)
else:
    print("loaded", binding.version)
"""
