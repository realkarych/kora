import os
import selectors
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.conformance.vcs import run
from kora.executor.entry import Executor
from kora.executor.failures import ExecutorError
from kora.executor.launcher import ProcessLauncher
from kora.ir import VcsRef
from kora.ir.node import ErrorClass
from kora.observability import Level, LoggingSettings
from kora.plugins.vcs.fake import DirVcs, store
from kora.protocols.errors import KoraProtocolError
from kora.protocols.vcs import CheckoutRequest, CredentialRequest
from tests.plugins.vcs.dirvcs_checkout_support import (
    SEED_FILES,
    configuration,
    empty_store,
    seeded_store,
    set_head,
    stored_revision,
)
from tests.plugins.vcs.git_support import REPO, make_world


@pytest.mark.parametrize(
    "selection",
    ["revision", "reference", "missing", "missing_base", "blob_suffix", "symlink_size"],
)
def test_directory_checkout_runs_through_the_real_executor(
    tmp_path: Path, kora_capture: Capture, selection: str
) -> None:
    remote, base = seeded_store(tmp_path)
    changed = {**SEED_FILES, "new.txt": (store.REGULAR, b"new\n")}
    head = stored_revision(remote, changed, base, "next")
    if selection == "blob_suffix":
        (remote / "blobs" / store.digest_of(b"alpha\n")).write_bytes(b"alpha\nextra")
    if selection == "symlink_size":
        revision_data = store.revision_of((remote / "revisions" / head).read_bytes())
        entries = store.tree_of((remote / "trees" / revision_data.tree).read_bytes())
        tree_data = store.tree_document(
            [
                entry._replace(size=1) if entry.path == "link" else entry
                for entry in entries
            ]
        )
        tree = store.digest_of(tree_data)
        (remote / "trees" / tree).write_bytes(tree_data)
        document = store.revision_document(base, "invalid symlink size", tree)
        head = store.digest_of(document)
        (remote / "revisions" / head).write_bytes(document)
    set_head(remote, "topic", head)
    gone = tmp_path / "gone"
    empty_store(gone)
    empty_tools = tmp_path / "empty-tools"
    empty_tools.mkdir()
    configured = configuration(remote, gone).model_copy(
        update={"tool_path": (str(empty_tools),)}
    )
    provider = DirVcs(configured)
    git_root = tmp_path / "git-world"
    git_root.mkdir()
    world = make_world(git_root, kora_capture)
    executor = Executor(
        provider.tools, ProcessLauncher(), provider, world.provider, provider
    )
    areas = world.bench().areas()
    revision: str | VcsRef = "1" * 64 if selection == "missing" else head
    if selection in {"reference", "missing_base"}:
        revision = VcsRef.model_validate(
            {
                "repo": REPO,
                "branch": "topic",
                "commit": head,
                "base_commit": "1" * 64 if selection == "missing_base" else base,
                "changed_paths": ["new.txt"],
                "diff_stat": "one added file",
                "pushed_by": provider.remotes[REPO].principal.model_dump(mode="json"),
            }
        )
    request = CheckoutRequest(
        repo=REPO,
        rev=revision,
        root=str(areas.candidate),
        attempt_id="directory-checkout",
        branch="topic",
    )
    ctx = world.context()
    plan = run(provider.checkout_plan(request, ctx))

    if selection in {"missing", "missing_base", "blob_suffix", "symlink_size"}:
        with pytest.raises(ExecutorError) as raised:
            run(executor.prepare_workspace(plan, request, areas, (), False, ctx))

        expected = (
            ErrorClass.CONFIG_ERROR
            if selection in {"missing", "missing_base"}
            else ErrorClass.INFRASTRUCTURE
        )
        assert raised.value.failure_class is expected
        assert areas.control is not None
        assert not list(areas.control.glob("checkout-*"))
        assert areas.candidate is not None
        assert not list(areas.candidate.rglob(".tmp-*"))
        return
    result = run(executor.prepare_workspace(plan, request, areas, (), False, ctx))

    assert areas.control is not None
    assert not list(areas.control.glob("checkout-*"))
    assert areas.candidate is not None
    assert not list(areas.candidate.rglob(".tmp-*"))
    assert result.commit == head
    assert result.branch == "topic"
    assert result.changed_paths == (("new.txt",) if selection == "reference" else ())
    assert areas.candidate is not None
    assert (areas.candidate / "new.txt").read_bytes() == b"new\n"
    assert (areas.candidate / "a.txt").read_bytes() == b"alpha\n"
    assert os.readlink(areas.candidate / "link") == "a.txt"
    assert (areas.candidate / "run.sh").stat().st_mode & 0o111
    assert (areas.candidate / ".kora/dirvcs/head").read_text() == head
    assert not list((areas.candidate / ".kora/dirvcs/revisions").iterdir())


