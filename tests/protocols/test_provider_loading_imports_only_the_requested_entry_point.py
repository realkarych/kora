import json
from dataclasses import dataclass
from pathlib import Path

from tests.support.observability import ProcessResult, run_python

PROBE: Path = Path(__file__).with_name("loading_probe.py")
DISCOVERY_CYCLES: int = 20
PREFIX: str = "kora_loading_fixture_"
SENTINEL: str = "sentinel-5c1e0b77"
DEBUG_LOADING: dict[str, str] = {"KORA_LOG_COMPONENT_LEVELS": "protocols.loading=debug"}
PROVIDER_MODULE: str = "\n".join(
    [
        "from kora.protocols.metadata import ProtocolName, ProviderMetadata",
        "",
        "",
        "def implementation(configuration: object) -> object:",
        "    return configuration",
        "",
        "",
        "def provider() -> ProviderMetadata:",
        "    return ProviderMetadata(",
        '        ProtocolName("{protocol}"),',
        '        "{name}",',
        "        implementation,",
        '        {{"type": "object"}},',
        '        "fixture-1",',
        "    )",
        "",
    ]
)
BROKEN_MODULE: str = f'raise ImportError("{SENTINEL}")\n'
RAISING_MODULE: str = (
    f'def provider() -> object:\n    raise RuntimeError("{SENTINEL}")\n'
)


@dataclass(frozen=True)
class Distribution:
    name: str
    version: str
    entries: dict[str, str]


MAIN: Distribution = Distribution(
    "kora-loading-fixture-main",
    "1.0",
    {
        "runtime.alpha": f"{PREFIX}alpha:provider",
        "runtime.beta": f"{PREFIX}beta:provider",
        "artifact_storage.alpha": f"{PREFIX}storage:provider",
        "harness.fake": f"{PREFIX}fake:provider",
        "runtime.fake": f"{PREFIX}fake:provider",
        "runtime.probe": "kora.testing.probe:provider",
        "harness.probe": "kora.plugins.harness.fake.x:provider",
        "runtime.broken": f"{PREFIX}broken:provider",
        "runtime.raising": f"{PREFIX}raising:provider",
        "runtime.malformed": "not a valid value!!",
        "runtime.noeq": "",
    },
)
GAMMA_ONE: Distribution = Distribution(
    "kora-loading-fixture-one", "1.0", {"runtime.gamma": f"{PREFIX}gamma_one:provider"}
)
GAMMA_TWO: Distribution = Distribution(
    "kora-loading-fixture-two", "1.0", {"runtime.gamma": f"{PREFIX}gamma_two:provider"}
)
DELTA_OLD: Distribution = Distribution(
    "kora-loading-fixture-copy", "1.0", {"runtime.delta": f"{PREFIX}delta_one:provider"}
)
DELTA_NEW: Distribution = Distribution(
    "kora-loading-fixture-copy", "2.0", {"runtime.delta": f"{PREFIX}delta_one:provider"}
)
SAME_COPY: Distribution = Distribution(
    "kora-loading-fixture-same", "1.0", {"runtime.epsilon": f"{PREFIX}epsilon:provider"}
)
PROVIDERS: dict[str, tuple[str, str]] = {
    "alpha": ("runtime", "alpha"),
    "beta": ("runtime", "beta"),
    "storage": ("artifact_storage", "alpha"),
    "fake": ("runtime", "fake"),
    "gamma_one": ("runtime", "gamma"),
    "gamma_two": ("runtime", "gamma"),
    "delta_one": ("runtime", "delta"),
    "epsilon": ("runtime", "epsilon"),
}


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


def write_modules(site: Path) -> None:
    for suffix, (protocol, name) in PROVIDERS.items():
        text = PROVIDER_MODULE.format(protocol=protocol, name=name)
        (site / f"{PREFIX}{suffix}.py").write_text(text)
    (site / f"{PREFIX}broken.py").write_text(BROKEN_MODULE)
    (site / f"{PREFIX}raising.py").write_text(RAISING_MODULE)


def build_sites(root: Path) -> tuple[Path, Path]:
    first = root / "site-first"
    second = root / "site-second"
    first.mkdir()
    second.mkdir()
    for distribution in (MAIN, GAMMA_ONE, GAMMA_TWO, DELTA_OLD, SAME_COPY):
        write_distribution(first, distribution)
    for distribution in (DELTA_NEW, SAME_COPY):
        write_distribution(second, distribution)
    write_modules(first)
    return first, second


def run_probe(mode: str, root: Path, log: Path) -> ProcessResult:
    first, second = build_sites(root)
    arguments = ["-I", str(PROBE), mode, str(first), str(second), str(log)]
    return run_python(arguments, DEBUG_LOADING)


def read_records(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_provider_loading_imports_only_the_requested_entry_point(
    tmp_path: Path,
) -> None:
    log = tmp_path / "diagnostics.jsonl"

    completed = run_probe("legs", tmp_path, log)

    assert completed.code == 0, completed.stderr
    assert completed.stderr == b""
    result = json.loads(completed.stdout)
    alpha = f"{PREFIX}alpha"
    storage = f"{PREFIX}storage"
    assert result["after_alpha"] == [alpha]
    assert result["protocols"] == ["runtime", "artifact_storage"]
    assert result["after_storage"] == [alpha, storage]
    assert result["codes"] == {
        "runtime.gamma": "provider_ambiguous",
        "runtime.delta": "provider_ambiguous",
        "harness.fake": "provider_reserved",
        "runtime.fake": "provider_reserved",
        "runtime.probe": "provider_reserved",
        "harness.probe": "provider_reserved",
        "runtime.malformed": "provider_metadata_invalid",
        "runtime.epsilon": "loaded",
    }
    assert result["after_refusals"] == sorted([alpha, storage, f"{PREFIX}epsilon"])
    first_load, last_load = result["load_cycles"]
    assert first_load == last_load
    first_failure, last_failure = result["failure_cycles"]
    assert first_failure == last_failure
    assert result["failure_codes"] == ["provider_load_failed"]
    first_discovery, last_discovery = result["discovery_cycles"]
    assert first_discovery == last_discovery
    assert result["discovered"] == [
        "artifact_storage.alpha",
        "runtime.alpha",
        "runtime.beta",
        "runtime.epsilon",
    ]
    assert result["discovery_failures"] == [
        "provider_ambiguous",
        "provider_ambiguous",
        "provider_load_failed",
        "provider_load_failed",
        "provider_metadata_invalid",
        "provider_metadata_invalid",
        "provider_reserved",
        "provider_reserved",
        "provider_reserved",
        "provider_reserved",
    ]
    assert result["drained"] is True
    records = read_records(log)
    warnings = [record for record in records if record["level"] == "WARNING"]
    assert len(warnings) == 10 * (DISCOVERY_CYCLES + 1)
    assert {record["event"] for record in warnings} == {
        "protocols.loading.provider_unavailable"
    }
    assert {record["component"] for record in records} == {"protocols.loading"}
    text = log.read_text()
    assert PREFIX not in text
    assert SENTINEL not in text
    assert "kora.testing.probe" not in text


def test_provider_loading_component_override_needs_the_loading_import(
    tmp_path: Path,
) -> None:
    log = tmp_path / "unused.jsonl"

    completed = run_probe("settings_first", tmp_path, log)

    assert completed.code == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "refused_before_import": True,
        "accepted_after_import": True,
    }
