from tests.support.workflows import (
    action_name,
    command_lines,
    step_list,
    table,
    workflow_document,
)

MIRROR = "https://archive.ubuntu.com/ubuntu"
MIRROR_LIST = "/etc/apt/apt-mirrors.txt"
FRONTEND = ("pnpm", "--dir", "web")
EVIDENCE_DIRECTORY = "measurements/browser"
MAXIMUM_RETENTION_DAYS = 14
BROWSER_COMMANDS = [
    ("test", "-f", MIRROR_LIST),
    ("printf", "%s\\n", MIRROR, "|", "sudo", "tee", MIRROR_LIST, ">", "/dev/null"),
    (*FRONTEND, "install", "--frozen-lockfile"),
    (
        *FRONTEND,
        "exec",
        "playwright",
        "install",
        "--with-deps",
        "--only-shell",
        "chromium",
    ),
    (*FRONTEND, "run", "typecheck"),
    (*FRONTEND, "run", "lint"),
    (*FRONTEND, "run", "build"),
    (*FRONTEND, "run", "test:browser"),
]


def test_browser_setup_preserves_authenticated_installation_and_hard_checks() -> None:
    job = table(table(workflow_document()["jobs"])["browser"])
    entries = step_list(job)
    publication = entries[-2]
    upload = table(publication["with"])

    commands = [
        line
        for step in entries[:-1]
        if "run" in step
        for line in command_lines(step["run"])
    ]

    assert job["name"] == "Console browser checks"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 10
    assert "permissions" not in job
    assert "continue-on-error" not in job
    assert table(table(job["defaults"])["run"])["shell"] == "bash"
    assert all("continue-on-error" not in step for step in entries)
    assert entries[-1]["if"] == "${{ always() }}"
    assert all(
        step["if"] == "${{ needs.change-scope.outputs.full != 'false' }}"
        for step in entries[:-2]
    )
    assert commands == BROWSER_COMMANDS
    assert action_name(publication["uses"]) == "actions/upload-artifact"
    assert (
        publication["if"]
        == "${{ needs.change-scope.outputs.full != 'false' && (always()) }}"
    )
    assert upload["if-no-files-found"] == "error"
    assert set(upload) <= {"name", "path", "if-no-files-found", "retention-days"}
    assert str(upload["path"]).rstrip("/") == EVIDENCE_DIRECTORY
    assert isinstance(upload["retention-days"], int)
    assert 0 < upload["retention-days"] <= MAXIMUM_RETENTION_DAYS
