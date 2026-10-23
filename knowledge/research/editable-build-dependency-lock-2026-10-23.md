# Editable build dependency lock

Checked on 2026-10-23 for [issue #225](https://github.com/realkarych/kora/issues/225) and [PR #230](https://github.com/realkarych/kora/pull/230). The probe used Python 3.11.17, uv 0.13.1 and Hatchling 1.32.4 against revision `4a5eb6ce96600942535af3d629b8fde1f0ae7819` and an isolated candidate with the build-group addition described below.

## Backend requirement and scope

Hatchling requests `editables~=0.3` from `get_requires_for_build_editable`. Its default editable builder imports the helper before adding the source directory to the generated path file. The installed editable metadata has no `editables` runtime requirement; the helper belongs in the build group. Evidence: [pinned Hatchling backend](https://raw.githubusercontent.com/pypa/hatch/hatchling-v1.32.4/backend/src/hatchling/build.py), the matching installed wheel builder and the installed distribution metadata.

The existing build-group export omitted the helper, so both explicit editable-install commands could resolve it outside `uv.lock`. The scaffold command already had this gap; the relocated IR measurement introduced another invocation. Current CI success did not establish complete locked build resolution. The finding concerns reproducibility, dependency admission and CI evidence; these probes do not establish a malicious release or credential compromise.

## Selected helper and enforcement

The build group pins `editables==0.6`, generated through uv without changing runtime dependencies or `build-system.requires`. This records a helper already requested by the selected backend. The backend, isolated build method and ADR-0013 stack choice are unchanged.

[PyPI metadata](https://pypi.org/project/editables/0.6/) checked on 2026-10-23 records Python >=3.7, MIT licensing and a universal Python wheel. The release was uploaded on 2026-04-14 using Trusted Publishing. The selected wheel SHA-256 is `d70e4698078a1d033e7786d9c64e5be070d058a67c21417024d38a58ac20aa43`; the source archive SHA-256 is `1163834902381c4613787951c5914800fdf155ae08848a373b8ea5006780977c`. Both hashes enter the generated lock. The helper declares no runtime dependencies; its source-build backend is separate from the selected universal wheel.

Both CI editable installations and the documented setup command use `--require-build-hashes` with the exported locked build constraints. This build-specific flag requires a trusted hash for every isolated build dependency. Ordinary `--require-hashes` on the editable project itself is unsuitable. The existing isolated offline distribution build retains its separate hash enforcement. Evidence: [uv build-dependency hashes](https://docs.astral.sh/uv/concepts/projects/build/#project-build-dependency-hashes), [uv CLI](https://docs.astral.sh/uv/reference/cli/#uv-pip-install), and the pinned tool's help and probes. In uv 0.13.1 the flag emits an experimental-feature warning; no warning suppression is added.

## Cold-cache probes

Each probe created a separate empty virtual environment and used `uv pip install --no-cache --no-deps --require-build-hashes --build-constraints <export> --editable <checkout>` with uv 0.13.1. The inner uv operation used `--no-cache`; the outer tool runner separately provisioned the exact uv executable. The current build export and the candidate export were generated using `uv export --locked --only-group build --no-emit-project --format requirements.txt`.

| Constraints | Observed result |
|---|---|
| Existing export without the helper | Exit 1: no trusted hash provided for `editables` |
| Candidate export with the exact helper and generated hashes | Exit 0: isolated editable build and installation succeed |
| Candidate export with both helper hashes replaced by zeroes | Exit 1: hash mismatch for `editables==0.6`, reporting the actual wheel digest |

After locked wheel setup, isolated Python imports and `kora --help` both exited 0 from outside the checkout with only `PATH`, `LANG` and `LC_ALL` supplied. Installed metadata had no helper runtime requirement. The subsequent locked `uv sync --no-build --group build --extra postgres --extra cel` reported 47 checked packages and performed no project rebuild. These local results do not replace final hosted checks.

Disabling isolation alone does not preserve this build: the default builder still imports the missing helper. An explicit-development-directory configuration plus disabled isolation would change both the editable mechanism and the accepted environment boundary. Declaring the existing helper preserves those behaviors.

Final verification belongs to the implementing pull request: real imports and CLI with `PYTHONPATH` absent, supported scaffold lanes, isolated offline distributions, CEL/IR measurements, dependency scanning, unchanged sample/resource gates and the ten-minute pipeline budget. Probe outputs do not replace final-revision CI.
