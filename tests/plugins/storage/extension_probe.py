import asyncio
import importlib.metadata
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from kora.conformance.capture import captured
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from kora.protocols.loading import LOADING_COMPONENT, discover_providers, load_provider
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.storage import ArtifactStorage


def now() -> datetime:
    return datetime.now(UTC)


def construct(
    metadata: ProviderMetadata, local: ProviderMetadata, configuration: JsonObject
) -> ArtifactStorage:
    created: list[ArtifactStorage] = []

    def constructor(supplied: JsonObject) -> ArtifactStorage:
        assert supplied == configuration
        instance = cast("ArtifactStorage", local.implementation(supplied))
        created.append(instance)
        return instance

    instance = metadata.implementation(configuration, constructor)
    assert len(created) == 1
    assert instance is created[0]
    assert created[0].config_schema == metadata.config_schema
    return created[0]


def main(site: Path, bootstrap: Path, output: Path) -> int:
    sys.path.insert(0, str(site))
    sys.path.insert(0, str(bootstrap))
    with captured(
        LOADING_COMPONENT, LoggingSettings(), "installed-extension", now
    ) as capture:
        discovery = discover_providers(capture.diagnostics)
        assert any(item.name == "fixture_local" for item in discovery.providers)
        metadata = load_provider(
            ProtocolName.ARTIFACT_STORAGE, "fixture_local", capture.diagnostics
        )
        assert metadata.implementation.__module__ == "kora_storage_extension.providers"
        assert "kora_storage_extension.cases" not in sys.modules
        assert "pytest" not in sys.modules
        distribution = importlib.metadata.distribution("kora-storage-extension")
        installed = Path(
            str(distribution.locate_file("kora_storage_extension/providers.py"))
        ).resolve()
        assert installed == site / "kora_storage_extension" / "providers.py"
        local = load_provider(
            ProtocolName.ARTIFACT_STORAGE, "local", capture.diagnostics
        )

        async def configurations() -> None:
            first = construct(
                metadata,
                local,
                {
                    "root": str(output / "first"),
                    "limits": {"version": 1, "stage_slots": 1},
                },
            )
            second = construct(
                metadata,
                local,
                {
                    "root": str(output / "second"),
                    "limits": {"version": 1, "stage_slots": 2},
                },
            )
            try:
                assert first.identity() != second.identity()
                assert (output / "first" / "manifest.json").is_file()
                assert (output / "second" / "manifest.json").is_file()
            finally:
                await first.aclose()
                await second.aclose()

        asyncio.run(configurations())
        try:
            construct(
                metadata,
                local,
                {
                    "root": str(output / "invalid"),
                    "limits": {"version": 1, "stage_slots": 0},
                },
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "installed configuration must reject unsupported limits"
            )

    import pytest

    status = int(
        pytest.main(
            [
                "--pyargs",
                "kora_storage_extension.cases",
                "-q",
                "-c",
                os.devnull,
                "-p",
                "storage_composition",
                "-p",
                "kora.conformance.plugin",
                "--strict-markers",
                "--import-mode=importlib",
                "-o",
                "markers=kora_conformance: provider conformance",
                f"--junitxml={output / 'conformance.xml'}",
            ]
        )
    )
    from kora.conformance import artifact_storage

    cases = sys.modules["kora_storage_extension.cases"]
    exported = tuple(
        name for name in artifact_storage.__all__ if name.startswith("test_")
    )
    assert all(
        getattr(cases, name, None) is getattr(artifact_storage, name)
        for name in exported
    )
    assert not any(
        name.startswith(
            ("kora.db", "kora.engine", "kora.scheduler", "kora.worker", "tests.")
        )
        for name in sys.modules
    )
    (output / "extension-report.json").write_text(
        json.dumps({"provider": str(installed), "cases": exported, "status": status})
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
