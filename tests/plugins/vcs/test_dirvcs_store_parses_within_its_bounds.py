import pytest

from kora.executor import seal
from kora.plugins.vcs.fake import store

PATHS: tuple[str, ...] = (
    "ok/name.txt",
    "é/ok",
    ".git/config",
    "a/.GIT",
    ".g‌it/x",
    ".git./x",
    ".git /x",
    "GIT~1/x",
    "kora~1",
    ".git:stream/x",
    "a/../b",
    "./a",
    "/absolute",
    "a//b",
    "a\x00b",
    "tab\there",
    "x" * 256,
)


def entry(path: str, mode: object = store.REGULAR) -> dict[str, object]:
    return {"path": path, "mode": mode, "size": 1, "sha256": "a" * 64}


def refused(data: bytes) -> bool:
    try:
        store.tree_of(data)
    except store.Refused:
        return True
    return False


@pytest.mark.parametrize("path", PATHS)
def test_dirvcs_path_rules_match_the_sealing_rules(path: str) -> None:
    expected = seal.path_allowed(path)

    allowed = store.path_allowed(path)

    assert allowed == expected


def test_dirvcs_tree_parsing_refuses_before_allocation() -> None:
    too_many = store.canonical(
        [entry(f"f{index:06d}") for index in range(store.MAX_TREE_ENTRIES + 1)]
    )
    empty_objects = b"[" + b",".join([b"{}"] * (store.MAX_TREE_TOKENS + 1)) + b"]"
    unhashable_mode = store.canonical([entry("a", [])])
    large_symlink = store.canonical(
        [{**entry("link", store.SYMLINK), "size": store.MAX_SYMLINK_BYTES + 1}]
    )
    accepted = store.canonical([entry("a"), entry("b/c")])
    unterminated = b'["' + b'\\"' * (store.MIB // 2)
    punctuated = store.canonical(
        [entry(f"{index:05d},[{{" * 30) for index in range(5_000)]
    )

    outcomes = [
        refused(data)
        for data in (
            too_many,
            empty_objects,
            unhashable_mode,
            large_symlink,
            unterminated,
            accepted,
            punctuated,
        )
    ]

    assert outcomes == [True, True, True, True, True, False, False]


def test_dirvcs_deep_paths_and_nonadjacent_parent_conflicts() -> None:
    deep = store.canonical(
        [entry(f"f{index:04d}/" + "a/" * 100 + "x") for index in range(1000)]
    )
    conflicting = store.canonical([entry("a"), entry("a-"), entry("a/x")])

    entries = store.tree_of(deep)
    conflict_refused = refused(conflicting)

    assert len(entries) == 1000
    assert conflict_refused
