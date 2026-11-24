import os
from pathlib import Path

from tests.support.provider_tooling import (
    copy_core_wheel,
    create_virtual_environment,
    prepare_wheelhouse,
    run_setup,
)

CHECK = """
import sys
from importlib.metadata import PackageNotFoundError, version

from kora.plugins.vcs.git import provider_metadata

assert provider_metadata().name == "git"
assert "jwt" not in sys.modules
assert "cryptography" not in sys.modules

if sys.argv[1] == "github-app":
    from kora.plugins.vcs.git.github_app import GitHubAppIssuer

    assert callable(GitHubAppIssuer)
    assert version("pyjwt") == "2.15.1"
    assert version("cryptography") == "50.0.2"
else:
    for distribution in ("pyjwt", "cryptography"):
        try:
            version(distribution)
        except PackageNotFoundError:
            continue
        raise AssertionError("the base wheel installed an App dependency")
"""


def test_github_app_wheel_extra_keeps_the_base_git_provider_independent(
    core_wheel: Path, tmp_path: Path
) -> None:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    prepare_wheelhouse(wheelhouse, github_app=True)
    copy_core_wheel(core_wheel, wheelhouse)
    profiles = {
        "base": create_virtual_environment(tmp_path / "base"),
        "github-app": create_virtual_environment(tmp_path / "github-app"),
    }

    for profile, python in profiles.items():
        requirement = "kora[github-app]" if profile == "github-app" else "kora"
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
                requirement,
            ]
        )

        run_setup(
            [str(python), "-I", "-c", CHECK, profile],
            environment={"PATH": os.defpath},
        )
