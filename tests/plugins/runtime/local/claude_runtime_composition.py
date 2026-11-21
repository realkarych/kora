import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from kora.observability import ProcessDiagnostics
from kora.plugins.runtime.local.agent import AgentSources
from kora.plugins.runtime.local.installation import InstallationConfiguration
from kora.plugins.runtime.local.launch import ExpectedBinary
from kora.plugins.runtime.local.production import production_sources
from kora.plugins.runtime.local.supervisor import run


def sources(
    diagnostics: ProcessDiagnostics, installation: InstallationConfiguration
) -> AgentSources:
    target = Path(sys.argv[1])
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    sources = replace(production_sources(diagnostics, installation), binary=binary)
    target.with_suffix(".runner-environment.json").write_text(
        json.dumps(dict(os.environ))
    )
    return sources


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[2:], sources))
