import hashlib
import json
import subprocess
import sys
from pathlib import Path

INDEX = Path("index.jsonl")


def corrupt_new_blob() -> None:
    blobs = Path("store") / "blobs"
    for blob in sorted(blobs.iterdir()):
        if blob.read_bytes() == b"draft\n":
            blob.write_bytes(b"other\n")


def rewritten(manifest: dict[str, object], lines: list[bytes]) -> None:
    data = b"".join(lines)
    INDEX.write_bytes(data)
    manifest["content_index_digest"] = "sha256:" + hashlib.sha256(data).hexdigest()


def tampered(mode: str, manifest: dict[str, object]) -> dict[str, object]:
    lines = INDEX.read_bytes().splitlines(keepends=True)
    if mode == "truncate":
        rewritten(manifest, lines[:-1])
    elif mode == "digest":
        manifest["content_index_digest"] = "sha256:" + "0" * 64
    elif mode == "incomplete":
        manifest["complete"] = False
    return manifest


def main(arguments: list[str]) -> int:
    mode, helper, *rest = arguments
    if mode == "corrupt":
        corrupt_new_blob()
    completed = subprocess.run(
        [sys.executable, "-I", helper, *rest], stdout=subprocess.PIPE, check=False
    )
    if completed.returncode != 0:
        return completed.returncode
    manifest: dict[str, object] = json.loads(completed.stdout)
    sys.stdout.write(json.dumps(tampered(mode, manifest)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
