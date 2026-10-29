import os
import re
import shutil
import subprocess
from pathlib import Path

EDITORIAL_PATHS = frozenset({"README.md", "docs/positioning.md"})
MAX_DOCUMENT_BYTES = 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
MARKUP = re.compile(r"[`\[\]<>#*_~\\|{}!]|^(?:\s|[-+>] |[0-9]+[.)] )")


def git(repository: Path, *arguments: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("git is required")
    return subprocess.run(
        [executable, "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def fingerprint(content: bytes) -> tuple[str, ...]:
    if len(content) > MAX_DOCUMENT_BYTES or b"\0" in content:
        raise ValueError("document is not bounded UTF-8 text")
    text = content.decode("utf-8")
    normalized = text.replace("\r\n", "\n")
    if (
        any(marker in text for marker in ("```", "~~~", "<", ">"))
        or "\r" in normalized
        or any(character.isspace() and character not in " \n\r" for character in text)
    ):
        return (text,)
    text = normalized.rstrip("\n")
    paragraphs = text.split("\n\n")
    return tuple(
        " ".join(paragraph.split())
        if paragraph
        and all(
            not MARKUP.search(line)
            and re.fullmatch(r"[=-]+\s*", line) is None
            and not line.endswith("  ")
            for line in paragraph.split("\n")
        )
        else paragraph
        for paragraph in paragraphs
    )


def editorial(repository: Path, base: str, head: str) -> bool:
    if SHA.fullmatch(base) is None or SHA.fullmatch(head) is None:
        return False
    if git(repository, "rev-parse", "--is-shallow-repository").strip() != b"false":
        return False
    ancestor = git(repository, "merge-base", base, head).decode().strip()
    changes = (
        git(repository, "diff", "--name-status", "--no-renames", "-z", ancestor, head)
        .decode()
        .split("\0")
    )
    if changes[-1] != "" or len(changes) < 3 or len(changes) % 2 != 1:
        return False
    for index in range(0, len(changes) - 1, 2):
        status, path = changes[index : index + 2]
        if status != "M" or path not in EDITORIAL_PATHS:
            return False
        for revision in (ancestor, head):
            entry = git(repository, "ls-tree", revision, "--", path)
            if not entry.startswith(b"100644 blob "):
                return False
        before = git(repository, "show", f"{ancestor}:{path}")
        after = git(repository, "show", f"{head}:{path}")
        if fingerprint(before) != fingerprint(after):
            return False
    return True


def main() -> None:
    full = True
    try:
        full = not editorial(
            Path.cwd(),
            os.environ["KORA_BASE_REVISION"],
            os.environ["KORA_HEAD_REVISION"],
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        full = True
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"full={str(full).lower()}\n")


if __name__ == "__main__":
    main()
