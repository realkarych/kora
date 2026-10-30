import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.ci.mutation_audit import ROOT, classify, command, inventory
from scripts.ci.mutation_models import Report, ordered
from scripts.ci.mutation_process import Stop, execute


def main() -> None:
    with TemporaryDirectory(prefix="mutation-native-") as temporary:
        root = Path(temporary)
        for name in ("src/kora/ir/pricing.py", "src/kora/cel/paths.py"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def value() -> int:\n    return 2\n")
        (root / "test_sample.py").write_text(
            "from src.kora.cel.paths import value\n"
            "def test_value() -> None:\n    assert value() == 2\n"
        )
        stop = Stop()
        deadline = time.monotonic() + 30
        tool = ROOT / ".venv/bin/cosmic-ray"
        work = inventory(root, tool, deadline, stop)
        target = next(item for item in work if item.module_path.endswith("paths.py"))
        original = (root / target.module_path).read_text()
        arguments = [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "--tb=no",
            "--junitxml=junit.xml",
            "test_sample.py",
        ]

        baseline = execute(arguments, root, deadline, stop)
        baseline_outcome = classify(baseline, root / "junit.xml")
        command(
            [
                str(tool),
                "apply",
                target.module_path,
                target.operator_name,
                str(target.occurrence),
            ],
            root,
            deadline,
            stop,
        )
        changed = execute(arguments, root, deadline, stop)
        changed_outcome = classify(changed, root / "junit.xml")

        assert baseline_outcome == "survived"
        assert changed_outcome == "killed"
        (root / target.module_path).write_text(
            "def earlier() -> int:\n    return 7\n\n" + original
        )
        (root / "audit.sqlite").unlink()
        later = inventory(root, tool, deadline, stop)
        previous = Report(
            source_revision="a" * 40,
            tooling_revision="a" * 40,
            next_identity=target.identity(),
        )

        queue = ordered(later, previous)

        assert any(item.identity() == target.identity() for item in later)
        assert queue[1].identity() == target.identity()


if __name__ == "__main__":
    main()
