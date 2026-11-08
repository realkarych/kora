import pytest

from tests.plugins.harness.codex.probe import ProbeFailure, verify_shell_patch_output


def patch_output(elapsed: str) -> str:
    return (
        "Wall time: 0.0000 seconds\nOutput:\nExit code: 0\n"
        f"Wall time: {elapsed} seconds\nOutput:\n"
        "Success. Updated the following files:\nA kora-probe.marker\n"
    )


@pytest.mark.parametrize("elapsed", ["0", "0.2", "1"])
def test_shell_patch_accepts_measured_inner_duration(elapsed: str) -> None:
    output = patch_output(elapsed)

    verify_shell_patch_output("selected-shell-patch-allow", output)


@pytest.mark.parametrize("elapsed", ["-0.2", "NaN"])
def test_shell_patch_refuses_invalid_inner_duration(elapsed: str) -> None:
    output = patch_output(elapsed)

    with pytest.raises(ProbeFailure, match="^shell_patch_function_result_missing$"):
        verify_shell_patch_output("selected-shell-patch-allow", output)
