from dataclasses import dataclass

from kora.executor.launcher import Ending, LaunchSpec, StepLauncher, StepOutcome


@dataclass(frozen=True)
class ToolReply:
    stdout: bytes
    stderr: bytes = b""
    exit_code: int = 0
    ending: Ending = Ending.EXITED


class ScriptedToolLauncher:
    def __init__(self, replies: tuple[ToolReply, ...]) -> None:
        self.replies = replies
        self.calls: list[LaunchSpec] = []

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        reply = self.replies[len(self.calls)]
        self.calls.append(spec)
        return StepOutcome(
            ending=reply.ending,
            exit_code=reply.exit_code if reply.ending is Ending.EXITED else None,
            stdout=reply.stdout[: spec.stdout_limit],
            stdout_overflow=len(reply.stdout) > spec.stdout_limit,
            stderr=reply.stderr[: spec.stderr_limit],
            stderr_dropped=max(0, len(reply.stderr) - spec.stderr_limit),
            peak_rss_bytes=0,
        )


class LingeringLauncher:
    def __init__(self, native: StepLauncher) -> None:
        self.native = native

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        if (
            len(spec.argv) == 7
            and spec.argv[1:4] == ("--system", "get-property", "org.freedesktop.login1")
            and spec.argv[5:] == ("org.freedesktop.login1.User", "Linger")
        ):
            return await ScriptedToolLauncher((ToolReply(b"b false"),)).run(spec)
        return await self.native.run(spec)
