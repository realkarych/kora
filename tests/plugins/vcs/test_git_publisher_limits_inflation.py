import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import Kept, pinned, publish_request, run
from kora.executor.failures import ExecutorError
from kora.ir.node import ErrorClass
from tests.plugins.vcs.git_support import World, git, make_world

DECLARED_RESULT: int = 3 * 1024**3
REF_DELTA: int = 7
FAKE_HEAD: str = "2" * 40


def varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def entry_header(kind: int, size: int) -> bytes:
    first = (kind << 4) | (size & 0x0F)
    size >>= 4
    encoded = bytearray()
    while size:
        encoded.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    encoded.append(first)
    return bytes(encoded)


def inflating_pack(base_blob: str, base_size: int) -> bytes:
    delta = varint(base_size) + varint(DECLARED_RESULT) + b"\x01x"
    body = (
        entry_header(REF_DELTA, len(delta))
        + bytes.fromhex(base_blob)
        + zlib.compress(delta)
    )
    pack = b"PACK" + struct.pack(">II", 2, 1) + body
    return pack + hashlib.sha1(pack, usedforsecurity=False).digest()


def forged_bundle(world: World, bundle: Path) -> None:
    blob = git(
        world.home, f"--git-dir={world.remote}", "rev-parse", f"{world.base}:a.txt"
    )
    size = int(git(world.home, f"--git-dir={world.remote}", "cat-file", "-s", blob))
    header = f"# v2 git bundle\n-{world.base} base\n{FAKE_HEAD} refs/heads/forged\n\n"
    bundle.write_bytes(header.encode("ascii") + inflating_pack(blob, size))


def test_git_publisher_limits_inflation(tmp_path: Path, kora_capture: Capture) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    context = world.context()
    remote = world.remote_fixture()
    plan = run(world.provider.publish_plan(publish_request(remote, checkout), context))
    workspace = checkout.workspace()
    exported = run(world.executor.export(plan, workspace, checkout.areas, context))
    export = world.candidate(checkout) / ".kora" / "export"
    forged_bundle(world, export / "head.bundle")
    (export / "head.json").write_text(json.dumps({"head": FAKE_HEAD, "bundle": True}))
    sealed = run(world.executor.seal(exported, workspace, checkout.areas, context))

    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.prepare_and_inspect(
                plan,
                sealed,
                workspace,
                publish_request(remote, checkout),
                pinned(world.binding()),
                checkout.areas,
                (),
                (),
                Kept(),
                context,
            )
        )

    limited = [step.argv[0] for step in plan.prepare[2:5]]
    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID
    assert limited == (
        ["prlimit"] * 3 if sys.platform == "linux" else ["python", "python", "git"]
    )
