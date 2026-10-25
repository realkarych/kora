import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

OBJECT_ID: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
GITLINK: Final[bytes] = b"160000"
EXCLUDED: Final[frozenset[str]] = frozenset({".git", ".kora"})
REFUSED: Final[int] = 3


class ExportRefused(Exception):
    pass


def git(binary: str, *arguments: str, cwd: str | None = None) -> bytes:
    completed = subprocess.run(
        [binary, *arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ExportRefused
    return completed.stdout


def object_id(text: bytes) -> str:
    value = text.strip().decode("ascii", "replace")
    if OBJECT_ID.fullmatch(value) is None:
        raise ExportRefused
    return value


def fresh_directory(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, mode=0o700)


def gitlinks(binary: str) -> dict[bytes, str]:
    found: dict[bytes, str] = {}
    for record in git(binary, "ls-files", "-z", "--stage").split(b"\x00"):
        if not record:
            continue
        header, _, path = record.partition(b"\t")
        fields = header.split(b" ")
        if fields[0] == GITLINK:
            found[path] = object_id(fields[1])
    return found


def submodule_head(binary: str, path: bytes, recorded: str) -> str:
    directory = path.decode("utf-8")
    if not os.path.lexists(os.path.join(directory, ".git")):
        return recorded
    return object_id(git(binary, "rev-parse", "--verify", "HEAD", cwd=directory))


def manifest_lines(binary: str) -> list[str]:
    links = gitlinks(binary)
    listed = git(binary, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    lines: list[str] = []
    seen: set[bytes] = set()
    for raw in listed.split(b"\x00"):
        if not raw or raw in seen or raw.endswith(b"/"):
            continue
        seen.add(raw)
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ExportRefused from None
        if path.split("/", 1)[0] in EXCLUDED:
            continue
        recorded = links.get(raw)
        if recorded is not None:
            lines.append(
                json.dumps(
                    {"path": path, "gitlink": submodule_head(binary, raw, recorded)},
                    ensure_ascii=False,
                )
            )
        elif os.path.lexists(path):
            lines.append(
                json.dumps({"path": path, "gitlink": None}, ensure_ascii=False)
            )
    return lines


def export(binary: str, checkout: str, output: Path) -> None:
    fresh_directory(output)
    head = object_id(git(binary, "rev-parse", "--verify", "HEAD^{commit}"))
    bundle = head != checkout
    if bundle:
        git(
            binary,
            "bundle",
            "create",
            "--quiet",
            str(output / "head.bundle"),
            "HEAD",
            f"^{checkout}",
        )
    lines = manifest_lines(binary)
    (output / "manifest.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )
    (output / "head.json").write_text(
        json.dumps({"head": head, "bundle": bundle}), encoding="ascii"
    )


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="export")
    parser.add_argument("--git", required=True)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--out", required=True)
    options = parser.parse_args(arguments)
    binary: str = options.git
    checkout: str = options.checkout
    output: str = options.out
    if OBJECT_ID.fullmatch(checkout) is None:
        return REFUSED
    try:
        export(binary, checkout, Path(output))
    except ExportRefused:
        return REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
