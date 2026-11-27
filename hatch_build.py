import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict, cast
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SQLITE_ARCHIVE = "sqlite-amalgamation-3540100"
SQLITE_SOURCE_DIGEST = (
    "e2b869d3003b33c942b7dd7063604fbc53553e1b66609570df39b9b7b558ee81"
)
SQLITE_ARCHIVE_DIGEST = (
    "92281c86e2957f9b9305890f9ac04204caa7b677cc868dc326d942689df28e30"
)
NATIVE_NAME = "_native.abi3.so"
NATIVE_RECEIPT = "_native-build.json"
NATIVE_FLAGS = (
    "-std=c11",
    "-O2",
    "-fPIC",
    "-fvisibility=hidden",
    "-DSQLITE_OMIT_LOAD_EXTENSION",
)


class WheelData(TypedDict):
    force_include: dict[str, str]
    pure_python: bool
    tag: str


def native_toolchain() -> tuple[Path, list[str], str]:
    if sys.implementation.name != "cpython" or sysconfig.get_config_var(
        "Py_GIL_DISABLED"
    ):
        raise ValueError("The SQLite wheel requires non-free-threaded CPython.")
    architecture = platform.machine()
    if sys.platform == "darwin":
        xcrun = ["/usr/bin/xcrun", "--sdk", "macosx"]
        compiler = Path(
            subprocess.check_output([*xcrun, "--find", "clang"], text=True).strip()
        )
        sdk = subprocess.check_output([*xcrun, "--show-sdk-path"], text=True).strip()
        minimum = str(sysconfig.get_config_var("MACOSX_DEPLOYMENT_TARGET"))
        if not minimum or not all(part.isdecimal() for part in minimum.split(".")):
            raise ValueError("The macOS deployment target is unavailable.")
        major, minor, *_ = (int(part) for part in minimum.split(".") + ["0"])
        options = [
            "-arch",
            architecture,
            "-isysroot",
            sdk,
            f"-mmacosx-version-min={minimum}",
        ]
        options.extend(
            ["-D_DARWIN_C_SOURCE", "-bundle", "-undefined", "dynamic_lookup"]
        )
        return compiler, options, f"macosx_{major}_{minor}_{architecture}"
    if sys.platform == "linux":
        compiler = Path("/usr/bin/cc").resolve(strict=True)
        target = subprocess.check_output([compiler, "-dumpmachine"], text=True).strip()
        if not target.startswith(f"{architecture}-"):
            raise ValueError("The Linux compiler architecture does not match CPython.")
        options = ["-D_GNU_SOURCE", "--sysroot=/", "-shared", "-pthread"]
        return compiler, options, f"linux_{architecture}"
    raise ValueError("The private SQLite wheel requires Linux or macOS.")


