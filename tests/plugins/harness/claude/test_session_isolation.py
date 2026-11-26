import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Final, Literal, get_args

from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.plugins.harness.claude.trust import REVIEWED_PLUGINS, REVIEWED_TOOLS
from kora.protocols.harness import Placement
from tests.plugins.harness.claude.contained import assert_no_outbound, run_contained
from tests.plugins.harness.claude.scripted_model import KEY, mapping, objects

PENDING_LEGS = {
    "trust-repo-config-true": (
        "explicit repository-trust implementation (first-slice.md)"
    ),
    "node-mcp-name-collision": (
        "explicit repository-trust implementation (first-slice.md)"
    ),
    "claudeai-login-server": "work item 21 manual experiment",
    "launcher-digest-record": "#199 / #1584 persisted digest evidence",
    "reserved-env-ir026-including-https-proxy-node-options": (
        "#199 / #1584 consuming validation"
    ),
    "output-schema-argument-limit-launch-rejected": "#199 / #1584 consuming validation",
}
REPOSITORY = Path(__file__).resolve().parents[4]
CANARY = "KORA_REPOSITORY_INSTRUCTION_CANARY_1645"
ENV_CANARY = "KORA_SETTINGS_ENV_VALUE_1645"
PLATFORM_ENVIRONMENT: Final[frozenset[str]] = (
    frozenset({"__CF_USER_TEXT_ENCODING"}) if sys.platform == "darwin" else frozenset()
)


def run_scenario(
    root: Path,
    workspace: Path,
    account: Path,
    markers: Path,
    calls: list[tuple[str, dict[str, object]]],
    *,
    contained: bool = sys.platform == "linux",
    denied_tool: str = "Forbidden",
    policy: Literal["conformance", "raising", "allow_deny"] = "conformance",
    cancel_marker: str | None = None,
    permission_mode: str = "dont_ask",
    subagent_calls: list[tuple[str, dict[str, object]]] | None = None,
    layers: dict[str, object] | None = None,
    protected_paths: tuple[str, ...] = (),
) -> dict[str, object]:
    config = root / "scenario.json"
    report = root / "result.json"
    config.write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "markers": str(markers),
                "calls": calls,
                "report": str(report),
                "contained": contained,
                "denied_tool": denied_tool,
                "policy": policy,
                "cancel_marker": cancel_marker,
                "permission_mode": permission_mode,
                "subagent_calls": subagent_calls or [],
                "layers": layers or {},
                "protected_paths": protected_paths,
            }
        )
    )
    argv = [
        sys.executable,
        "-m",
        "tests.plugins.harness.claude.isolation_scenario",
        str(config),
    ]
    environment = {
        "HOME": str(account),
        "PATH": os.environ["PATH"],
        "KORA_SUPERVISOR_MARKER": "supervisor-only-1645",
        "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
    }
    if contained:
        completed = run_contained(
            argv, writable=root, environment=environment, cwd=REPOSITORY, timeout=120
        )
    else:
        with (root / "supervisor-descriptor").open("w") as marker:
            os.set_inheritable(marker.fileno(), True)
            completed = subprocess.run(
                argv,
                cwd=REPOSITORY,
                env=environment,
                pass_fds=(marker.fileno(),),
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
    assert completed.returncode == 0, completed.stderr
    result = mapping(json.loads(report.read_text()))
    assert result is not None
    assert result["leftover"] == []
    return result


def hooks(marker: Path) -> dict[str, object]:
    return {
        "hooks": {
            name: [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "touch " + shlex.quote(str(marker)),
                        }
                    ],
                }
            ]
            for name in (
                "SessionStart",
                "UserPromptSubmit",
                "PreToolUse",
                "PostToolUse",
            )
        }
    }


