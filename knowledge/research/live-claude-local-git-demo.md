# Live Claude and local Git demo

The [reference graph](../specs/examples/live-claude-local-git.ir.json) asks Claude to write one line to `demo.txt`. Kora publishes the inspected revision to a local bare Git repository. A separate command checks the exact published commit, and the ordinary console shows the command attempt's success from the same run.

The offline procedure exercises the verifier embedded in the graph with real Git and the installed VcsRef parser. The live witness remains pending. On `33f2d40`, deploy and manual start still refuse the graph's two-input bound CLI approval. Ordinary execution also needs the publication and sealed command input deliveries. [#1919 dependency check](https://github.com/realkarych/kora/issues/1919#issuecomment-7202461193)

## Prepare a disposable repository

Use a trusted Linux or macOS environment dedicated to this demo. The `local` runtime is cooperative. Give it a private installation and repository with no ambient service credentials, custom Git hooks or includes. The live installation needs a trusted Git binding for the logical repository, selecting `local_credential_free` with the saved binding identity, revision and scoped local principal. Configure only the dedicated model credential through the ordinary private harness configuration. The graph carries no credentials. [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md) · [Local publication contract](../specs/protocols.md#credential-free-local-publication)

Use Bash from the repository root with Kora installed at the checked revision. The default Python path below comes from the locked development environment. To use the installed runtime's Python, set `KORA_DEMO_PYTHON` to its absolute executable path. Set `KORA_DEMO_GIT` if the installation's trusted Git executable has a different absolute path. Run this block again in a fresh Bash session for the live witness to start with a clean bare repository. The trusted Git binding requires a physical repository path without symlink aliases. [Development setup](../../docs/development.md#tool-setup)

```bash
set -euo pipefail
umask 077
demo_dir=$(mktemp -d "${TMPDIR:-/tmp}/kora-live-demo.XXXXXX")
demo_dir=$(cd -P -- "$demo_dir" && pwd -P)
demo_home="$demo_dir/home"
mkdir "$demo_home"
bare="$demo_dir/demo.git"
seed="$demo_dir/seed"
git_bin=${KORA_DEMO_GIT:-/usr/bin/git}
kora_python=${KORA_DEMO_PYTHON:-$(uv run --no-sync python -c 'import sys; print(sys.executable)')}

demo_git() {
    env -i PATH=/usr/bin:/bin HOME="$demo_home" GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL=/dev/null "$git_bin" --no-replace-objects "$@"
}

demo_commit() {
    demo_git -C "$1" -c user.name='Kora Demo' -c user.email='demo@example.invalid' \
        -c commit.gpgsign=false commit "${@:2}"
}

demo_git init -q --bare --object-format=sha1 --initial-branch=main --template= "$bare"
demo_git init -q --object-format=sha1 --initial-branch=main --template= "$seed"
printf 'seed\n' > "$seed/demo.txt"
demo_git -C "$seed" add -- demo.txt
demo_commit "$seed" -qm 'Seed the demo repository'
base=$(demo_git -C "$seed" rev-parse --verify 'HEAD^{commit}')
demo_git -C "$seed" push -q -- "$bare" HEAD:refs/heads/main
```

Render the two workspace repository fields with the same logical repository value. This example uses the absolute bare path as that value. Render the agent base SHA and the two absolute verifier tool paths. The command selects `inputs.change` at execution time and has no workspace revision fallback. Validate the complete JSON directly through `GraphDocument`.

```bash
reference=knowledge/specs/examples/live-claude-local-git.ir.json
rendered_ir="$demo_dir/live-claude-local-git.ir.json"
repo="$bare"
"$kora_python" -I - "$reference" "$rendered_ir" "$repo" "$base" "$git_bin" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

from kora.ir import GraphDocument

reference, destination, repo, base, git = sys.argv[1:]
if not Path(sys.executable).is_absolute() or not Path(git).is_absolute():
    raise SystemExit("verifier tool paths must be absolute")
document = json.loads(Path(reference).read_bytes())
agent = document["graph"]["nodes"]["agent"]["config"]
agent["workspace"].update(repo=repo, rev=base)
gate = document["graph"]["nodes"]["gate"]["config"]
gate["workspace"]["repo"] = repo
gate["cmd"][0] = sys.executable
gate["cmd"][4] = git
payload = (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
GraphDocument.model_validate_json(payload)
Path(destination).write_bytes(payload)
print("rendered_ir_sha256=" + hashlib.sha256(payload).hexdigest())
PY
```

## Check the exact SHA offline

Run this block in the same session. It creates two distinct commits with the same tree and uses a local push as an independent ref observation. The input files are constructed for this offline procedure with the VcsRef revision-1 model. The offline result covers the verifier, including refusal despite matching marker content. The live witness requires a Kora-produced VcsRef and the real sealed input owner.

```bash
checkout="$demo_dir/checkout"
demo_git clone -q --no-hardlinks -- "$bare" "$checkout"
printf 'kora live demo\n' > "$checkout/demo.txt"
demo_git -C "$checkout" add -- demo.txt
demo_commit "$checkout" -qm 'Write the demo marker'
published=$(demo_git -C "$checkout" rev-parse --verify 'HEAD^{commit}')
demo_commit "$checkout" -qm 'Create another identity for the same tree' --allow-empty
other=$(demo_git -C "$checkout" rev-parse --verify 'HEAD^{commit}')
test "$published" != "$other"
test "$(demo_git -C "$checkout" rev-parse "$published^{tree}")" = \
    "$(demo_git -C "$checkout" rev-parse "$other^{tree}")"
demo_git -C "$checkout" push -q -- "$bare" "$published:refs/heads/offline-check"
test "$(demo_git --git-dir="$bare" rev-parse --verify 'refs/heads/offline-check^{commit}')" = "$published"
demo_git -C "$checkout" merge-base --is-ancestor "$base" "$published"
demo_git -C "$checkout" checkout -q --detach "$published"

"$kora_python" -I - "$rendered_ir" "$demo_dir" "$checkout" "$repo" "$base" "$published" "$other" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

from kora.ir.vcs_ref import canonical_vcs_ref, parse_vcs_ref

rendered, directory, checkout, repo, base, published, other = sys.argv[1:]
document = json.loads(Path(rendered).read_bytes())
command = document["graph"]["nodes"]["gate"]["config"]["cmd"]
for name, sha, expected in (
    ("matching", published, (0, f"verified_commit={published}\n".encode(), b"")),
    ("different", other, (3, b"", b"commit mismatch\n")),
):
    reference = parse_vcs_ref({
        "repo": repo,
        "branch": "offline-check",
        "commit": sha,
        "base_commit": base,
        "changed_paths": ["demo.txt"],
        "diff_stat": "",
        "pushed_by": {
            "provider_instance": "demo.git",
            "stable_id": "offline-check",
            "kind": "local_service",
            "display_name": "Offline verifier fixture",
        },
    })
    input_root = Path(directory) / name
    input_root.mkdir()
    input_path = input_root / "change.json"
    input_path.write_bytes(canonical_vcs_ref(reference))
    if command[-1] != "{input_root}/change.json":
        raise SystemExit("unexpected verifier input argument")
    result = subprocess.run(
        [*command[:-1], str(input_path)], cwd=checkout, env={},
        capture_output=True, check=False, timeout=10,
    )
    if (result.returncode, result.stdout, result.stderr) != expected:
        raise SystemExit(f"{name} SHA verification failed (exit {result.returncode})")
    print(f"{name}: exit {result.returncode}")
print(f"verified_commit={published}")
PY
```

Record the graph revision, installed Python and Git identities, rendered JSON digest and the command's result. Expected results are `matching: exit 0`, `different: exit 3` and `verified_commit=<published SHA>`. The refusal is checked with empty stdout and exactly `commit mismatch` plus one newline on stderr. Dispose of the offline scratch directory after all its commands exit.

## Run once after ordinary execution activation

Use the ordinary launch and stop procedures supplied by work item 20, with the installed Claude, LocalRuntime and Git binding, a private prepared SQLite database, and an authorized personal credential for the graph namespace. Record the installed revision and active producer versions. Confirm installed deploy and trigger admit this bound graph and that trusted publication, accepted VcsRef and AgentSession, sealed command inputs, and CommandResult acceptance and reads are available. If an interface or ownership check is missing, keep the live witness pending with its owner. The `first-claim` staging API has a narrower scope, and the isolated walking skeleton uses an input-free command. [Work item 20](../../docs/implementation-plan.md#6-work-items) · [Installed Scheduler staging](../specs/api-and-agent-surface.md#installed-sqlite-scheduler-staging) · [First-slice contract](../specs/first-slice.md) · [Dependency check](https://github.com/realkarych/kora/issues/1919#issuecomment-7202461193)

Once the ordinary execution installation is ready, use its origin and personal credential through `KORA_URL` and `KORA_TOKEN`. Deploy the rendered JSON and trigger its exact returned graph version. Require an accepted run for that same version.

```bash
graph=live_claude_local_git
ns=example
kora deploy "$rendered_ir" --json > "$demo_dir/deploy.json"
graph_version=$("$kora_python" -I -c '
import sys
from pathlib import Path
from kora.models.graphs import DeployResponse
print(DeployResponse.model_validate_json(Path(sys.argv[1]).read_bytes()).graph_version_id)
' "$demo_dir/deploy.json")
kora trigger "$graph" --graph-version "$graph_version" --namespace "$ns" --json > "$demo_dir/trigger.json"
run_id=$("$kora_python" -I -c '
import sys
from pathlib import Path
from kora.models.runs import AcceptedRun
run = AcceptedRun.model_validate_json(Path(sys.argv[1]).read_bytes())
if run.graph_version_id != sys.argv[2]:
    raise SystemExit("unexpected run graph version")
print(run.run_id)
' "$demo_dir/trigger.json" "$graph_version")
printf 'graph_version=%s\nrun_id=%s\n' "$graph_version" "$run_id"
```

Use the installed scoped artifact collection and payload reads for this namespace and run. The contract supplies `GET /namespaces/{ns}/runs/{run}/artifacts`, `GET /namespaces/{ns}/artifacts/{art}/payload` and the immediate lineage and selected attempt input reads. Keep raw payloads private and retain only selected identities and comparison results. [Bounded read projections](../specs/bounded-read-projections.md#4-normative-route-catalogue)

1. Select the current accepted `agent.change` VcsRef. Record its artifact version, producing attempt, branch and full commit, and compare its repository and base commit to the configured values. With `branch` and `commit` taken from that VcsRef, verify the precise branch ref, base ancestry, distinct commit, and committed marker bytes.

   ```bash
   test "$(demo_git --git-dir="$bare" rev-parse --verify "refs/heads/$branch^{commit}")" = "$commit"
   test "$commit" != "$base"
   demo_git --git-dir="$bare" merge-base --is-ancestor "$base" "$commit"
   printf 'kora live demo\n' > "$demo_dir/expected-marker"
   demo_git --git-dir="$bare" cat-file blob "$commit:demo.txt" | cmp - "$demo_dir/expected-marker"
   ```

2. Select the current accepted `gate.result` CommandResult in the same run. Its attempt is distinct from the agent attempt. The saved `change` input binding or direct upstream lineage must name the exact accepted VcsRef artifact version. Require `CommandResult.commit == VcsRef.commit`, `exit_code == 0` and `stop_reason == "completed"`. In `stdout` require EOF, no truncation and `size == observed_bytes == len(expected)`, where `expected` is the UTF-8 encoding of `verified_commit=<commit>` plus one newline. Require strict Base64 decoding of `tail_base64` to equal those exact bytes and verify the saved capture SHA-256 over them. The [command input contract](../specs/data-model.md#command-input-preparation-issue-106) owns the sealed input digest and runtime re-hash. Compare the saved input digest against the canonical accepted VcsRef bytes if the digest is exposed. [CommandResult revision 1](../specs/artifacts/CommandResult.md)
3. Read the finite timeline and identify exactly one committed `attempt.succeeded` with that gate attempt ID and run ID. This is the demo's stopping point. Preserve any durable pending approval and holds for the installation's approval lifecycle.

## Check the same run in the browser

Wait for actual attempt resource release. Stop and join the execution Worker, Scheduler and API through their ordinary lifecycle. If execution or cleanup is unresolved, preserve its owners and stop the handoff. Keep the identical prepared SQLite database for the browser profile.

After [#1896](https://github.com/realkarych/kora/issues/1896) is installed, start the approved ordinary SQLite HTTPS console with `KORA_API_CONSOLE=1` and the same `KORA_DATABASE_URL`. Unset `KORA_API_HARNESS`, `KORA_API_WORKER_CLOCK_UNCERTAINTY_US` and `KORA_API_SCHEDULER`. Set `KORA_API_HTTPS_ORIGIN` to the exact `https://127.0.0.1:<port>` origin for `kora api --port <port>` and supply `KORA_API_TLS_CERT_FILE` and `KORA_API_TLS_KEY_FILE`. Verify the certificate chain and the `127.0.0.1` IP identity in both the CLI and browser. Set CLI `KORA_URL` to this HTTPS origin and `KORA_CA_BUNDLE` to its approved trust bundle. [ADR-0029](../../docs/decisions/0029-installed-api-serving-stack.md#verified-loopback-https-for-ordinary-sqlite) · [Console launch plan](https://github.com/realkarych/kora/issues/1896#issuecomment-7193639770)

```bash
kora events "$run_id" --namespace "$ns" --limit 20 --json
kora open "$run_id" --namespace "$ns"
```

Use the CLI continuation cursor and the browser's earlier-page action if needed. The displayed namespace, run ID, event ID, kind, time, actor and gate attempt ID must match the authorized CLI event row. The page observation time belongs to its own read. The committed event identifies the attempt whose accepted result proved the SHA. Receipt of a bound approval and `run.succeeded` are separate outcomes.

Retain the installed revision, rendered JSON digest, deployed graph version, namespace, run ID, both attempt and artifact version IDs, full verified commit, and matched event identity and attribution. Keep login URLs, codes, cookies, credentials, prompt text, transcripts, and raw artifact payloads out of the evidence. Sign out and stop the console normally. Resolve outstanding approval and resource ownership through the installation lifecycle before disposing of its private files.
