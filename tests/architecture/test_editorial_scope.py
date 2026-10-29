import subprocess
from pathlib import Path

import pytest

from scripts.ci.editorial_scope import editorial, git


def commit(root: Path) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD").decode().strip()


def document(root: Path, text: str) -> tuple[Path, str]:
    root.mkdir()
    git(root, "init", "--initial-branch=master")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    path = root / "README.md"
    path.write_text(text)
    return path, commit(root)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (
            "Plain prose with\na wrapped line.\n",
            "Plain prose with a\nwrapped line.\n",
            True,
        ),
        ("Plain prose.\n", "Plain prose.\n\n", True),
        ("The operation must stop.\n", "The operation may stop.\n", False),
        ("```python\nvalue = 1\n```\n", "```python\nvalue = 2\n```\n", False),
        ("[guide](one.md)\n", "[guide](two.md)\n", False),
        ("A paragraph.\n", "A paragraph.  \n", False),
        ("-\tfirst\n-\tsecond\n", "- first - second\n", False),
        ("- first - second\n", "-\tfirst\n-\tsecond\n", False),
        ("1.\tfirst\n2.\tsecond\n", "1. first 2. second\n", False),
        ("1. first 2. second\n", "1.\tfirst\n2.\tsecond\n", False),
        ("Heading\r===", "Heading ===", False),
        ("Heading ===", "Heading\r===", False),
        ("Heading\n---\n", "Heading ---\n", False),
        ("Heading ===\n", "Heading\n===\n", False),
        ("```text\nword\n", "```text\nword\n\n", False),
        (
            "```text\n\nword one\nword two\n\n```\n",
            "```text\n\nword one word two\n\n```\n",
            False,
        ),
        (
            "<div>\n\nword one\nword two\n\n</div>\n",
            "<div>\n\nword one word two\n\n</div>\n",
            False,
        ),
    ],
)
def test_editorial_scope_requires_unchanged_content(
    tmp_path: Path, before: str, after: str, expected: bool
) -> None:
    root = tmp_path / "repository"
    path, base = document(root, before)
    path.write_text(after)
    head = commit(root)

    classified = editorial(root, base, head)

    assert classified is expected


@pytest.mark.parametrize(
    "change",
    ["contract", "code", "classifier", "rename", "delete", "executable", "symlink"],
)
def test_uncertain_or_structural_changes_require_full_ci(
    tmp_path: Path, change: str
) -> None:
    root = tmp_path / "repository"
    path, base = document(root, "Plain prose.\n")
    if change in {"contract", "code", "classifier"}:
        name = {
            "contract": "knowledge/specs/example.md",
            "code": "example.py",
            "classifier": "scripts/ci/editorial_scope.py",
        }[change]
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("changed\n")
    elif change == "rename":
        path.rename(root / "RENAMED.md")
    elif change == "delete":
        path.unlink()
    elif change == "executable":
        path.chmod(0o755)
    else:
        path.unlink()
        path.symlink_to("untrusted.md")
    head = commit(root)

    classified = editorial(root, base, head)

    assert classified is False


def test_missing_base_defaults_to_full_ci(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _, head = document(root, "Plain prose.\n")

    with pytest.raises(subprocess.CalledProcessError):
        editorial(root, "f" * 40, head)
