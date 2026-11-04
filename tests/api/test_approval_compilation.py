import pytest

from tests.cel_eval.children import ROOT, full_path, run_child

PROBE: str = "from tests.api.approval_compilation_probe import main\nmain()"


@pytest.mark.parametrize("case", ["compile", "refuse", "cancel"])
def test_root_approval_rendering_uses_the_real_evaluator(case: str) -> None:
    path = full_path(ROOT)

    result = run_child(PROBE, path, case)

    assert result.code == 0, result.stderr
    assert result.stdout == result.stderr == ""
