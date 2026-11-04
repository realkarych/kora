import hashlib
import os
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from kora.db.graph_value_json import TextBudget
from kora.engine.graph_native_sources import agent_session_checker
from kora.ir.agent_session import agent_session_revision_descriptor
from kora.ir.agent_session_view import (
    POLICY_BYTES,
    SCHEMA_BYTES,
    VIEW_BYTES,
    AgentSessionViewUnavailable,
    published_object,
)
from tests.engine.graph_native_cases import native_arguments
from tests.engine.graph_output_cases import child
from tests.support.provider_tooling import (
    copy_core_wheel,
    create_virtual_environment,
    prepare_wheelhouse,
    run_setup,
)

REPOSITORY: Path = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "mode", ["valid", "missing", "directory", "short", "large", "changed"]
)
def test_native_source_refusal_is_bounded_and_closes_handle(
    tmp_path: Path, mode: str
) -> None:
    published = (
        REPOSITORY / "knowledge/specs/artifacts/AgentSession.cel-policy.json"
    ).read_bytes()
    resource = tmp_path / "private-source"
    if mode == "directory":
        resource.mkdir()
    elif mode != "missing":
        resource.write_bytes(
            published
            if mode == "valid"
            else published[:-1]
            if mode == "short"
            else b"private" * 200000
            if mode == "large"
            else published.replace(
                b"kora.agent-session-view.v1", b"kora.agent-session-view.v2"
            )
        )
    digest = hashlib.sha256(published).hexdigest()
    before = frozenset(os.listdir("/dev/fd"))

    if mode == "valid":
        result = published_object(resource, POLICY_BYTES, digest)
        assert result["identity"] == "kora.agent-session-view.v1"
    else:
        with pytest.raises(AgentSessionViewUnavailable) as failure:
            published_object(resource, POLICY_BYTES, digest)
        assert str(failure.value) == "published AgentSession view unavailable"
        assert failure.value.__cause__ is None
        assert failure.value.__suppress_context__ or failure.value.__context__ is None
        if mode == "large":
            acquired: list[int] = []
            traceback = failure.value.__traceback__
            while traceback is not None:
                if traceback.tb_frame.f_code.co_name == "published_object":
                    raw: object = traceback.tb_frame.f_locals["raw"]
                    assert isinstance(raw, bytes)
                    acquired.append(len(raw))
                traceback = traceback.tb_next
            assert acquired == [POLICY_BYTES + 1]

    assert frozenset(os.listdir("/dev/fd")) == before


def test_native_view_uses_same_published_bytes_in_core_wheel(core_wheel: Path) -> None:
    names = ("AgentSession.cel-policy.json", "AgentSession.cel.schema.json")
    expected = {
        name: (REPOSITORY / "knowledge/specs/artifacts" / name).read_bytes()
        for name in names
    }
    descriptor = agent_session_revision_descriptor()

    actual = agent_session_checker(TextBudget(VIEW_BYTES))
    with ZipFile(core_wheel) as archive:
        packaged = {
            name: archive.read("kora/ir/artifact_schemas/" + name) for name in names
        }

    assert packaged == expected
    assert list(map(len, expected.values())) == [POLICY_BYTES, SCHEMA_BYTES]
    assert (
        hashlib.sha256(expected[names[0]]).hexdigest()
        == descriptor["cel_policy_sha256"]
    )
    assert (
        hashlib.sha256(expected[names[1]]).hexdigest()
        == child(actual, "policy")["cel_schema_sha256"]
    )


def test_native_output_works_from_installed_core_wheel(
    core_wheel: Path, tmp_path: Path
) -> None:
    wheelhouse = tmp_path / "dependencies"
    wheelhouse.mkdir()
    prepare_wheelhouse(wheelhouse)
    wheel = copy_core_wheel(core_wheel, wheelhouse)
    python = create_virtual_environment(tmp_path / "environment")
    run_setup(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--python",
            str(python),
            str(wheel),
        ]
    )
    arguments = native_arguments("AgentSession")
    program = "\n".join(
        [
            "import sys",
            "import kora",
            "from pathlib import Path",
            "from importlib.resources import files",
            "from kora.db.graph_value_json import GraphValueRejected, ObjectReader",
            "from kora.engine.graph_native_outputs import "
            "build_graph_native_output_contract, read_graph_native_output_contract",
            f"assert Path(kora.__file__).is_relative_to({str(tmp_path)!r})",
            f"arguments = {arguments!r}",
            "built = build_graph_native_output_contract("
            "*arguments, max_encoded_bytes=1000000)",
            "saved = ObjectReader(built.text).object()",
            "checked = read_graph_native_output_contract("
            "*arguments, saved, built.digest, max_encoded_bytes=1000000)",
            "assert checked == built",
            "assert 'claude_agent_sdk' not in sys.modules",
            "assert 'cel' not in sys.modules",
            "resource = files('kora.ir').joinpath("
            "'artifact_schemas', 'AgentSession.cel-policy.json')",
            "original = resource.read_bytes()",
            "resource.write_bytes(b'x' + original[1:])",
            "try:",
            "    build_graph_native_output_contract("
            "*arguments, max_encoded_bytes=1000000)",
            "except GraphValueRejected as error:",
            "    assert str(error) == 'invalid graph value representation'",
            "else:",
            "    raise AssertionError('changed package source accepted')",
        ]
    )

    result = subprocess.run(
        [str(python), "-I", "-c", program],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b""