def build_sqlite_native(source: Path, output: Path, *, editable: bool) -> str:
    vendor = source / "_vendor"
    archive = vendor / f"{SQLITE_ARCHIVE}.zip"
    patch = vendor / "guard.patch"
    compiler, target_flags, target = native_toolchain()
    with compiler.open("rb") as executable:
        compiler_hash = hashlib.file_digest(executable, "sha256").hexdigest()
    inputs = [archive, patch, *sorted(source.glob("_*.h")), source / "_native.c"]
    identity = {
        item.name: hashlib.sha3_256(item.read_bytes()).hexdigest() for item in inputs
    }
    if identity[archive.name] != SQLITE_ARCHIVE_DIGEST:
        raise ValueError("The SQLite source archive digest does not match.")
    receipt = json.dumps(
        dict(
            inputs=identity,
            compiler=str(compiler),
            compiler_sha256=compiler_hash,
            flags=[*NATIVE_FLAGS, *target_flags],
            target=target,
        ),
        sort_keys=True,
    )
    binary = output / NATIVE_NAME
    recorded = output / NATIVE_RECEIPT
    with (output / "_native-build.lock").open("ab") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if editable and (recorded.exists() or binary.exists()):
            if not binary.is_file() or recorded.read_text() != receipt:
                raise ValueError(
                    "The editable SQLite build differs from its checkout generation."
                )
            return target
        with (
            TemporaryDirectory(dir=output, prefix="_native-tmp-") as location,
            ZipFile(archive) as packaged,
        ):
            isolated = Path(location)
            pristine = packaged.read(f"{SQLITE_ARCHIVE}/sqlite3.c")
            if hashlib.sha3_256(pristine).hexdigest() != SQLITE_SOURCE_DIGEST:
                raise ValueError("The pristine SQLite source digest does not match.")
            (isolated / "sqlite3.c").write_bytes(pristine)
            (isolated / "sqlite3.h").write_bytes(
                packaged.read(f"{SQLITE_ARCHIVE}/sqlite3.h")
            )
            subprocess.run(
                ["/usr/bin/patch", "-s", "-f", "-F", "0", "-p1", "-i", patch],
                cwd=isolated,
                check=True,
            )
            compiled = isolated / NATIVE_NAME
            command = [compiler, *NATIVE_FLAGS, *target_flags]
            includes = [
                f"-I{folder}"
                for folder in (sysconfig.get_path("include"), source, isolated)
            ]
            subprocess.run(
                [*command, *includes, source / "_native.c", "-o", compiled], check=True
            )
            (isolated / NATIVE_RECEIPT).write_text(receipt)
            compiled.replace(binary)
            (isolated / NATIVE_RECEIPT).replace(recorded)
    return target


class FrontendReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name not in {"src", "href"} or value is None:
                continue
            reference = urlsplit(value)
            if reference.scheme or reference.netloc or not reference.path:
                continue
            self.paths.add(unquote(reference.path).lstrip("/"))


class FrontendBuildHook(BuildHookInterface[BuilderConfig]):
    _native_build: TemporaryDirectory[str] | None = None

    def initialize(self, version: str, build_data: object) -> None:
        root = Path(self.root)
        native = root / "src" / "kora" / "sqlite"
        if version == "editable":
            build_sqlite_native(native, native, editable=True)
            return

        self._native_build = TemporaryDirectory()
        native_output = Path(self._native_build.name)
        wheel = cast(WheelData, build_data)
        wheel["tag"] = (
            f"cp311-abi3-{build_sqlite_native(native, native_output, editable=False)}"
        )
        wheel["pure_python"] = False
        for name in (NATIVE_NAME, NATIVE_RECEIPT):
            wheel["force_include"][str(native_output / name)] = f"kora/sqlite/{name}"

        offline = os.environ.get("KORA_BUILD_OFFLINE")
        if offline not in {None, "0", "1"}:
            raise ValueError("KORA_BUILD_OFFLINE must be unset, 0 or 1")

        output = root / "src" / "kora" / "api" / "static"
        if output.exists():
            shutil.rmtree(output)

        pnpm = shutil.which("pnpm")
        if pnpm is None:
            raise FileNotFoundError("pnpm is required to build the frontend")
        install = [pnpm, "--dir", "web", "install", "--frozen-lockfile"]
        if offline == "1":
            install.append("--offline")
        subprocess.run(install, cwd=root, check=True)
        subprocess.run([pnpm, "--dir", "web", "run", "build"], cwd=root, check=True)

        entry_point = output / "index.html"
        references = FrontendReferences()
        references.feed(entry_point.read_text(encoding="utf-8"))
        references.close()
        for reference in references.paths:
            asset = (output / reference).resolve()
            if not asset.is_relative_to(output.resolve()):
                raise ValueError(
                    f"Frontend asset escapes the output directory: {reference}"
                )
            if not asset.is_file():
                raise FileNotFoundError(f"Frontend asset is missing: {reference}")

    def finalize(self, version: str, build_data: object, artifact_path: str) -> None:
        if self._native_build is not None:
            self._native_build.cleanup()
            self._native_build = None
