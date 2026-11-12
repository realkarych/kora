import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT, INSTANCE_ID, utc_now
from kora.conformance.vcs import Checkout, prepared_from, run, sealed_input
from kora.executor.digests import input_digest
from kora.executor.entry import PreparedResult
from kora.executor.failures import ExecutorError
from kora.executor.launcher import (
    LaunchSpec,
    ProcessLauncher,
    StepLauncher,
    StepOutcome,
)
from kora.executor.seal import file_digest, sealed_names
from kora.ir.node import ErrorClass
from kora.observability import LoggingSettings
from kora.plugins.vcs.fake import store
from kora.protocols.vcs import PublicationInput, PublishPlan
from tests.plugins.vcs.dirvcs_support import (
    SEED_FILES,
    DirWorld,
    make_dir_world,
    residue,
)

OTHER: str = "f" * 64


@dataclass(frozen=True)
class ImportEnd:
    exit_code: int | None
    residue: list[str]
    objects: list[str]


def stored_objects(root: Path) -> list[str]:
    return sorted(
        f"{section}/{path.name}"
        for section in store.SECTIONS
        for path in (root / section).iterdir()
    )


@dataclass
class ImportRecorder(StepLauncher):
    inner: StepLauncher = field(default_factory=ProcessLauncher)
    snapshots: list[ImportEnd] = field(default_factory=lambda: [])

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        outcome = await self.inner.run(spec)
        if "import" in spec.argv:
            self.snapshots.append(
                ImportEnd(
                    outcome.exit_code,
                    residue(spec.cwd),
                    stored_objects(spec.cwd / store.PREPARED_STORE),
                )
            )
        return outcome


@dataclass(frozen=True)
class Bench:
    world: DirWorld
    checkout: Checkout
    plan: PublishPlan
    sealed: PublicationInput
    base_tree: str
    recorder: ImportRecorder


@dataclass(frozen=True)
class Forged:
    head: str
    bundle: bytes


Forge = Callable[[Bench], Forged]


def revision(parent: str | None, tree: str, message: str = "change") -> bytes:
    return store.revision_document(parent, message, tree)


def blob_entry(path: str, data: bytes, mode: str = store.REGULAR) -> store.Entry:
    return store.Entry(path, mode, len(data), store.digest_of(data))


def seed_entries() -> list[store.Entry]:
    return [blob_entry(path, data, mode) for path, (mode, data) in SEED_FILES.items()]


def records(*items: tuple[bytes, bytes]) -> bytes:
    return b"".join(
        store.RECORD_HEADER.pack(kind, len(data)) + data for kind, data in items
    )


def single(bench: Bench, tree: bytes, blobs: tuple[bytes, ...] = ()) -> Forged:
    head = revision(bench.world.base, store.digest_of(tree))
    items = [(store.REVISION_KIND, head), (store.TREE_KIND, tree)]
    items += [(store.BLOB_KIND, blob) for blob in blobs]
    return Forged(store.digest_of(head), records(*items))


def with_file(path: str, data: bytes, mode: str = store.REGULAR) -> bytes:
    return store.tree_document([*seed_entries(), blob_entry(path, data, mode)])


def ordinary(bench: Bench) -> Forged:
    first_tree = with_file("one.txt", b"one\n")
    second_tree = with_file("two.txt", b"two\n")
    first = revision(bench.world.base, store.digest_of(first_tree))
    second = revision(store.digest_of(first), bench.base_tree)
    third = revision(store.digest_of(second), store.digest_of(second_tree))
    return Forged(
        store.digest_of(third),
        records(
            (store.REVISION_KIND, first),
            (store.REVISION_KIND, second),
            (store.REVISION_KIND, third),
            (store.TREE_KIND, first_tree),
            (store.TREE_KIND, second_tree),
            (store.BLOB_KIND, b"one\n"),
            (store.BLOB_KIND, b"two\n"),
            (store.BLOB_KIND, b"alpha\n"),
        ),
    )


