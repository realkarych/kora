import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


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
    def initialize(self, version: str, build_data: object) -> None:
        if version == "editable":
            return

        offline = os.environ.get("KORA_BUILD_OFFLINE")
        if offline not in {None, "0", "1"}:
            raise ValueError("KORA_BUILD_OFFLINE must be unset, 0 or 1")

        root = Path(self.root)
        output = root / "src" / "kora" / "api" / "static"
        if output.exists():
            shutil.rmtree(output)

        install = ["pnpm", "--dir", "web", "install", "--frozen-lockfile"]
        if offline == "1":
            install.append("--offline")
        subprocess.run(install, cwd=root, check=True)
        subprocess.run(["pnpm", "--dir", "web", "run", "build"], cwd=root, check=True)

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
