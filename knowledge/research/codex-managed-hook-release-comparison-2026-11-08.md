# Codex managed hook release comparison — 2026-11-08

[#967](https://github.com/realkarych/kora/issues/967) compares official Codex 0.166.0 and 0.167.0 in the same selected Linux app-server and exec-server profile. The experiment asks whether the newer dispatch path prevents the effects observed after a managed `PreToolUse` failure in [#804](https://github.com/realkarych/kora/issues/804) and [#838](https://github.com/realkarych/kora/issues/838).

## Matched observations

Both releases produced the following results for the same scripted Bash call or direct patch within each tool kind. The fixture matches the bounded hook receipt and tool response to the scripted call and the native hook completion to the acknowledged thread and turn. It checks the real workspace marker separately from the native notifications.

| Tool and managed hook behavior | Native observation in both releases | Model-visible result | Marker |
| --- | --- | --- | --- |
| Bash, exit 2 with the blocking reason | Hook `blocked` | Exact PreToolUse refusal | Absent |
| Bash, exit 2 with empty stderr | Hook `failed` | Bash exit 0 | Exact selected Bash and executor-home marker |
| Bash, exit 1 | Hook `failed` | Bash exit 0 | Exact selected Bash and executor-home marker |
| Bash, timeout after the hook started | Hook `failed` | Bash exit 0 | Exact selected Bash and executor-home marker |
| Direct `apply_patch`, exit 2 with the blocking reason | Hook `blocked`, zero file-change or command items | Exact PreToolUse refusal | Absent |
| Direct `apply_patch`, exit 1 | Hook `failed`, one started and completed `fileChange`, zero command items | Correlated patch result with exit 0 | Regular file with exact marker bytes |

For each timeout the probe matches the ready-socket peer PID to the recorded helper, observes exit through the live helper's pidfd and observes barrier EOF while both native roles are live. Each of the 12 cases then reaped the two direct native children, closed the local Responses server and streams, and confirmed removal of its exact container. The probe-container inventory was empty before and after the campaign. The cleanup evidence covers those owned children and channels and container removal. It supplies no independent inventory of all executor listeners, handles or descendants before removal.

The matched results still leave the selected profile without a blocking decision on the named hook failure paths. An explicit denial stopped the tested tools on both releases. Required Policy activation still depends on pre-effect authorization for the exact claimed kinds, controls and supported platforms. The Linux patch marker shows a filesystem effect in the selected profile. The probe observes the result and marker rather than attributing the write to a kernel-observed process or tracing the filesystem RPC. [Required tool-policy activation](../specs/codex-execution-profile.md#required-tool-policy-activation)

## Exact inputs and source observations

Both inputs came from kora `578ab606acad5c9a75c79ff64e4234e30141c21e`, with Codex provider tree `0a66c20b29f66a13ec88dabaee9f17c1c5f3d193` and fixture tree `629e277a8c5c3b22173666a1245f52830f6ec7c8`. The exported archive SHA-256 was `04b95fe3fbe45b51f53554744435f338b4b2ea881979227e7c89a99b07868eb5`. The control used the repository pin. The trial changed the official archive reference, archive checksum and version in the exported Dockerfile, and `CODEX_VERSION` in the exported launch configuration. The overlay comparison found exactly those two changed files. Docker checked the archive bytes before extraction and the executable version before running the cases.

| Official Codex release | Tagged source commit | Linux amd64 musl asset ID | Archive SHA-256 |
| --- | --- | ---: | --- |
| [0.166.0](https://github.com/openai/codex/releases/tag/rust-v0.166.0) | `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` | 689712643 | `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978` |
| [0.167.0](https://github.com/openai/codex/releases/tag/rust-v0.167.0) | `086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a` | 715506769 | `9d64836106d5b374e90ee4454c7ebcde7ca7106558bd7e41a34b9f26ebda251a` |

The official hook parser and hook runtime are byte-identical at the two source commits. The 0.167.0 registry moves the call into `apply_pre_tool_use`. That helper returns an error for an explicit block, invalid cwd or rejected input rewrite, and returns success for a `Continue` outcome without an input rewrite. This identifies the scope of the source comparison. The matrix supplies the actual effect evidence. [Hook parser](https://github.com/openai/codex/blob/086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a/codex-rs/hooks/src/events/pre_tool_use.rs) · [Hook runtime](https://github.com/openai/codex/blob/086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a/codex-rs/core/src/hook_runtime.rs) · [Tool registry](https://github.com/openai/codex/blob/086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a/codex-rs/core/src/tools/registry.rs#L1080-L1129)

## Reproduce

Run from a kora checkout with the named commit, Python 3, GNU `timeout`, and a local Docker socket with Buildx. The Python 3.11.17 image manifest is pinned to `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`. The measured host ran macOS arm64 and Docker Engine 29.9.2 with Buildx 0.38.1 and LinuxKit 7.0.14, using Linux amd64 containers under emulation. The build can fetch the pinned inputs. Execution uses nonroot containers with `--network none`, without host mounts. Both runs use a synthetic provider key, a fresh executor bearer and loopback Responses. The existing case, cleanup and attach limits are 30, 5 and 75 seconds, with a five-second native hook timeout in the timeout image.

```sh
set -eu
base=578ab606acad5c9a75c79ff64e4234e30141c21e
scratch=$(mktemp -d)
trap 'find "$scratch" -depth -delete' EXIT
git archive --format=tar --output="$scratch/fixture.tar" "$base" \
    src/kora/plugins/harness/codex tests/plugins/harness/codex
for version in control trial; do
    mkdir "$scratch/$version"
    tar -xf "$scratch/fixture.tar" -C "$scratch/$version"
done
python3 - "$scratch/trial" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
changes = {
    "tests/plugins/harness/codex/Dockerfile": {
        b"rust-v0.166.0/": b"rust-v0.167.0/",
        b"a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978": b"9d64836106d5b374e90ee4454c7ebcde7ca7106558bd7e41a34b9f26ebda251a",
        b"codex-cli 0.166.0": b"codex-cli 0.167.0",
    },
    "src/kora/plugins/harness/codex/launch_configuration.py": {
        b'CODEX_VERSION = "0.166.0"': b'CODEX_VERSION = "0.167.0"',
    },
}
for name, replacements in changes.items():
    path = root / name
    content = path.read_bytes()
    for old, new in replacements.items():
        assert content.count(old) == 1
        content = content.replace(old, new)
    path.write_bytes(content)
PY
for version in control trial; do
    (
        cd "$scratch/$version"
        sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-hook-failures
        sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-patch-hook-failures
    )
done
```

The four measured commands completed once each. Elapsed time includes image builds and is diagnostic. Each native mode's case checks still cover its exact expected wire and marker observations.

| Release and mode | Cases | Direct children reaped | Exact container removals | Exit | Elapsed |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0.166.0 Bash | 4 | 8 | 4 | 0 | 39.49 s |
| 0.166.0 direct patch | 2 | 4 | 2 | 0 | 15.74 s |
| 0.167.0 Bash | 4 | 8 | 4 | 0 | 102.59 s |
| 0.167.0 direct patch | 2 | 4 | 2 | 0 | 15.80 s |

The combined stdout/stderr log hashes, in table order, were

- `be747dd50e789a85c999f584f42d0e455f7423d61a6372284f11d7464b82bede`
- `55b6be774f9dcf0348e612b2ec4f473b537f4c13628f3cebb37d635b0ed32aff`
- `0837ff50c5e94ec3e88aea944cf7b07907faac10ada8f00e4ba3ef6c3b4d95cd`
- `0b8f467908aac414383e4ea1b24913ac00064ded2d26f492f98f3849f73d5e79`
