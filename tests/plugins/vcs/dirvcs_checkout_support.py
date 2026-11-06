import os
from collections.abc import Mapping
from pathlib import Path

from kora.plugins.vcs.fake import store
from kora.plugins.vcs.local import LocalConfiguration
from tests.plugins.vcs.git_support import (
    BINDING,
    GONE_BINDING,
    GONE_REPO,
    PROVIDER_INSTANCE,
    REPO,
    STABLE_ID,
)

SEED_FILES: dict[str, tuple[str, bytes]] = {
    "a.txt": (store.REGULAR, b"alpha\n"),
    "run.sh": (store.EXECUTABLE, b"exit 0\n"),
    "link": (store.SYMLINK, b"a.txt"),
}


def empty_store(path: Path) -> None:
    for section in (*store.SECTIONS, "heads"):
        (path / section).mkdir(parents=True)


def put(path: Path, name: str, data: bytes) -> None:
    target = path / name
    if not target.exists():
        target.write_bytes(data)


def stored_revision(
    remote: Path,
    files: Mapping[str, tuple[str, bytes]],
    parent: str | None,
    message: str,
) -> str:
    entries: list[store.Entry] = []
    for path, (mode, data) in files.items():
        sha = store.digest_of(data)
        put(remote / "blobs", sha, data)
        entries.append(store.Entry(path, mode, len(data), sha))
    tree_data = store.tree_document(entries)
    tree = store.digest_of(tree_data)
    put(remote / "trees", tree, tree_data)
    document = store.revision_document(parent, message, tree)
    identifier = store.digest_of(document)
    put(remote / "revisions", identifier, document)
    return identifier


def set_head(remote: Path, branch: str, revision: str) -> None:
    target = remote / "heads" / branch
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(revision, encoding="ascii")


def configuration(remote: Path, gone: Path) -> LocalConfiguration:
    principal: dict[str, object] = {
        "provider_instance": PROVIDER_INSTANCE,
        "stable_id": STABLE_ID,
        "display_name": "kora local publisher",
    }

    def remote_entry(path: Path, binding: str) -> dict[str, object]:
        return {
            "path": str(path),
            "binding": binding,
            "binding_revision": 1,
            "credential_mode": "local_credential_free",
            "principal": principal,
        }

    return LocalConfiguration.model_validate(
        {
            "remotes": {
                REPO: remote_entry(remote, BINDING),
                GONE_REPO: remote_entry(gone, GONE_BINDING),
            },
            "tool_path": os.environ["PATH"].split(":"),
        }
    )


def seeded_store(root: Path) -> tuple[Path, str]:
    remote = root / "remote.dirvcs"
    empty_store(remote)
    base = stored_revision(remote, SEED_FILES, None, "seed")
    set_head(remote, "main", base)
    return remote, base
