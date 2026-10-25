from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.vcs import Kept, Prepared, pinned, publish_request, run
from tests.plugins.vcs.git_support import git, make_world, write


def marker_script(directory: Path, name: str) -> Path:
    script = directory / f"{name}.sh"
    script.write_text(f"#!/bin/sh\n/usr/bin/touch {directory}/ran-{name}\n/bin/cat\n")
    script.chmod(0o755)
    return script


def test_candidate_git_configuration_cannot_execute_in_publisher(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    markers = tmp_path / "markers"
    markers.mkdir()
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    for key, name in (
        ("filter.marker.clean", "clean"),
        ("filter.marker.smudge", "smudge"),
        ("filter.marker.process", "process"),
        ("core.fsmonitor", "fsmonitor"),
        ("credential.helper", "helper"),
        ("diff.external", "external"),
        ("diff.marker.textconv", "textconv"),
        ("core.hooksPath", "hooks"),
    ):
        target = markers if name == "hooks" else marker_script(markers, name)
        git(world.home, "config", key, str(target), cwd=candidate)
    for hook in ("pre-commit", "reference-transaction", "pre-push", "post-checkout"):
        script = marker_script(markers, hook)
        script.rename(markers / hook)
    write(candidate, {".gitattributes": "* filter=marker diff=marker\n"})
    world.commit(checkout, {"a.txt": "planted\n"}, "planted")
    git(world.home, "config", "filter.marker.required", "false", cwd=candidate)
    remote = world.remote_fixture()
    workspace = checkout.workspace()
    context = world.context()
    plan = run(world.provider.publish_plan(publish_request(remote, checkout), context))
    exported = run(world.executor.export(plan, workspace, checkout.areas, context))
    after_export = sorted(path.name for path in markers.glob("ran-*"))

    sealed = run(world.executor.seal(exported, workspace, checkout.areas, context))
    prepared = run(
        world.executor.prepare_and_inspect(
            plan,
            sealed,
            workspace,
            publish_request(remote, checkout),
            pinned(world.binding()),
            checkout.areas,
            (),
            (),
            Kept(),
            context,
        )
    )
    outcome = world.publish(Prepared(checkout, plan, sealed, prepared))
    after_publication = sorted(path.name for path in markers.glob("ran-*"))

    assert outcome.state == "pushed"
    assert after_publication == after_export
    assert after_export
