import json

import pytest

from kora.executor.captures import Captures, parsed
from kora.executor.failures import ExecutorError, Phase
from kora.ir.vcs_ref import MAX_PATH_BYTES, MAX_PATHS
from kora.protocols.vcs import Capture

DIGEST: str = "sha256:" + "a" * 64
MALFORMED: str = "capture_malformed"
OVERSIZED: str = "capture_oversized"


def manifest(**changes: object) -> bytes:
    document: dict[str, object] = {
        "prepared_digest": DIGEST,
        "revision": "a" * 40,
        "content_index_location": "index.jsonl",
        "content_index_digest": DIGEST,
        "complete": True,
        **changes,
    }
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    ("capture", "output", "expected"),
    [
        ("commit", b"a" * 40 + b"\n", {"commit": "a" * 40}),
        ("branch", b"kora/work", {"branch": "kora/work"}),
        ("changed_paths", b'["a.txt", "dir/b"]', {"changed_paths": ("a.txt", "dir/b")}),
        ("diff_stat", b" 1 file changed\n", {"diff_stat": " 1 file changed"}),
        ("diff", b"-a\n+b\n", {"diff": "-a\n+b\n"}),
    ],
)
def test_capture_parsers_accept_their_forms(
    capture: Capture, output: bytes, expected: dict[str, object]
) -> None:
    result = parsed(Captures(), capture, output, False, Phase.INSPECT)

    assert {name: getattr(result, name) for name in expected} == expected


def test_inspection_capture_is_a_manifest() -> None:
    result = parsed(Captures(), "inspection", manifest(), False, Phase.INSPECT)

    assert result.inspection is not None
    assert result.inspection.complete is True


@pytest.mark.parametrize(
    ("capture", "output", "code"),
    [
        ("commit", b"", MALFORMED),
        ("commit", b"a b\n", MALFORMED),
        ("commit", b"one\ntwo\n", MALFORMED),
        ("commit", b"x" * 256, MALFORMED),
        ("commit", b"\xff\xfe", MALFORMED),
        ("branch", b"with space", MALFORMED),
        ("changed_paths", b"a.txt\nb.txt\n", MALFORMED),
        ("changed_paths", b'{"a": 1}', MALFORMED),
        ("changed_paths", b'["/abs"]', MALFORMED),
        ("changed_paths", b'["a/../b"]', MALFORMED),
        ("changed_paths", b'["a", "a"]', OVERSIZED),
        ("changed_paths", b"[1]", MALFORMED),
        ("changed_paths", b'["a\\u0000b"]', MALFORMED),
        (
            "changed_paths",
            json.dumps([f"{i}" for i in range(MAX_PATHS + 1)]).encode(),
            OVERSIZED,
        ),
        (
            "changed_paths",
            json.dumps(
                [f"{i:05d}" + "x" * 250 for i in range(MAX_PATH_BYTES // 255 + 1)]
            ).encode(),
            OVERSIZED,
        ),
        ("diff_stat", b"\x00", MALFORMED),
        ("diff_stat", b"x" * 4097, MALFORMED),
        ("inspection", b"{}", MALFORMED),
        ("inspection", manifest(complete="yes"), MALFORMED),
        ("inspection", manifest(extra=1), MALFORMED),
        ("inspection", manifest(prepared_digest="md5:1"), MALFORMED),
    ],
)
def test_capture_parsers(capture: Capture, output: bytes, code: str) -> None:
    with pytest.raises(ExecutorError) as raised:
        parsed(Captures(), capture, output, False, Phase.INSPECT)

    assert raised.value.failure_class.value == "artifact_invalid"
    assert raised.value.code == code


def test_an_overflowing_capture_is_refused() -> None:
    with pytest.raises(ExecutorError) as raised:
        parsed(Captures(), "commit", b"a" * 40, True, Phase.CHECKOUT)

    assert raised.value.code == "capture_oversized"
