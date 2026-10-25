import json
from pathlib import Path
from typing import Final

from tests.plugins.harness.fake.fake_support import result, tool_call, turn
from tests.support.observability import run_python

PROBE: Final[Path] = Path(__file__).with_name("script_probe.py")
REPOSITORY: Final[Path] = Path(__file__).resolve().parents[4]
PLANTED: Final[list[object]] = [
    turn("planted"),
    tool_call("Bash", "planted-call"),
    result("error"),
]


def test_fake_harness_reads_only_its_script(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / ".kora").mkdir(parents=True)
    planted = json.dumps({"steps": PLANTED, "end": "cli_exit_without_result"})
    for name in ("script.json", ".kora/script.json", "fake-script.jsonl"):
        (root / name).write_text(planted)
    environment = {
        "KORA_FAKE_SCRIPT": str(root / "script.json"),
        "KORA_SCRIPT_BOOK": str(root / "fake-script.jsonl"),
    }

    completed = run_python(["-I", str(PROBE), str(REPOSITORY), str(root)], environment)

    assert completed.code == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "kinds": ["turn", "result"],
        "stop_reason": "completed",
    }
