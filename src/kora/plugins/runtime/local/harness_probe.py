import os
from dataclasses import dataclass
from pathlib import Path

from kora.observability import ProcessDiagnostics
from kora.plugins.runtime.local.installation import (
    FORMAT,
    INSTALLATION_NAME,
    InstallationConfiguration,
    RunnerRefused,
    read_installation,
)
from kora.plugins.runtime.local.production import (
    ClaudeResolver,
    production_harnesses,
    production_sources,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness_versions import tested_version_refusal
from kora.runner.filesystem import DirectoryAnchor

ABSENT: str = "absent"


@dataclass(frozen=True)
class InstallationProbe:
    configuration: InstallationConfiguration
    detail: str


@dataclass(frozen=True)
class HarnessPassed:
    name: str
    version: str


@dataclass(frozen=True)
class HarnessFailed:
    name: str
    reason: str


def installation_detail(anchor: DirectoryAnchor) -> str:
    try:
        os.stat(INSTALLATION_NAME, dir_fd=anchor.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return ABSENT
    return FORMAT


def probe_installation(
    anchor: DirectoryAnchor, path: Path, diagnostics: ProcessDiagnostics
) -> InstallationProbe | str:
    try:
        installation = read_installation(anchor, diagnostics, path)
        production_sources(diagnostics, installation)
    except RunnerRefused as refused:
        return str(refused)
    except KoraProtocolError as refused:
        return refused.code
    return InstallationProbe(installation, installation_detail(anchor))


def probe_harness(name: str, resolver: ClaudeResolver) -> HarnessPassed | HarnessFailed:
    try:
        binary = resolver.expected_binary()
    except KoraProtocolError as refused:
        return HarnessFailed(name, refused.code)
    try:
        matches = binary.matches()
    except OSError:
        matches = False
    if not matches:
        return HarnessFailed(name, "integrity_error")
    try:
        version = resolver.installed_version()
    except KoraProtocolError as refused:
        return HarnessFailed(name, refused.code)
    refusal = tested_version_refusal(version, resolver.tested_versions)
    if refusal is not None:
        return HarnessFailed(name, refusal.value)
    return HarnessPassed(name, version)


def probe_harnesses(
    diagnostics: ProcessDiagnostics, installation: InstallationConfiguration
) -> tuple[HarnessPassed | HarnessFailed, ...]:
    hosted = production_harnesses(diagnostics, installation)
    return tuple(
        probe_harness(name, hosted[name])
        if name in hosted
        else HarnessFailed(name, "not_hosted")
        for name in sorted(installation.harness)
    )
