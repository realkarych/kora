import json
import shutil
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import get_args

from kora.plugins.harness.claude.integrity import trusted_binary
from kora.protocols.harness import Launch, LaunchInputs, Placement
from tests.plugins.harness.claude.conformance_launch import ClaudeLaunch
from tests.plugins.harness.claude.scripted_model import KEY

RECORDER = """import json
import os
import sys
from pathlib import Path
fds = []
for name in os.listdir("/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"):
    try:
        os.fstat(int(name))
    except OSError:
        continue
    fds.append(int(name))
stderr = os.fstat(2)
Path(sys.argv[1]).write_text(json.dumps({
    "environment": dict(os.environ), "argv": sys.argv[2:],
    "descriptors": sorted(fds),
    "stderr": [stderr.st_dev, stderr.st_ino],
}))
os.execv(sys.argv[2], sys.argv[2:])
"""


class ScriptedLaunches:
    def __init__(self, root: Path, workspace: Path, base_url: str) -> None:
        self.root = root
        self.workspace = workspace
        self.base_url = base_url
        self.placements: Mapping[Placement, Path] = {
            name: root / name for name in get_args(Placement)
        }
        for path in self.placements.values():
            path.mkdir()
        self.launch: ClaudeLaunch | None = None
        self.inputs: LaunchInputs | None = None
        self.report_path = self.placements["harness_input"] / "report.json"
        self.starts = self.placements["harness_input"] / "starts"
        self.probes = self.placements["harness_input"] / "probes"
        self.stream = self.placements["harness_input"] / "stream.ndjson"

    async def allocate(self, inputs: LaunchInputs) -> Launch:
        assert self.launch is None
        self.inputs = inputs
        binary = trusted_binary().path
        directory = self.placements["harness_input"]
        recorder = directory / "recorder.py"
        recorder.write_text(RECORDER)
        environment = dict(inputs.environment) | {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "ANTHROPIC_API_KEY": KEY,
            "ANTHROPIC_BASE_URL": self.base_url,
        }
        endpoint = self.root / "s"
        wrapper = directory / "launch"
        wrapper.write_text(f"""#!{sys.executable} -I
import os
import socket
import subprocess
import sys
from pathlib import Path
if sys.argv[1:] == ["-v"]:
    with Path({str(self.probes)!r}).open("a") as probes:
        probes.write("probe\\n")
    raise SystemExit(0)
with Path({str(self.starts)!r}).open("a") as starts:
    starts.write("start\\n")
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.connect({str(endpoint)!r})
os.setsid()
connection.sendall((str(os.getpid()) + "\\n").encode())
child = subprocess.Popen(
    [{sys.executable!r}, "-I", {str(recorder)!r}, {str(self.report_path)!r},
     {str(binary)!r}, *sys.argv[1:]],
    env={environment!r}, cwd={str(inputs.cwd)!r}, close_fds=True,
    stdout=subprocess.PIPE,
)
with Path({str(self.stream)!r}).open("wb") as stream:
    for line in child.stdout:
        stream.write(line)
        stream.flush()
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
status = child.wait()
report = f"exit {{status}}\\n" if status >= 0 else f"signal {{-status}}\\n"
connection.sendall(report.encode())
connection.close()
if status < 0:
    os.kill(os.getpid(), -status)
raise SystemExit(status)
""")
        wrapper.chmod(0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(endpoint))
            listener.listen(1)
            listener.setblocking(False)
            self.launch = ClaudeLaunch(wrapper, listener)
            return self.launch
        finally:
            if self.launch is None:
                listener.close()

    def recorder_report(self) -> object:
        value: object = json.loads(self.report_path.read_text())
        return value

    async def close(self) -> None:
        if self.launch is not None:
            await self.launch.close()
        shutil.rmtree(self.root)
