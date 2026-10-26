from typing import cast

from tests.architecture.test_measurement_job_routing import table, workflow_document


def test_browser_setup_preserves_authenticated_installation_and_hard_checks() -> None:
    workflow = workflow_document()
    job = table(table(workflow["jobs"])["browser"])
    entries = [table(value) for value in cast(list[object], job["steps"])]
    named = {str(step["name"]): step for step in entries if "name" in step}

    installation = str(named["Install pinned browser tools"]["run"]).splitlines()
    source_checks = str(named["Check and build browser sources"]["run"]).splitlines()
    publication = entries[-1]

    assert workflow["permissions"] == {"contents": "read"}
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 10
    assert table(table(job["defaults"])["run"])["shell"] == "bash"
    assert installation == [
        "test -f /etc/apt/apt-mirrors.txt",
        "printf '%s\\n' 'https://archive.ubuntu.com/ubuntu' "
        "| sudo tee /etc/apt/apt-mirrors.txt >/dev/null",
        "pnpm --dir web install --frozen-lockfile",
        "pnpm --dir web exec playwright install --with-deps chromium",
    ]
    assert source_checks == [
        "pnpm --dir web run typecheck",
        "pnpm --dir web run lint",
        "pnpm --dir web run build",
    ]
    assert named["Run browser checks"]["run"] == "pnpm --dir web run test:browser"
    assert [step["name"] for step in entries if "name" in step] == [
        "Install pinned browser tools",
        "Check and build browser sources",
        "Run browser checks",
    ]
    assert publication["uses"] == (
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
    )
    assert publication["if"] == "${{ always() }}"
    assert publication["with"] == {
        "name": "console-browser-checks",
        "path": "measurements/browser/",
        "if-no-files-found": "error",
        "retention-days": 14,
    }
