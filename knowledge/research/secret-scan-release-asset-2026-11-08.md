# Secret scanner release asset acquisition

On 2026-11-08, the required secret scan for the CI placement change failed while downloading Gitleaks 8.30.1. The scanner had not started. An isolated recovery and a fresh head built on the updated master also received HTTP 504. The failure is visible in the [initial job](https://github.com/realkarych/kora/actions/runs/42466354739/job/128686681888), the [isolated recovery](https://github.com/realkarych/kora/actions/runs/42466354739/job/128691207738) and the [fresh job](https://github.com/realkarych/kora/actions/runs/42467997754/job/128691883148).

| Official download request | Observed response |
| --- | --- |
| Published release URL for `gitleaks_8.30.1_linux_x64.tar.gz` | HTTP 504 with no archive. |
| Published release URL with `download=1` | HTTP 504 with no archive. |
| Public release asset API for asset `378332058` with the binary media type | HTTP 200 and 8,230,402 bytes. The archive and executable passed the existing verification. |

The public API request also passed with an empty home, clean environment and curl configuration disabled. The [GitHub asset API](https://docs.github.com/en/rest/releases/assets#get-a-release-asset) supports a direct binary response or a redirect for this media type. The [official v8.30.1 release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1) identifies the asset. The [owner-approved change](https://github.com/realkarych/kora/issues/1068) selects that asset through its public API.

| Existing pin | SHA-256 |
| --- | --- |
| Archive | `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` |
| Executable | `88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509` |

The downloader makes one unauthenticated HTTPS acquisition with the existing 60-second transfer limit and 65-second process timeout. It verifies the archive size and SHA-256 before cache publication. It separately verifies the selected executable before writing it to private storage and verifies the written bytes before execution. API rate limits and invalid responses still fail the job. The scan command, revision range, adverse histories and required status use the existing policy.

The following command runs cold acquisition and both verification steps without executing the scanner. Run it from the repository root. The owned temporary files are removed when the command returns.

```sh
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.ci.secret_scan import SCANNER, cached_archive, executable

with TemporaryDirectory() as temporary:
    root = Path(temporary)
    archive = cached_archive(root / "cache", SCANNER)
    output = root / "private"
    output.mkdir(mode=0o700)
    executable(archive, output, SCANNER)
PY
```

The hosted `Secret scan` job provides the separate evidence for real Linux execution, adverse-history detection, complete actual-head scanning and the redacted summary.
