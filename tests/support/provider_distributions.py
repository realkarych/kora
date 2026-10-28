from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Distribution:
    name: str
    version: str
    entries: dict[str, str]


def write_distribution(site: Path, distribution: Distribution) -> None:
    folder = site / (
        f"{distribution.name.replace('-', '_')}-{distribution.version}.dist-info"
    )
    folder.mkdir()
    (folder / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        f"Name: {distribution.name}\n"
        f"Version: {distribution.version}\n"
    )
    lines = [
        f"{name} = {value}" if value else name
        for name, value in distribution.entries.items()
    ]
    (folder / "entry_points.txt").write_text(
        "[kora.provider]\n" + "\n".join(lines) + "\n"
    )
