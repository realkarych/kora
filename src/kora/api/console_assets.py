import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

ASSET_NAME = re.compile(r"assets/[A-Za-z0-9_.-]+\.(?:js|css|woff2)")
CSS_URL = re.compile(r"""url\(\s*("[^"]*"|'[^']*'|[^)\s]+)|(\\|@import)""", re.I)


class DocumentReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"src", "href"} and value is not None:
                self.paths.add(value)


class ConsoleAssetError(ValueError):
    pass


class Chunk(BaseModel):
    file: str
    css: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    dynamic_imports: tuple[str, ...] = Field(default=(), alias="dynamicImports")


MANIFEST = TypeAdapter(dict[str, Chunk])


@dataclass(frozen=True, slots=True)
class ConsoleAssets:
    html: bytes
    files: dict[str, tuple[str, bytes]]


def load_console_assets(root: Path) -> ConsoleAssets:
    try:
        manifest = root / "console-manifest.json"
        entry = MANIFEST.validate_json(manifest.read_bytes())["index.html"]
        html = (root / "index.html").read_bytes()
        references = DocumentReferences()
        references.feed(html.decode())
        references.close()
        paths = {entry.file, *entry.css, *entry.assets}
        font_paths = {"/" + path for path in entry.assets}
        if (
            entry.imports
            or entry.dynamic_imports
            or not entry.file.endswith(".js")
            or not entry.css
            or any(not path.endswith(".css") for path in entry.css)
            or len(font_paths) != 3
            or any(not path.endswith(".woff2") for path in font_paths)
            or not all(ASSET_NAME.fullmatch(path) for path in paths)
            or references.paths - {"data:,"}
            != {"/" + path for path in (entry.file, *entry.css)}
        ):
            raise ConsoleAssetError()
        content = {path: (root / path).read_bytes() for path in paths}
        if not all(content.values()) or font_paths != {
            (path or forbidden).strip("\"'")
            for css in entry.css
            for path, forbidden in CSS_URL.findall(content[css].decode())
        }:
            raise ConsoleAssetError()
        types = {"js": "text/javascript", "css": "text/css", "woff2": "font/woff2"}
        files = {
            path: (types[path.rsplit(".", 1)[1]], body)
            for path, body in content.items()
        }
        return ConsoleAssets(html, files)
    except (KeyError, OSError, UnicodeError, ValidationError):
        raise ConsoleAssetError() from None
