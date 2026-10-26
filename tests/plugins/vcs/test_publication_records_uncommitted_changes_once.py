from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.vcs import Kept, pinned, publish_request, run
from tests.plugins.vcs.git_support import World, git, make_world, write


def parents(world: World, repository: Path, commit: str) -> list[str]:
    line = git(
        world.home, f"--git-dir={repository}", "rev-list", "--parents", "-n1", commit
    )
    return line.split()[1:]


def prepared_repository(location: str) -> Path:
    return Path(location) / "repo"


def test_publication_records_uncommitted_changes_once(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    head = world.commit(checkout, {"a.txt": "committed\n"}, "committed")
    write(candidate, {"a.txt": "edited\n", "keep/old.txt": None, "new.txt": "new\n"})

    prepared = world.prepare(checkout)

    revision = prepared.result.revision
    repository = prepared_repository(revision.location)
    assert parents(world, repository, revision.commit) == [head]
    assert set(prepared.result.publication.changed_paths) == {
        "a.txt",
        "keep/old.txt",
        "new.txt",
    }


def test_uncommitted_changes_without_candidate_commits_need_no_bundle(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"only.txt": "only\n"})

    prepared = world.prepare(checkout)

    revision = prepared.result.revision
    assert not (Path(prepared.sealed.location) / "head.bundle").exists()
    assert parents(world, prepared_repository(revision.location), revision.commit) == [
        world.base
    ]


def test_clean_head_needs_no_extra_commit(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    head = world.commit(checkout, {"a.txt": "clean\n"}, "clean")

    prepared = world.prepare(checkout)

    assert prepared.result.revision.commit == head


def test_persisted_revision_is_reused_without_a_new_commit(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"once.txt": "once\n"})
    first = world.prepare(checkout)
    repository = prepared_repository(first.result.revision.location)
    commits_before = git(world.home, f"--git-dir={repository}", "rev-list", "--all")
    kept = Kept()

    again = run(
        world.executor.prepare_and_inspect(
            first.plan,
            first.sealed,
            checkout.workspace(),
            publish_request(world.remote_fixture(), checkout),
            pinned(world.binding()),
            checkout.areas,
            (),
            (),
            kept,
            world.context(),
            first.result.revision,
        )
    )

    commits_after = git(world.home, f"--git-dir={repository}", "rev-list", "--all")
    assert again.revision == first.result.revision
    assert again.publication == first.result.publication
    assert commits_after == commits_before
    assert kept.revisions == []
