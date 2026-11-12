import os
from collections.abc import Generator
from pathlib import Path

import pytest

from kora.plugins.vcs.fake import store
from tests.plugins.vcs.dirvcs_checkout_support import empty_store, stored_revision

BLOB_SHA: str = store.digest_of(b"blob")


@pytest.fixture
def staged(tmp_path: Path) -> Generator[store.Scratch]:
    directory = os.open(tmp_path, store.DIRECTORY_FLAGS)
    scratch = store.scratch_file(directory)
    scratch.close()
    yield scratch
    store.discard(scratch)
    os.close(directory)


@pytest.mark.parametrize(
    ("kind", "bound"),
    list(store.RECORD_BOUNDS.items()),
    ids=["revision", "tree", "blob"],
)
def test_chain_header_refuses_each_kind_past_its_bound(
    tmp_path: Path, kind: bytes, bound: int
) -> None:
    source = tmp_path / "chain"
    source.write_bytes(
        store.RECORD_HEADER.pack(kind, bound)
        + store.RECORD_HEADER.pack(kind, bound + 1)
    )
    descriptor = os.open(source, os.O_RDONLY)

    try:
        reader = store.ChainReader(descriptor, 2**40, -1)
        accepted = reader.header()
        with pytest.raises(store.Refused):
            reader.header()
    finally:
        os.close(descriptor)

    assert accepted == (kind, bound)


def test_open_chain_refuses_past_the_byte_bound(tmp_path: Path) -> None:
    bundle = tmp_path / "head.bundle"
    bundle.touch()
    sealed = os.open(tmp_path, store.DIRECTORY_FLAGS)
    baseline = len(os.listdir("/dev/fd"))

    try:
        os.truncate(bundle, store.MAX_CHAIN_BYTES)
        descriptor, size = store.open_chain(sealed)
        os.close(descriptor)
        os.truncate(bundle, store.MAX_CHAIN_BYTES + 1)
        with pytest.raises(store.Refused):
            store.open_chain(sealed)
        remaining = len(os.listdir("/dev/fd"))
    finally:
        os.close(sealed)

    assert size == store.MAX_CHAIN_BYTES
    assert remaining == baseline


def test_chain_refuses_a_blob_past_its_count_bound(staged: store.Scratch) -> None:
    chain = store.Chain()
    chain.named[BLOB_SHA] = 4
    chain.blobs = {str(index): staged for index in range(store.MAX_CHAIN_BLOBS - 1)}
    store.add_blob(chain, staged, BLOB_SHA, 4)
    chain.named["another"] = 4

    with pytest.raises(store.Refused):
        store.add_blob(chain, staged, "another", 4)

    assert len(chain.blobs) == store.MAX_CHAIN_BLOBS


def chain_naming(identifier: str, named: int) -> store.Chain:
    chain = store.Chain()
    chain.revisions.append(store.Revision("r" * 64, None, "message", identifier, 1))
    chain.named_trees.add(identifier)
    chain.named = dict.fromkeys(map(str, range(named)), 1)
    return chain


def test_chain_refuses_a_tree_past_the_named_blob_bound(
    staged: store.Scratch,
) -> None:
    tree = store.tree_document([store.Entry("file", store.REGULAR, 4, BLOB_SHA)])
    written = os.open(staged.name, os.O_WRONLY | os.O_CLOEXEC, dir_fd=staged.directory)
    os.write(written, tree)
    os.close(written)
    identifier = store.digest_of(tree)
    at_bound = chain_naming(identifier, store.MAX_NAMED_BLOBS - 1)
    past_bound = chain_naming(identifier, store.MAX_NAMED_BLOBS)

    store.add_tree(at_bound, staged, identifier)
    with pytest.raises(store.Refused):
        store.add_tree(past_bound, staged, identifier)

    assert len(at_bound.named) == store.MAX_NAMED_BLOBS


def test_index_data_refuses_past_its_byte_limit(tmp_path: Path) -> None:
    empty_store(tmp_path)
    base = stored_revision(tmp_path, {"a": (store.REGULAR, b"a")}, None, "base")
    head = stored_revision(tmp_path, {"a": (store.REGULAR, b"b")}, base, "head")
    root = os.open(tmp_path, store.DIRECTORY_FLAGS)
    prepared = store.Store(root)

    try:
        data = store.index_data(prepared, base, head, 2**20)
        exact = store.index_data(prepared, base, head, len(data))
        with pytest.raises(store.Refused):
            store.index_data(prepared, base, head, len(data) - 1)
    finally:
        prepared.close()
        os.close(root)

    assert data == exact
    assert len(data.splitlines()) == 2
