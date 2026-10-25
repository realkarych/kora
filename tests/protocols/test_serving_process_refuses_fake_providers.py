import json
from pathlib import Path
from typing import Final

from tests.support.observability import run_python

PROBE: Final[Path] = Path(__file__).with_name("loading_probe.py")
PREFIX: Final[str] = "kora_loading_fixture_"
IMPLEMENTED_MODULE: Final[str] = "\n".join(
    [
        "from kora.plugins.harness.fake import FakeHarness",
        "from kora.protocols.metadata import ProtocolName, ProviderMetadata",
        "",
        "",
        "def provider() -> ProviderMetadata:",
        "    return ProviderMetadata(",
        "        ProtocolName.HARNESS,",
        '        "implemented",',
        "        FakeHarness,",
        '        {"type": "object"},',
        '        "fixture-1",',
        "    )",
        "",
    ]
)
ENTRIES: Final[dict[str, str]] = {
    "harness.fake": f"{PREFIX}implemented:provider",
    "harness.genuine": "kora.plugins.harness.fake:FakeHarness",
    "harness.implemented": f"{PREFIX}implemented:provider",
    "runtime.fake": f"{PREFIX}implemented:provider",
}


def build_site(root: Path) -> Path:
    site = root / "site"
    folder = site / "kora_loading_fixture_serving-1.0.dist-info"
    folder.mkdir(parents=True)
    (folder / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: kora-loading-fixture-serving\nVersion: 1.0\n"
    )
    lines = [f"{name} = {value}" for name, value in ENTRIES.items()]
    (folder / "entry_points.txt").write_text(
        "[kora.provider]\n" + "\n".join(lines) + "\n"
    )
    (site / f"{PREFIX}implemented.py").write_text(IMPLEMENTED_MODULE)
    return site


def test_serving_process_refuses_fake_providers(tmp_path: Path) -> None:
    site = build_site(tmp_path)
    log = tmp_path / "diagnostics.jsonl"
    arguments = ["-I", str(PROBE), "serving", str(site), str(site), str(log)]

    completed = run_python(arguments, {})

    assert completed.code == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["before"] == []
    assert result["imported"] == {
        "harness.fake": "provider_reserved",
        "harness.genuine": "provider_reserved",
        "runtime.fake": "provider_reserved",
    }
    assert result["after_imported"] == []
    assert result["implemented"] == "provider_reserved"
    assert result["drained"] is True
