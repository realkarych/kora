import asyncio
import os
import re
import shutil
import subprocess
import sysconfig
import tarfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from kora.db.connection import DATABASE_URL_VARIABLE, DatabaseSettings
from kora.db.identifiers import is_identifier
from tests.api.test_packaging import installed_core
from tests.conftest import SELECTED_DATABASES
from tests.db.predecessor import (
    PredecessorInstallation,
    PredecessorProducer,
    PredecessorWrite,
)
from tests.db.test_db_upgrade_process import DEBUG_ENVIRONMENT
from tests.support.databases import DatabaseKind
from tests.support.observability import ProcessResult
from tests.support.provider_tooling import (
    REPOSITORY,
    build_core_wheel,
    core_wheel_workspace,
)

PREDECESSOR: Path = REPOSITORY / "tests/db/predecessor_revision.txt"


def archive_predecessor(root: Path) -> Path:
    revision = PREDECESSOR.read_text(encoding="ascii").strip()
    if re.fullmatch("[0-9a-f]{40}", revision) is None:
        raise ValueError("The predecessor artifact requires an exact Git revision.")
    git = shutil.which("git")
    if git is None:
        raise ValueError("The predecessor artifact requires git.")
    probe = subprocess.run(
        [git, "cat-file", "-t", revision],
        cwd=REPOSITORY,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if (probe.returncode, probe.stdout) != (0, b"commit\n"):
        raise ValueError(
            "The offline predecessor fixture requires the staged Git commit."
        )
    archive = root / "predecessor.tar"
    subprocess.run(
        [git, "archive", "--format=tar", f"--output={archive}", revision],
        cwd=REPOSITORY,
        timeout=15,
        check=True,
    )
    source = root / "source"
    source.mkdir()
    with tarfile.open(archive, "r:") as payload:
        payload.extractall(source, filter="data")
    return source


@pytest.fixture(scope="session")
def predecessor_installation(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PredecessorInstallation]:
    with core_wheel_workspace() as root:
        source = archive_predecessor(root)
        wheel = build_core_wheel(root, source=source)
        python = installed_core(
            wheel,
            tmp_path_factory.mktemp("run-event-position-predecessor"),
            postgres=DatabaseKind.POSTGRESQL in SELECTED_DATABASES,
        )
        yield PredecessorInstallation(python, source, wheel)


@pytest.fixture(scope="session")
def predecessor_db_upgrade(
    predecessor_installation: PredecessorInstallation,
) -> Callable[[Mapping[str, str]], ProcessResult]:
    python = predecessor_installation.python

    def upgrade(environment: Mapping[str, str]) -> ProcessResult:
        completed = subprocess.run(
            [str(python), "-I", "-m", "kora", "db", "upgrade"],
            env={"PATH": os.environ["PATH"], **DEBUG_ENVIRONMENT, **environment},
            capture_output=True,
            timeout=60,
            check=False,
        )
        return ProcessResult(completed.returncode, completed.stdout, completed.stderr)

    return upgrade


@pytest.fixture(scope="session")
def predecessor_write(
    predecessor_installation: PredecessorInstallation,
) -> PredecessorWrite:
    installation = predecessor_installation
    probe = Path(__file__).with_name("predecessor_writer.py")

    async def run(settings: DatabaseSettings, producer: PredecessorProducer) -> str:
        completed = await asyncio.to_thread(
            subprocess.run,
            [
                str(installation.python),
                "-I",
                str(probe),
                str(installation.source),
                sysconfig.get_path("purelib"),
                producer,
            ],
            env={
                "PATH": os.environ["PATH"],
                DATABASE_URL_VARIABLE: settings.url.render_as_string(
                    hide_password=False
                ),
            },
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, "The installed historical writer failed."
        saved_id = completed.stdout.decode("ascii")
        assert is_identifier(saved_id)
        return saved_id

    return run
