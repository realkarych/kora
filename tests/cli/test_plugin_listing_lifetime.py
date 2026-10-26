import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import cast

import pytest

from tests.support.provider_tooling import build_fixture_wheel, install_tooling


@pytest.mark.parametrize(
    ("case", "resistant", "phase", "retained"),
    [
        ("cancel", False, "factory", False),
        ("cancel", False, "import", False),
        ("repeat_cancel", False, "factory", False),
        ("cancel", True, "factory", False),
        ("cancel_before_timer", True, "factory", False),
        ("result_wait_timeout", True, "factory", False),
        ("timeout", False, "factory", False),
        ("crash", False, "factory", False),
        ("descendant", False, "factory", False),
        ("cancel", False, "factory", True),
        ("crash", False, "factory", True),
        ("retained", False, "factory", True),
        ("unknown", False, "factory", False),
    ],
)
def test_listing_joins_owned_processes_on_every_exit(
    tmp_path: Path,
    provider_tooling_wheelhouse: Path,
    case: str,
    resistant: bool,
    phase: str,
    retained: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix="kora-list-probe-", dir="/tmp") as private:
        entry = (
            "kora_tooling_fixture.blocked_import:provider_metadata"
            if phase == "import"
            else "kora_tooling_fixture.control:controlled"
        )
        fixture = build_fixture_wheel(
            tmp_path,
            {"policy.controlled": entry},
            {
                "socket": str(Path(private).resolve() / "c"),
                "resistant": resistant,
                "retained": retained,
            },
        )
        installed = install_tooling(tmp_path, provider_tooling_wheelhouse, fixture)

        completed = subprocess.run(
            [str(installed.python), "-I", "-m", "kora_tooling_fixture.probe", case],
            cwd=installed.outside,
            env={"PATH": os.defpath},
            capture_output=True,
            check=False,
            timeout=20,
        )
        payload = (
            cast(dict[str, object], json.loads(completed.stdout))
            if completed.stdout
            else {}
        )

        if completed.returncode != 0:
            pytest.fail(completed.stderr.decode(errors="replace"), pytrace=False)
        assert payload["closed"] is True
        assert payload["reaped"] is True
        if resistant:
            assert payload["returncode"] == -signal.SIGKILL
        assert payload["pid_absent"] is True
        assert payload["descendant_absent"] is True
        assert payload["group_empty"] is True
        assert payload["descriptors_before"] == payload["descriptors_after"]
        assert payload["remaining_tasks"] == 1
        assert payload["cleanup_reason"] == (
            "closure_unknown"
            if case == "unknown"
            else "state_retained"
            if retained
            else None
        )
        assert payload["retained_content"] == (
            "closure_unknown"
            if case == "unknown"
            else "provider-owned-content"
            if retained
            else None
        )
        if retained:
            assert b"cli.provider_listing_retained" in completed.stderr
        assert payload["outcome"] == (
            "cancelled"
            if case
            in {
                "cancel",
                "cancel_before_timer",
                "result_wait_timeout",
                "repeat_cancel",
                "unknown",
            }
            else "invalid_discovery"
            if case in {"timeout", "crash"}
            else "state_retained"
            if case == "retained"
            else "completed"
        )
