from kora.artifacts import VcsRef

from kora import approval, graph, node


@graph(
    namespace="example",
    repo="/operator-owned/demo.git",
    rev="1111111111111111111111111111111111111111",
    runtime="local",
    vcs="git",
)
def live_claude_local_git() -> None:
    prepare = node.agent(
        "agent",
        harness="claude",
        prompt=(
            "Write exactly one line containing kora live demo followed by a newline "
            "to demo.txt. Leave the change in the worktree for kora to publish. "
            "Do not edit other files, commit, or push."
        ),
        output={"change": VcsRef},
        trust_repo_config=False,
    )
    check = node.command(
        "gate",
        cmd=[
            "/operator-owned/bin/python",
            "-I",
            "-c",
            """import os
import stat
import subprocess
import sys
from pathlib import Path

from kora.ir.vcs_ref import VcsRefValidationError, parse_vcs_ref_json
from kora.ir.vcs_ref_json import MAX_JSON_BYTES

LINE_END = bytes((10,))
MARKER = b"kora live demo" + LINE_END
GIT_ENV = dict(
    GIT_ALLOW_PROTOCOL="",
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_NOSYSTEM="1",
    GIT_NO_LAZY_FETCH="1",
    GIT_TERMINAL_PROMPT="0",
    LC_ALL="C",
)


def read_commit(input_path: str) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        with os.fdopen(os.open(input_path, flags), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise SystemExit("invalid VcsRef input file")
            reference = parse_vcs_ref_json(source.read(MAX_JSON_BYTES + 1))
    except (OSError, VcsRefValidationError):
        raise SystemExit("invalid VcsRef input") from None
    commit = reference.commit
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise SystemExit("invalid Git commit ID")
    return commit


def read_git(executable: str, limit: int, *arguments: str) -> bytes:
    try:
        process = subprocess.Popen(
            (executable, "--no-replace-objects", "--git-dir=.git", *arguments),
            env=GIT_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise SystemExit("Git executable unavailable") from None
    with process:
        output = process.stdout
        if output is None:
            raise RuntimeError("Git output pipe unavailable")
        data = output.read(limit + 1)
        if len(data) > limit:
            process.kill()
            raise SystemExit("Git output exceeds verifier limit")
        if process.wait() != 0:
            raise SystemExit("Git verification query failed")
    return data


def verify(executable: str, input_path: str) -> None:
    if not Path(executable).is_absolute():
        raise SystemExit("Git executable must be absolute")
    commit = read_commit(input_path)
    head = read_git(executable, 41, "rev-parse", "--verify", "HEAD^0")
    if head != commit.encode("ascii") + LINE_END:
        sys.stderr.write("commit mismatch" + chr(10))
        raise SystemExit(3)
    tree_entry = b"100644 blob" + LINE_END
    entry = read_git(
        executable,
        len(tree_entry),
        "ls-tree",
        "--format=%(objectmode) %(objecttype)",
        commit,
        "--",
        "demo.txt",
    )
    if entry != tree_entry:
        raise SystemExit("demo.txt must be a regular non-executable file")
    marker_object = commit + ":demo.txt"
    size_line = str(len(MARKER)).encode("ascii") + LINE_END
    if read_git(executable, len(size_line), "cat-file", "-s", """
            """marker_object) != size_line:
        raise SystemExit("demo.txt size mismatch")
    if read_git(executable, len(MARKER), "cat-file", "blob", marker_object) != MARKER:
        raise SystemExit("demo.txt content mismatch")
    sys.stdout.write("verified_commit=" + commit + chr(10))


verify(sys.argv[1], sys.argv[2])
""",
            "/operator-owned/bin/git",
            "{input_root}/change.json",
        ],
        workspace=prepare.change,
    )(change=prepare.change)
    prepare >> check
    approval(
        "approve",
        channel="cli",
        timeout="1h",
        on_expiry="fail",
        binds={"vcs_ref": "inputs.change", "gate_digests": ["inputs.gate"]},
    )(change=prepare.change, gate=check)
