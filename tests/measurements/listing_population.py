import json
import subprocess
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from kora.cli.plugins import PluginListing

CORE_TARGETS: dict[str, str] = {
    "authentication.static_token": (
        "kora.plugins.authentication.static_token:provider_metadata"
    ),
    "artifact_storage.local": "kora.plugins.storage.local:provider_metadata",
    "artifact_type.agent_session": (
        "kora.plugins.artifact_type.agent_session:provider_metadata"
    ),
    "artifact_type.command_result": (
        "kora.plugins.artifact_type.command_result:provider_metadata"
    ),
    "artifact_type.structured_output": (
        "kora.plugins.artifact_type.structured_output:provider_metadata"
    ),
    "artifact_type.vcs_ref": "kora.plugins.artifact_type.vcs_ref:provider_metadata",
    "policy.allow_deny": "kora.plugins.policy.allow_deny:provider_metadata",
    "secrets.env": "kora.plugins.secrets.env:provider_metadata",
    "vcs.git": "kora.plugins.vcs.git:provider_metadata",
}
EXTERNAL_COUNTS: tuple[int, ...] = (1, 252)
DATASET_COUNTS: tuple[int, ...] = tuple(
    len(CORE_TARGETS) + count for count in EXTERNAL_COUNTS
)
CONTROLLED_ENTRY = "policy.zz_controlled"


class InstalledEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    target: str
    distribution: str


def fixture_entries(external: int) -> dict[str, str]:
    if external not in EXTERNAL_COUNTS:
        raise ValueError("unknown listing population")
    entries = {
        f"policy.item_{index:03}": f"kora_tooling_fixture.inventory:item_{index:03}"
        for index in range(external - 1)
    }
    entries[CONTROLLED_ENTRY] = "kora_tooling_fixture.measurement:controlled"
    return entries


def declared_entries(external: int) -> tuple[InstalledEntry, ...]:
    return tuple(
        InstalledEntry(name=name, target=target, distribution=owner)
        for owner, entries in (
            ("kora", CORE_TARGETS),
            ("kora-tooling-fixture", fixture_entries(external)),
        )
        for name, target in sorted(entries.items())
    )


def installed_entries(python: Path) -> tuple[InstalledEntry, ...]:
    completed = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import importlib.metadata, json, re; "
            "print(json.dumps([{'name': e.name, 'target': e.value, "
            "'distribution': re.sub(r'[-_.]+', '-', d.metadata['Name']).lower()} "
            "for d in importlib.metadata.distributions() for e in d.entry_points "
            "if e.group == 'kora.provider']))",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return TypeAdapter(tuple[InstalledEntry, ...]).validate_json(completed.stdout)


def verify_inventory(external: int, observed: tuple[InstalledEntry, ...]) -> None:
    assert Counter(observed) == Counter(declared_entries(external)), (
        "listing entry-point inventory differs from the declared population",
        observed,
    )


def assert_listing(result: PluginListing, count: int, *, failure: bool) -> None:
    external = count - len(CORE_TARGETS)
    expected = Counter(entry.name for entry in declared_entries(external))
    if failure:
        del expected[CONTROLLED_ENTRY]
    actual = Counter(f"{item.protocol.value}.{item.name}" for item in result.providers)
    assert actual == expected
    assert len(result.providers) == count - failure
    assert [
        (
            item.protocol.value if item.protocol is not None else None,
            item.name,
            item.code,
            item.entry_name_length,
        )
        for item in result.failures
    ] == (
        [("policy", "zz_controlled", "provider_load_failed", len(CONTROLLED_ENTRY))]
        if failure
        else []
    )
    external_entries = fixture_entries(external)
    for provider in result.providers:
        if f"{provider.protocol.value}.{provider.name}" in external_entries:
            assert (
                len(json.dumps(provider.config_schema, separators=(",", ":")).encode())
                == 8192
            )
