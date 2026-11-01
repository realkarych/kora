import asyncio
import json
import os
import sys
from pathlib import Path


def record() -> None:
    expected: object = json.loads(sys.stdin.buffer.read())
    if dict(os.environ) != expected:
        raise RuntimeError("candidate environment values differ")
    if os.getcwd() != os.environ.get("PWD"):
        raise RuntimeError("candidate working directory differs")
    sys.stdout.write("candidate environment verified\n")


async def assemble(cwd: Path) -> None:
    from kora.conformance.capture import captured
    from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now
    from kora.executor.launcher import Ending, LaunchSpec, ProcessLauncher
    from kora.observability import Level, LoggingSettings
    from kora.plugins.runtime.local.environment import candidate_environment

    recorder_settings = {"LC_ALL": "C"}
    if "__CF_USER_TEXT_ENCODING" in os.environ:
        recorder_settings["__CF_USER_TEXT_ENCODING"] = os.environ[
            "__CF_USER_TEXT_ENCODING"
        ]
    expected = {
        "PATH": "/usr/bin:/bin",
        "PWD": str(cwd),
        "KORA_ATTEMPT_TOKEN": bytes(range(16)).hex(),
        "WORKLOAD_TEXT": "text-π",
        "CASE_NAME": "upper-case-name",
        "EMPTY": "",
        "TOOL_SECRET": "tool-secret-canary",
        "HARNESS_KEY": "harness-secret-canary",
        "ADAPTER_TEXT": "adapter-value",
        "case_name": "lower-case-name",
        **recorder_settings,
    }
    workload = {"WORKLOAD_TEXT": "text-π", "EMPTY": "", "CASE_NAME": "upper-case-name"}
    adapter = {
        "ADAPTER_TEXT": "adapter-value",
        "case_name": "lower-case-name",
        **recorder_settings,
    }
    with captured(
        CONFORMANCE_COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "candidate-environment-probe",
        utc_now,
    ) as capture:
        secrets = {"TOOL_SECRET": capture.scope.admit("tool-secret-canary")}
        credentials = {"HARNESS_KEY": capture.scope.admit("harness-secret-canary")}
        token = capture.scope.admit(bytes(range(16)).hex())
        registrations = capture.scope.registered_count()

        environment = candidate_environment(
            cwd=cwd,
            attempt_token=token,
            workload=workload,
            secrets=secrets,
            harness_credentials=credentials,
            adapter=adapter,
        )
        workload["WORKLOAD_TEXT"] = "changed"
        adapter["ADAPTER_TEXT"] = "changed"
        secrets.clear()
        credentials.clear()
        outcome = await ProcessLauncher().run(
            LaunchSpec(
                argv=(sys.executable, "-I", str(Path(__file__)), "record"),
                cwd=cwd,
                env=environment,
                stdin=json.dumps(expected).encode(),
                timeout_ns=30_000_000_000,
                stdout_limit=1024,
                stderr_limit=4096,
            )
        )

        checks = {
            "exit": outcome.ending is Ending.EXITED and outcome.exit_code == 0,
            "stdout": outcome.stdout == b"candidate environment verified\n",
            "stderr": outcome.stderr == b"",
            "bounded": not outcome.stdout_overflow and not outcome.stderr_dropped,
            "scope": not capture.scope.released,
            "registrations": capture.scope.registered_count() == registrations,
        }
        failed = tuple(name for name, passed in checks.items() if not passed)
        if failed:
            raise RuntimeError(f"candidate environment probe failed: {failed}")
        environment.clear()
    sys.stdout.write("candidate environment verified\n")


def main() -> None:
    if sys.argv[1] == "record":
        record()
    elif sys.argv[1] == "assemble":
        asyncio.run(assemble(Path(sys.argv[2])))
    else:
        raise ValueError("unknown candidate environment probe")


if __name__ == "__main__":
    main()
