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

## Scaffold Node and offline links

The [first CI attempt](https://github.com/realkarych/kora/actions/runs/42468783748/attempts/1) for the scanner change failed to download Lychee and lost two Linux Scaffold receipts to the existing ten-minute job deadline. The Python 3.15 remainder and Python 3.11 conformance jobs spent 69 and 73 seconds in Node setup. The first made three GitHub archive attempts before the fallback from nodejs.org reached extraction in about 0.30 seconds. The second took about 68.58 seconds between the GitHub request and extraction. A [macOS conformance lane](https://github.com/realkarych/kora/actions/runs/42469702384/job/128697643269) on another revision spent 39 seconds in Node setup, including 34.43 seconds from request to extraction. The first full workflow took about 22 minutes to its final job, including more than ten minutes of scheduling wait for the two affected Linux lanes. These are observations from failed runs. [Linux remainder](https://github.com/realkarych/kora/actions/runs/42468783748/job/128694580400), [Linux conformance](https://github.com/realkarych/kora/actions/runs/42468783748/job/128694580454), [link job](https://github.com/realkarych/kora/actions/runs/42468783748/job/128694403476), [scheduling follow-up](https://github.com/realkarych/kora/issues/527).

The [approved extension](https://github.com/realkarych/kora/issues/1068#issuecomment-6720473810) prepares the pinned action's exact tool cache from nodejs.org on a Scaffold miss. The [pinned setup action](https://github.com/actions/setup-node/blob/820762786026740c76f36085b0efc47a31fe5020/src/distributions/official_builds/official_builds.ts) looks for the exact cache entry before consulting the GitHub manifest. The [Node release manifest](https://nodejs.org/dist/v24.22.0/SHASUMS256.txt) supplies the two archive digests. The GitHub archives repackage the same upstream Node release binaries and have distinct archive digests. [GitHub archive builder](https://github.com/actions/node-versions/blob/442d4508db8277904aa4b7ac19ba219e94dafd34/builders/nix-node-builder.psm1).

| Official Node v24.22.0 archive | Bytes | SHA-256 |
| --- | ---: | --- |
| `node-v24.22.0-linux-x64.tar.gz` | 58,256,361 | `28ea0a438de9a13bd7891fd758eee6c7819ee8af5442dfd8d45bd65e56d4228c` |
| `node-v24.22.0-darwin-arm64.tar.gz` | 53,049,576 | `6a9724d9b50a5f22b4816cded441b5a8d9ddae546191a5e5b7efa5a5e5e2ee26d5` |

Use `RUNNER_TOOL_CACHE=<existing physical private directory> bash scripts/ci/setup_scaffold_node.sh` from the repository root on Linux x64 or macOS arm64. An absent architecture directory and completion marker select cold acquisition. A valid cache hit leaves the cache and the runner PATH untouched. A partial entry refuses. Cold staging stays on the same filesystem. The helper checks the archive size and digest and requires successful exact executable version output, moves the extracted tree and writes the completion marker last. The pinned setup action retains PATH publication. Each cold request has a 15-second connection limit, 60-second transfer limit and 64 MiB byte cap.

A macOS arm64 probe of the implemented preparation and cache handoff took 36.012 seconds cold and 0.010 seconds for reuse. The pinned setup action selected the exact cache and emitted its `bin` path, and `node --version` returned `v24.22.0`. An initial local action invocation lacked `GITHUB_WORKSPACE` and failed before cache lookup; the corrected invocation set the same workspace input as hosted Actions. The helper published no PATH or environment entry and removed its staging. A separate Linux x64 probe of the pinned action accepted the equivalent preseeded cache with networking disabled. Local transfer time includes that environment's route, verification and extraction. Hosted times remain separate. [Native acquisition and cache evidence](https://github.com/realkarych/kora/issues/1068#issuecomment-6720401612).

The [official Lychee 0.24.2 release](https://github.com/lycheeverse/lychee/releases/tag/lychee-v0.24.2) supplies Linux x64 GNU asset `409959156`. Its public binary API produced the release archive in a credential-free probe. A separate request returned HTTP 504. The available evidence does not identify which hop produced that response. The workflow request has a 15-second connection limit, a 60-second transfer limit and a 16 MiB byte cap.

| Lychee pin | Bytes | SHA-256 |
| --- | ---: | --- |
| Archive | 7,626,970 | `1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a` |
| `lychee-x86_64-unknown-linux-gnu/lychee` | | `87e6e75195df5753f08c53b5c0a13694b8328edd8df009914ae9e29b5000162d` |

To run the job locally on Ubuntu x64, set `RUNNER_TEMP` to an existing private directory, `GITHUB_REPOSITORY` to the repository slug and `GITHUB_RUN_ID` to a run identifier, then use `bash scripts/ci/check_links.sh` from the repository root. The wrapper verifies the archive before extracting the selected member and verifies the executable before starting it. The report remains at `RUNNER_TEMP/lychee.md`. Both checker streams and the report are replayed on one output stream while GitHub workflow commands are suspended, because diagnostic paths can contain repository-controlled text. The temporary checker log scales with checker output and is removed with the private staging.

The pinned binary on Ubuntu 24.04 x64 passed 5,721 links across 295 Markdown files with networking disabled. A document with no links produced a zero-total report, and a broken link returned nonzero. Five other offline cases verified scheme-like names, leading dashes, glob metacharacters, spaces and embedded newlines. In the glob case, the raw input matched a passing decoy. The escaped literal input detected the broken link in the intended file. [Offline controls](https://github.com/realkarych/kora/issues/1068#issuecomment-6720401612), [literal filename controls](https://github.com/realkarych/kora/issues/1068#issuecomment-6720473810).

The three downloads keep their fixed sources, integrity checks and bounded failure paths. Hosted acquisition, the link report, the complete scanner, all twelve same-attempt Scaffold receipts and the existing mandatory statuses remain final-head checks. End-to-end scheduling and setup duration remain diagnostics.