def forked(bench: Bench) -> Forged:
    first = revision(bench.world.base, bench.base_tree, "first")
    second = revision(bench.world.base, bench.base_tree, "second")
    return Forged(
        store.digest_of(second),
        records((store.REVISION_KIND, first), (store.REVISION_KIND, second)),
    )


def not_the_tip(bench: Bench) -> Forged:
    first = revision(bench.world.base, bench.base_tree, "first")
    second = revision(store.digest_of(first), bench.base_tree, "second")
    return Forged(
        store.digest_of(first),
        records((store.REVISION_KIND, first), (store.REVISION_KIND, second)),
    )


def head_is_the_base(bench: Bench) -> Forged:
    first = revision(bench.world.base, bench.base_tree)
    return Forged(bench.world.base, records((store.REVISION_KIND, first)))


def missing_intermediate(bench: Bench) -> Forged:
    first = revision(bench.world.base, bench.base_tree, "first")
    second = revision(store.digest_of(first), bench.base_tree, "second")
    return Forged(store.digest_of(second), records((store.REVISION_KIND, second)))


def unknown_base(bench: Bench) -> Forged:
    first = revision(OTHER, bench.base_tree)
    return Forged(store.digest_of(first), records((store.REVISION_KIND, first)))


def missing_blob(bench: Bench) -> Forged:
    return single(bench, with_file("new.txt", b"absent\n"))


def corrupt_blob(bench: Bench) -> Forged:
    return single(bench, with_file("new.txt", b"intact\n"), (b"broken\n",))


def missing_tree(bench: Bench) -> Forged:
    head = revision(bench.world.base, OTHER)
    return Forged(store.digest_of(head), records((store.REVISION_KIND, head)))


def corrupt_tree(bench: Bench) -> Forged:
    tree = with_file("new.txt", b"new\n")
    head = revision(bench.world.base, store.digest_of(tree))
    return Forged(
        store.digest_of(head),
        records(
            (store.REVISION_KIND, head),
            (store.TREE_KIND, tree.replace(b"new.txt", b"new.tx0")),
            (store.BLOB_KIND, b"new\n"),
        ),
    )


def unnamed_tree(bench: Bench) -> Forged:
    head = revision(bench.world.base, bench.base_tree)
    return Forged(
        store.digest_of(head),
        records((store.REVISION_KIND, head), (store.TREE_KIND, with_file("x", b"x"))),
    )


def unnamed_blob(bench: Bench) -> Forged:
    return single(bench, with_file("new.txt", b"new\n"), (b"new\n", b"stray\n"))


def non_canonical(bench: Bench) -> Forged:
    head = json.dumps(
        {"parent": bench.world.base, "message": "spaced", "tree": bench.base_tree}
    ).encode()
    return Forged(store.digest_of(head), records((store.REVISION_KIND, head)))


def oversized_prefix(bench: Bench) -> Forged:
    head = revision(bench.world.base, bench.base_tree)
    header = store.RECORD_HEADER.pack(store.REVISION_KIND, len(head) + 1)
    return Forged(store.digest_of(head), header + head)


def too_many_records(bench: Bench) -> Forged:
    parent = bench.world.base
    items: list[tuple[bytes, bytes]] = []
    for index in range(store.MAX_REVISIONS + 1):
        document = revision(parent, bench.base_tree, f"r{index}")
        items.append((store.REVISION_KIND, document))
        parent = store.digest_of(document)
    return Forged(parent, records(*items))


def unknown_kind(bench: Bench) -> Forged:
    head = revision(bench.world.base, bench.base_tree)
    return Forged(
        store.digest_of(head),
        records((store.REVISION_KIND, head), (b"X", b"payload")),
    )


def truncated_header(bench: Bench) -> Forged:
    head = revision(bench.world.base, bench.base_tree)
    return Forged(
        store.digest_of(head), records((store.REVISION_KIND, head)) + b"R\x00"
    )


def section_order(bench: Bench) -> Forged:
    tree = with_file("new.txt", b"new\n")
    first = revision(bench.world.base, store.digest_of(tree))
    second = revision(store.digest_of(first), bench.base_tree)
    return Forged(
        store.digest_of(second),
        records(
            (store.REVISION_KIND, first),
            (store.TREE_KIND, tree),
            (store.REVISION_KIND, second),
            (store.BLOB_KIND, b"new\n"),
        ),
    )


