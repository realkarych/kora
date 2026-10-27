import pytest

from kora.conformance.capture import MAX_CAPTURE_BYTES, Pipe
from kora.conformance.result import ChildResult, accepted_report

VALID: bytes = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuites name="pytest tests"><testsuite name="pytest" errors="0" '
    b'failures="0" skipped="0" tests="1" time="1">'
    b'<testcase classname="command_case" name="test_configured_provider" time="1" />'
    b"</testsuite></testsuites>"
)


@pytest.mark.parametrize(
    "document",
    [
        b"",
        VALID[:-1],
        b"bad",
        VALID.replace(b'tests="1"', b'tests="2"'),
        VALID.replace(b'failures="0"', b'failures="1"'),
        VALID.replace(b'errors="0"', b'errors="1"'),
        VALID.replace(b'skipped="0"', b'skipped="1"'),
        VALID.replace(b'name="test_configured_provider"', b'name="foreign"'),
        VALID.replace(b'classname="command_case"', b'classname="foreign"'),
        VALID.replace(
            b"</testsuite>",
            b'<testcase classname="command_case" name="test_configured_provider" />'
            b"</testsuite>",
        ),
        VALID.replace(
            b"</testsuites>",
            b'<testsuite errors="0" failures="0" skipped="0" tests="1" /></testsuites>',
        ),
        VALID.replace(
            b" />",
            b'><skipped type="pytest.skip" message="private-marker" /></testcase>',
        ),
        VALID.replace(b" />", b'><error message="private-marker" /></testcase>'),
        VALID.replace(b" />", b'><failure message="private-marker" /></testcase>'),
        VALID.replace(
            b" />",
            b'><properties><property name="result" value="success" />'
            b"</properties></testcase>",
        ),
        b'<!DOCTYPE testsuites [<!ENTITY e "private-marker">]>' + VALID.split(b"?>")[1],
        VALID.replace(b'time="1"', b'time="\xff"'),
        b" " * (MAX_CAPTURE_BYTES + 1) + VALID,
    ],
)
def test_installed_conformance_report_is_bounded(document: bytes) -> None:
    private_report = document

    accepted = accepted_report(private_report, 0)

    assert not accepted


def test_complete_report_requires_no_discarded_bytes() -> None:
    document = VALID

    accepted = accepted_report(document, 0)
    overflow = accepted_report(document, 1)

    assert accepted
    assert not overflow


@pytest.mark.parametrize(
    ("expected", "completed"), [(0, 0), (10, 0), (10, 9), (10, 11)]
)
def test_result_requires_complete_nonempty_execution(
    expected: int, completed: int
) -> None:
    result = ChildResult(
        phase="complete",
        expected=expected,
        completed=completed,
        pytest_status=0,
        wrapper_closed=True,
        report_accepted=True,
    )

    accepted = result.accepted

    assert not accepted


def test_report_pipe_drains_after_its_retention_cap() -> None:
    import os
    from concurrent.futures import ThreadPoolExecutor

    pipe = Pipe.open(max_bytes=64)
    pipe.reader.start()
    payload = b"private-report-marker" * 65536

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            writing = pool.submit(os.write, pipe.write_end, payload)
            assert writing.result(timeout=5) == len(payload)
        pipe.release()

        assert pipe.reader.data == payload[:64]
        assert pipe.reader.discarded == len(payload) - 64
        assert not accepted_report(pipe.reader.data, pipe.reader.discarded)
        assert pipe.read_end == pipe.write_end == -1
    finally:
        pipe.release()