def test_directory_head_refuses_a_fifo_without_waiting_for_a_writer(
    tmp_path: Path,
) -> None:
    remote, base = seeded_store(tmp_path)
    gone = tmp_path / "gone"
    empty_store(gone)
    configured = configuration(remote, gone)
    config_file = tmp_path / "config.json"
    config_file.write_text(configured.model_dump_json())
    reference: dict[str, object] = {
        "repo": REPO,
        "branch": "topic",
        "commit": base,
        "base_commit": base,
        "changed_paths": [],
        "diff_stat": "",
        "pushed_by": DirVcs(configured).remotes[REPO].principal.model_dump(mode="json"),
    }
    ref_file = tmp_path / "ref.json"
    ref_file.write_bytes(store.canonical(reference))
    os.mkfifo(remote / "heads/topic", 0o600)
    probe = Path(__file__).with_name("dirvcs_head_probe.py")

    completed = subprocess.run(
        [sys.executable, "-I", str(probe), str(config_file), str(ref_file)],
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (remote / "heads/topic").exists()


def test_directory_provider_logs_safe_checkout_events(tmp_path: Path) -> None:
    remote, base = seeded_store(tmp_path)
    gone = tmp_path / "gone"
    empty_store(gone)
    provider = DirVcs(configuration(remote, gone))
    request = CheckoutRequest(
        repo=REPO,
        rev=base,
        root=str(tmp_path / "candidate"),
        attempt_id="directory-checkout",
        branch="topic",
    )
    configured = provider.remotes[REPO].configuration
    credential = CredentialRequest(
        binding=configured.binding,
        binding_revision=configured.binding_revision,
        credential_mode=configured.credential_mode,
        repo=REPO,
        purpose="checkout",
        attempt_id="directory-checkout",
        operation_id=None,
        prepared_digest=None,
        destination_digest=None,
    )

    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(level=Level.DEBUG), "directory", utc_now
    ) as capture:
        ctx = context_for(capture)
        run(provider.checkout_plan(request, ctx))
        issued = run(provider.acquire_credential(credential, ctx))
        with pytest.raises(KoraProtocolError):
            run(provider.checkout_plan(replace(request, repo="private-unknown"), ctx))
        records = capture.finish()

    events = [
        record
        for record in records
        if str(record.get("event", "")).startswith("vcs.fake.")
    ]
    assert {record["event"] for record in events} == {
        "vcs.fake.planned",
        "vcs.fake.local_binding_used",
        "vcs.fake.refused",
    }
    assert issued.value is None
    assert str(remote) not in str(events)
    assert base not in str(events)
    assert "private-unknown" not in str(events)


def test_directory_store_failed_settlement_releases_scratch(tmp_path: Path) -> None:
    destination = tmp_path / "existing"
    destination.write_bytes(b"original")
    baseline = len(os.listdir("/dev/fd"))
    directory = os.open(tmp_path, store.DIRECTORY_FLAGS)

    try:
        with pytest.raises(store.Failed):
            store.put_bytes(directory, "existing", b"replacement")
    finally:
        os.close(directory)

    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob(".tmp-*"))
    assert not store.SCRATCH
    assert len(os.listdir("/dev/fd")) == baseline


def test_directory_scratch_is_discarded_on_cooperative_termination(
    tmp_path: Path,
) -> None:
    baseline = len(os.listdir("/dev/fd"))
    probe = Path(__file__).with_name("dirvcs_scratch_probe.py")
    child = subprocess.Popen(
        [sys.executable, "-I", str(probe), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5)
            assert child.stdout.readline() == b"ready\n"
        staged = list(tmp_path.glob(".tmp-*"))
        assert len(staged) == 1
        assert staged[0].read_bytes() == b"incomplete"
        child.send_signal(signal.SIGTERM)
        _, errors = child.communicate(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()

    assert child.returncode == 128 + signal.SIGTERM, errors
    assert not list(tmp_path.iterdir())
    assert len(os.listdir("/dev/fd")) == baseline