def seed_configuration(root: Path, markers: Path, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text(CANARY + "-" + name)
    settings = hooks(markers / name) | {
        "permissions": {"deny": ["Write(denied.txt)", "Write"]},
        "env": {"KORA_SETTINGS_CANARY": ENV_CANARY},
    }
    (root / "settings.json").write_text(json.dumps(settings))


def test_claude_session_ignores_repository_settings(tmp_path: Path) -> None:
    workspace, account, markers = (
        tmp_path / name for name in ("workspace", "account", "markers")
    )
    for path in (workspace, account, markers):
        path.mkdir()
    (workspace / "CLAUDE.md").write_text(CANARY)
    seed_configuration(workspace / ".claude", markers, "repository")
    seed_configuration(account / ".claude", markers, "account")
    seed_configuration(workspace / ".kora" / "claude", markers, "preseed-config")
    seed_configuration(
        workspace / ".kora" / "home" / ".claude", markers, "preseed-home"
    )
    (workspace / "account-config").symlink_to(
        account / ".claude", target_is_directory=True
    )
    mcp = {
        "mcpServers": {
            "canary-server": {
                "command": "/bin/sh",
                "args": ["-c", "touch " + shlex.quote(str(markers / "mcp"))],
            }
        }
    }
    (workspace / ".mcp.json").write_text(json.dumps(mcp))
    (account / ".claude.json").write_text(json.dumps(mcp))
    for profile in (".bash_profile", ".bashrc", ".profile"):
        content = (
            "export KORA_ACCOUNT_PROFILE_CANARY=1\ntouch "
            + shlex.quote(str(markers / profile))
            + "\n"
        )
        (account / profile).write_text(content)
        (workspace / ".kora" / "home" / profile).write_text(content)
    for relative, text in (
        (
            "skills/canary-skill/SKILL.md",
            "---\nname: canary-skill\ndescription: canary skill\n---\n" + CANARY,
        ),
        (
            "agents/canary-agent.md",
            "---\nname: canary-agent\ndescription: canary agent\n---\n" + CANARY,
        ),
        ("commands/canary-cmd.md", CANARY),
    ):
        target = workspace / ".claude" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    repository_settings = (workspace / ".claude/settings.json").read_text()
    rewritten = json.dumps(hooks(markers / "rewritten-hook"))
    calls: list[tuple[str, dict[str, object]]] = [
        ("Write", {"file_path": str(workspace / "denied.txt"), "content": "written"}),
        (
            "Bash",
            {
                "command": (
                    'printf \'%s\\n\' "SHELL=$SHELL" "HOME=$HOME" '
                    '"PROFILE=${KORA_ACCOUNT_PROFILE_CANARY-}" '
                    '"SETTINGS=${KORA_SETTINGS_CANARY-}"'
                )
            },
        ),
        (
            "Write",
            {
                "file_path": str(workspace / ".claude/settings.json"),
                "content": rewritten,
            },
        ),
        (
            "Bash",
            {
                "command": "printf '%s' "
                + shlex.quote(rewritten)
                + " > "
                + shlex.quote(str(workspace / ".claude/settings.json"))
            },
        ),
        (
            "Bash",
            {
                "command": "printf '%s' "
                + shlex.quote(rewritten)
                + ' > "$CLAUDE_CONFIG_DIR/settings.json"'
            },
        ),
        ("Bash", {"command": "true"}),
    ]

    report = run_scenario(tmp_path, workspace, account, markers, calls)

    assert_no_outbound(report)
    assert report["cause"] == "completed"
    assert report["error_class"] is None
    assert (workspace / "denied.txt").read_text() == "written"
    assert report["markers"] == []
    assert not list(markers.iterdir())
    placements = mapping(report["placements"])
    assert placements is not None
    events = objects(report["events"])
    assert events is not None
    results = [
        mapping(event.get("data")) or {}
        for value in events
        if (event := mapping(value)) is not None and event.get("kind") == "tool_result"
    ]
    assert len(results) == len(calls)
    assert results[1]["content"] == (
        f"SHELL=/bin/bash\nHOME={placements['harness_home']}\nPROFILE=\nSETTINGS="
    )
    assert [result["is_error"] for result in results] == [
        False,
        False,
        True,
        True,
        False,
        False,
    ]
    denied = [
        mapping(event.get("data")) or {}
        for value in events
        if (event := mapping(value)) is not None
        and event.get("kind") == "permission_denied"
    ]
    assert [(entry["tool_use_id"], entry["source"]) for entry in denied] == [
        ("toolu_scripted_2", "harness"),
        ("toolu_scripted_3", "harness"),
    ]
    assert (workspace / ".claude/settings.json").read_text() == repository_settings
    assert report["config_settings"] == rewritten
    requests = json.dumps(report["requests"])
    for canary in (CANARY, ENV_CANARY, "canary-skill", "canary-agent", "canary-cmd"):
        assert canary not in requests
    init = mapping(report["init"])
    assert init is not None
    plugins = objects(init["plugins"])
    assert plugins is not None
    assert {(mapping(plugin) or {})["name"] for plugin in plugins} == REVIEWED_PLUGINS
    tools = objects(init["tools"])
    assert tools is not None
    assert {
        name for name in tools if isinstance(name, str) and not name.startswith("mcp__")
    } <= REVIEWED_TOOLS
    assert init["mcp_servers"] == []
    assert "canary-" not in json.dumps(init)
    assert report["clients"]
    assert set(objects(report["clients"]) or []) == {"127.0.0.1"}


def test_claude_session_receives_only_constructed_environment(tmp_path: Path) -> None:
    workspace, account, markers = (
        tmp_path / name for name in ("workspace", "account", "markers")
    )
    for path in (workspace, account, markers):
        path.mkdir()

    report = run_scenario(
        tmp_path,
        workspace,
        account,
        markers,
        [("Bash", {"command": "true"})],
        contained=False,
    )

    assert report["cause"] == "completed"
    assert report["error_class"] is None
    recorder = mapping(report["recorder"])
    placements = mapping(report["placements"])
    assert recorder is not None
    assert placements is not None
    paths = {name: Path(str(placements[name])) for name in get_args(Placement)}
    expected = ClaudeEnvironmentConfiguration(
        binding="claude-test", bash_path="/bin/bash", max_retries=0, cli_telemetry=False
    ).launch_inputs(cwd=workspace, placements=paths)
    observed = mapping(recorder["environment"]) or {}
    assert {
        name: value
        for name, value in observed.items()
        if name not in PLATFORM_ENVIRONMENT
    } == dict(expected.environment) | {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "ANTHROPIC_API_KEY": KEY,
        "ANTHROPIC_BASE_URL": report["base_url"],
    }
    assert "KORA_SUPERVISOR_MARKER" not in observed
    assert recorder["descriptors"] == [0, 1, 2]
    stderr = os.fstat(2)
    assert recorder["stderr"] != [stderr.st_dev, stderr.st_ino]
    assert recorder["stderr"] != report["child_stderr"]
    argv = objects(recorder["argv"])
    assert argv is not None
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--setting-sources=" in argv
    assert "--strict-mcp-config" in argv
    assert "--system-prompt" not in argv
    assert "--append-system-prompt" not in argv
    assert KEY not in json.dumps(argv)
    assert report["control_socket"] not in argv
    assert report["report_path"] not in argv
    assert report["starts"] == 1
    assert report["probes"] == 0