def duplicate_tree(bench: Bench) -> Forged:
    tree = with_file("new.txt", b"new\n")
    head = revision(bench.world.base, store.digest_of(tree))
    return Forged(
        store.digest_of(head),
        records(
            (store.REVISION_KIND, head),
            (store.TREE_KIND, tree),
            (store.TREE_KIND, tree),
            (store.BLOB_KIND, b"new\n"),
        ),
    )


def duplicate_blob(bench: Bench) -> Forged:
    return single(bench, with_file("new.txt", b"new\n"), (b"new\n", b"new\n"))


def empty_bundle(bench: Bench) -> Forged:
    head = revision(bench.world.base, bench.base_tree)
    return Forged(store.digest_of(head), b"")


def sized(path: str, data: bytes, size: int) -> store.Entry:
    return store.Entry(path, store.REGULAR, size, store.digest_of(data))


def size_differs_from_base(bench: Bench) -> Forged:
    others = [entry for entry in seed_entries() if entry.path != "a.txt"]
    tree = store.tree_document([*others, sized("new.txt", b"alpha\n", 3)])
    return single(bench, tree)


def size_differs_within_chain(bench: Bench) -> Forged:
    first_tree = store.tree_document([*seed_entries(), sized("new.txt", b"new\n", 4)])
    second_tree = store.tree_document([*seed_entries(), sized("new.txt", b"new\n", 5)])
    first = revision(bench.world.base, store.digest_of(first_tree))
    second = revision(store.digest_of(first), store.digest_of(second_tree))
    return Forged(
        store.digest_of(second),
        records(
            (store.REVISION_KIND, first),
            (store.REVISION_KIND, second),
            (store.TREE_KIND, first_tree),
            (store.TREE_KIND, second_tree),
            (store.BLOB_KIND, b"new\n"),
        ),
    )


def size_differs_from_chain_blob(bench: Bench) -> Forged:
    tree = store.tree_document([*seed_entries(), sized("new.txt", b"new\n", 5)])
    return single(bench, tree, (b"new\n",))


def escaped_message(bench: Bench) -> Forged:
    head = revision(bench.world.base, bench.base_tree, 'pa"ss')
    return Forged(store.digest_of(head), records((store.REVISION_KIND, head)))


def path_leg(*entries: store.Entry) -> Forge:
    return lambda bench: single(bench, store.tree_document([*seed_entries(), *entries]))


def named(path: str, mode: str = store.REGULAR) -> store.Entry:
    return blob_entry(path, b"alpha\n", mode)


LEGS: dict[str, Forge] = {
    "forked": forked,
    "not_the_tip": not_the_tip,
    "head_is_the_base": head_is_the_base,
    "unknown_base": unknown_base,
    "missing_intermediate": missing_intermediate,
    "missing_blob": missing_blob,
    "corrupt_blob": corrupt_blob,
    "missing_tree": missing_tree,
    "corrupt_tree": corrupt_tree,
    "unnamed_tree": unnamed_tree,
    "unnamed_blob": unnamed_blob,
    "non_canonical": non_canonical,
    "oversized_prefix": oversized_prefix,
    "too_many_records": too_many_records,
    "reserved_git": path_leg(named(".git/config")),
    "reserved_kora": path_leg(named("x/.KORA/y")),
    "gitlink": path_leg(named("sub", "gitlink")),
    "traversing": path_leg(named("x/../a.txt")),
    "case_fold": path_leg(named("A.txt")),
    "symlink_parent": path_leg(named("link", store.SYMLINK), named("link/x")),
    "unknown_kind": unknown_kind,
    "truncated_header": truncated_header,
    "section_order": section_order,
    "duplicate_tree": duplicate_tree,
    "duplicate_blob": duplicate_blob,
    "empty_bundle": empty_bundle,
    "size_differs_from_base": size_differs_from_base,
    "size_differs_within_chain": size_differs_within_chain,
    "size_differs_from_chain_blob": size_differs_from_chain_blob,
    "escaped_message": escaped_message,
}


