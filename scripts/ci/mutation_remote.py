import io
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from http.client import HTTPException
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from scripts.ci.mutation_models import MAX_FILE, Report


class NativeRun(BaseModel):
    id: int
    run_attempt: int
    head_sha: str
    head_branch: str
    event: str
    conclusion: str | None
    path: str


class Runs(BaseModel):
    workflow_runs: list[NativeRun] = Field(max_length=100)


class Artifact(BaseModel):
    id: int
    name: str
    expired: bool
    size_in_bytes: int


class Artifacts(BaseModel):
    artifacts: list[Artifact] = Field(max_length=100)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: object,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def fetch(url: str, token: str = "") -> bytes:
    target = urlsplit(url)
    if target.scheme != "https" or (token and target.hostname != "api.github.com"):
        raise ValueError("native metadata requires https")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://{url[8:]}", headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as reply:
        value: bytes = reply.read(MAX_FILE + 1)
    if len(value) > MAX_FILE:
        raise ValueError("oversized native response")
    return value


def eligible(artifact: Artifact, run: NativeRun) -> bool:
    return (
        artifact.name == f"mutation-{run.id}-{run.run_attempt}"
        and not artifact.expired
        and 0 < artifact.size_in_bytes <= MAX_FILE
        and artifact.id > 0
    )


def restore(repository: str, token: str, current: int) -> Report | None:
    if re.fullmatch(r"[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*", repository) is None:
        raise ValueError("invalid native repository")
    base = f"https://api.github.com/repos/{repository}/actions"
    runs = Runs.model_validate_json(
        fetch(
            f"{base}/workflows/mutation.yml/runs?branch=master&per_page=100",
            token,
        )
    )
    for run in runs.workflow_runs:
        if (
            run.id == current
            or run.head_branch != "master"
            or run.event not in {"schedule", "workflow_dispatch"}
            or run.path != ".github/workflows/mutation.yml"
            or run.conclusion != "success"
        ):
            continue
        artifacts = Artifacts.model_validate_json(
            fetch(
                f"{base}/runs/{run.id}/artifacts?per_page=100",
                token,
            )
        )
        for artifact in artifacts.artifacts:
            if not eligible(artifact, run):
                continue
            try:
                fetch(f"{base}/artifacts/{artifact.id}/zip", token)
            except urllib.error.HTTPError as error:
                if error.code != 302:
                    raise
                location = error.headers["Location"]
            else:
                raise ValueError("missing native artifact redirect")
            target = urlsplit(location)
            if target.scheme != "https" or not (
                (target.hostname or "").endswith(".blob.core.windows.net")
                or (target.hostname or "").endswith(".githubusercontent.com")
            ):
                raise ValueError("unsupported artifact origin")
            return validated_state(fetch(location), run)
    return None


def validated_state(content: bytes, run: NativeRun) -> Report:
    if (
        len(content) > MAX_FILE
        or run.head_branch != "master"
        or (
            run.event not in {"schedule", "workflow_dispatch"}
            or run.path != ".github/workflows/mutation.yml"
            or run.conclusion != "success"
        )
    ):
        raise ValueError("invalid native artifact publisher")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if len(archive.infolist()) > 10:
            raise ValueError("oversized artifact inventory")
        info = archive.getinfo("report.json")
        if info.file_size > MAX_FILE or info.is_dir():
            raise ValueError("oversized artifact report")
        report = Report.model_validate_json(archive.read(info))
    if (
        report.source_revision != run.head_sha
        or report.tooling_revision != run.head_sha
        or report.run_id != run.id
        or report.attempt != run.run_attempt
        or report.event != run.event
        or report.status == "failed"
        or report.tooling_dirty
        or report.retained_workspace
        or len(set(report.rotation)) != len(report.rotation)
        or not set(report.rotation).issubset(report.results)
    ):
        raise ValueError("native report identity mismatch")
    return report


def main() -> int:
    try:
        restored = restore(
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_TOKEN"],
            int(os.environ["GITHUB_RUN_ID"]),
        )
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        zipfile.BadZipFile,
        HTTPException,
    ):
        sys.stderr.write('{"event":"mutation.state.unavailable","level":"WARNING"}\n')
        return 1
    sys.stdout.write(restored.model_dump_json() if restored else "null")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
