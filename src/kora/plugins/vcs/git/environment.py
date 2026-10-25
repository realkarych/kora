from collections.abc import Sequence
from pathlib import Path
from typing import Final

EMPTY_TREE: Final[str] = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
Option = tuple[str, str]
NO_DETACH: Final[tuple[Option, ...]] = (
    ("gc.auto", "0"),
    ("gc.autoDetach", "false"),
    ("maintenance.auto", "false"),
    ("maintenance.autoDetach", "false"),
    ("fetch.writeCommitGraph", "false"),
)
CLIENT: Final[tuple[Option, ...]] = (
    ("core.fsmonitor", "false"),
    ("protocol.allow", "never"),
    ("protocol.file.allow", "always"),
    ("credential.helper", ""),
    ("core.sshCommand", "false"),
    ("init.defaultBranch", "kora"),
    ("advice.detachedHead", "false"),
)
PUBLISHER: Final[tuple[Option, ...]] = (
    ("core.attributesFile", "/dev/null"),
    ("attr.tree", EMPTY_TREE),
    ("core.protectHFS", "true"),
    ("core.protectNTFS", "true"),
    ("transfer.fsckObjects", "true"),
    ("fetch.fsckObjects", "true"),
    ("receive.fsckObjects", "true"),
    ("pack.threads", "2"),
    ("core.packedGitWindowSize", "32m"),
    ("core.packedGitLimit", "256m"),
    ("core.bigFileThreshold", "512m"),
    ("diff.noprefix", "false"),
)


def trusted_options(hooks: str, safe: Sequence[str]) -> tuple[Option, ...]:
    return (
        *NO_DETACH,
        *CLIENT,
        ("core.hooksPath", hooks),
        *(("safe.directory", directory) for directory in safe),
    )


def publisher_options(hooks: str, safe: Sequence[str]) -> tuple[Option, ...]:
    return (*trusted_options(hooks, safe), *PUBLISHER)


def push_options() -> tuple[Option, ...]:
    return (*NO_DETACH, *CLIENT)


def export_options() -> tuple[Option, ...]:
    return NO_DETACH


def git_environment(git: str, home: str, options: Sequence[Option]) -> dict[str, str]:
    environment = {
        "PATH": str(Path(git).parent),
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
        "GIT_CONFIG_COUNT": str(len(options)),
    }
    for index, (key, value) in enumerate(options):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment
