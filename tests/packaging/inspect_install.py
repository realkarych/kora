import sys
from html.parser import HTMLParser
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import kora


class InstalledReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()
        self.scripts: set[str] = set()
        self.stylesheets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        for attribute in ("src", "href"):
            value = attributes.get(attribute)
            if value is None:
                continue
            reference = urlsplit(value)
            if reference.scheme or reference.netloc or not reference.path:
                continue
            path = unquote(reference.path).lstrip("/")
            self.paths.add(path)
            if tag == "script" and attribute == "src":
                self.scripts.add(path)
            if tag == "link" and attributes.get("rel") == "stylesheet":
                self.stylesheets.add(path)


def inspect_install() -> None:
    name, version, environment, major, minor, license_file = sys.argv[1:]
    environment_path = Path(environment).resolve()
    assert sys.version_info[:2] == (int(major), int(minor))
    assert Path(sys.prefix).resolve() == environment_path
    assert kora.__file__ is not None
    assert Path(kora.__file__).resolve().is_relative_to(environment_path)
    installed = distribution(name)
    assert installed.metadata["Name"] == name
    assert installed.version == version
    assert installed.metadata["License-Expression"] == "Apache-2.0"
    assert installed.metadata.get_all("License-File") == ["LICENSE"]
    assert installed.requires is None
    installed_files = installed.files
    assert installed_files is not None
    licenses = [
        path
        for path in installed_files
        if PurePosixPath(str(path)).parts[-2:] == ("licenses", "LICENSE")
    ]
    assert len(licenses) == 1
    assert licenses[0].read_text() == Path(license_file).read_text(encoding="utf-8")
    package = files("kora")
    assert package.joinpath("py.typed").is_file()
    static = package.joinpath("api", "static")
    notices = static.joinpath("THIRD_PARTY_LICENSES.md")
    assert notices.is_file(), "Bundled frontend dependency notices are missing"
    notice_text = notices.read_text(encoding="utf-8")
    dependency_names: set[str] = set()
    for section in notice_text.split("\n## ")[1:]:
        heading, separator, license_text = section.partition("\n")
        assert separator
        dependency_name, version_separator, version_and_license = heading.partition(
            " - "
        )
        assert version_separator
        assert version_and_license.endswith(" (MIT)"), heading
        assert dependency_name not in dependency_names, dependency_name
        dependency_names.add(dependency_name)
        normalized_license = " ".join(license_text.split())
        for required_text in (
            "MIT License",
            "Copyright (c)",
            "Permission is hereby granted, free of charge,",
            "The above copyright notice and this permission notice shall be included",
            'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
        ):
            assert required_text in normalized_license, (dependency_name, required_text)
    assert {"react", "react-dom", "scheduler"} <= dependency_names, dependency_names
    index = static.joinpath("index.html").read_text(encoding="utf-8")
    assert index.strip()
    references = InstalledReferences()
    references.feed(index)
    references.close()
    assert references.scripts
    assert references.stylesheets
    assert all(
        PurePosixPath(path).is_relative_to("assets")
        and PurePosixPath(path).suffix == ".js"
        for path in references.scripts
    )
    assert all(
        PurePosixPath(path).is_relative_to("assets")
        and PurePosixPath(path).suffix == ".css"
        for path in references.stylesheets
    )
    static_path = Path(str(static)).resolve()
    for reference in references.paths:
        asset = (static_path / reference).resolve()
        assert asset.is_relative_to(static_path), reference
        assert asset.is_file(), reference
        assert asset.stat().st_size > 0, reference
    print(
        f"Verified {name} {version} on Python {major}.{minor}: "
        f"isolated import, license, typing marker and {len(references.paths)} assets; "
        f"{len(notice_text.encode('utf-8'))} notice bytes for "
        f"{', '.join(sorted(dependency_names))}"
    )


if __name__ == "__main__":
    inspect_install()