def built_bench(world: DirWorld, recorder: ImportRecorder) -> Bench:
    checkout = world.checkout()
    world.commit(checkout, {"notes.txt": "candidate\n"}, "candidate")
    plan, sealed = run(
        sealed_input(
            world.provider,
            world.executor,
            world.remote_fixture(),
            checkout,
            world.context(),
        )
    )
    base_tree = store.digest_of(store.tree_document(seed_entries()))
    return Bench(world, checkout, plan, sealed, base_tree, recorder)


def recorded_world(root: Path, capture: Capture) -> tuple[DirWorld, ImportRecorder]:
    recorder = ImportRecorder()
    return make_dir_world(root, capture, launcher=recorder), recorder


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Generator[Bench]:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), INSTANCE_ID, utc_now
    ) as capture:
        world, recorder = recorded_world(tmp_path_factory.mktemp("chains"), capture)
        yield built_bench(world, recorder)


def resealed(bench: Bench, forged: Forged) -> PublicationInput:
    original = Path(bench.sealed.location)
    copy = original.parent / f"input-{os.urandom(8).hex()}"
    shutil.copytree(original, copy)
    (copy / "head.json").write_text(json.dumps({"head": forged.head, "bundle": True}))
    (copy / "head.bundle").write_bytes(forged.bundle)
    directory = os.open(copy, os.O_RDONLY | os.O_DIRECTORY)
    try:
        digests = [
            file_digest(directory, name, 2**32, threading.Event())
            for name in sealed_names(directory, True)
        ]
    finally:
        os.close(directory)
    return PublicationInput(
        attempt_id=bench.sealed.attempt_id,
        format=bench.sealed.format,
        location=str(copy),
        digest=input_digest(digests),
    )


def prepared(bench: Bench, sealed: PublicationInput) -> PreparedResult:
    world = bench.world
    done = prepared_from(
        world.executor,
        world.binding(),
        bench.checkout,
        bench.plan,
        sealed,
        world.context(),
    )
    return run(done).result


def base_copy(bench: Bench) -> list[str]:
    return [
        name
        for name in stored_objects(bench.world.remote)
        if not name.startswith("blobs/")
    ]


def leftovers(bench: Bench) -> list[str]:
    publication = bench.checkout.areas.publication
    assert publication is not None
    return sorted(
        path.name for path in publication.iterdir() if path.name.startswith("prepared-")
    )


@pytest.mark.parametrize("leg", sorted(LEGS))
def test_dirvcs_prepare_refuses_foreign_chains(bench: Bench, leg: str) -> None:
    sealed = resealed(bench, LEGS[leg](bench))
    bench.recorder.snapshots.clear()

    with pytest.raises(ExecutorError) as raised:
        prepared(bench, sealed)

    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID
    assert raised.value.code == "step_failed"
    assert bench.recorder.snapshots == [ImportEnd(store.REFUSED, [], base_copy(bench))]
    assert leftovers(bench) == []
    assert bench.world.remote_head() is None


def test_dirvcs_prepare_accepts_an_ordinary_chain(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world, recorder = recorded_world(tmp_path, kora_capture)
    own = built_bench(world, recorder)
    sealed = resealed(own, ordinary(own))

    result = prepared(own, sealed)

    assert result.revision.base_commit == world.base
    assert "two.txt" in result.publication.changed_paths
    assert "one.txt" in result.publication.changed_paths
    assert [(end.exit_code, end.residue) for end in recorder.snapshots] == [(0, [])]


@pytest.mark.parametrize("leg", ["special_file", "escaped_message"])
def test_candidate_commit_refuses_invalid_input(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    head = candidate / ".kora" / "dirvcs" / "head"
    before = head.read_text()
    if leg == "special_file":
        os.mkfifo(candidate / "special")

    with pytest.raises(subprocess.CalledProcessError) as raised:
        world.commit(checkout, {}, 'a"b' if leg == "escaped_message" else "plain")

    assert raised.value.returncode == store.REFUSED
    assert head.read_text() == before
    assert list(candidate.rglob(".tmp-*")) == []
