# Selected Codex Bash trace environment

[#1022](https://github.com/realkarych/kora/issues/1022) verifies two Bash trace controls in the selected Linux Codex profile. The private constructor refuses exact `SHELLOPTS` and `PS4` in both authored name collections with `codex_launch_environment_conflict`. It uses names only, preserves adjacent names, and leaves the two controls unset in both role recipes.

## Matched native observations

Bash enables options named in an inherited `SHELLOPTS` at startup. With tracing enabled, Bash expands `PS4` before each traced command. The [Bash variable](https://www.gnu.org/s/bash/manual/html_node/Bash-Variables.html) and [trace](https://www.gnu.org/s/bash/manual/html_node/The-Set-Builtin.html) references specify these mechanisms.

The two isolated pairs keep the command and expected managed-hook input exact within each pair. The fixture supplies a value only to the materialized exec-server environment in the adverse case of each pair. In the explicit-trace pair, neither case supplies `SHELLOPTS`.

| Approved command | Fixture input | Trace in the subsequent local Responses request | Separate workspace marker |
| --- | --- | --- | --- |
| Ordinary selected Bash command | Default | Absent | Absent |
| Same ordinary command | `SHELLOPTS=xtrace` | Fixed command witness with the default trace prefix | Absent |
| Same command preceded by `set -x` | Default | Fixed command witness with the default trace prefix | Absent |
| Same explicit-trace command | `PS4` containing fixed command substitution | Fixed command witness on the line with the custom trace prefix | Exact bytes before the ordinary marker |

The fixture verifies exit zero, the exact hook receipt and the independent command marker. The ordinary marker records the selected Bash executable and executor home. The command requires non-login mode and absence of the app-server key. The prefix expansion writes a separate marker only if the ordinary marker does not yet exist, including as a dangling symlink. This proves the additional effect before the approved command writes its ordinary marker, after the approved command has enabled tracing. All controls omit the custom trace prefix. Diagnostics log fixed outcome names. Trace evidence comes from the synthetic local model exchange and establishes no credential disclosure or general redaction result.

Runtime environment assembly and actual admission remain with [#184](https://github.com/realkarych/kora/issues/184). Complete declarations and fail-closed Policy enforcement remain with [#549](https://github.com/realkarych/kora/issues/549) and [#199](https://github.com/realkarych/kora/issues/199).

## Reproduction

Executable revision `c7644e01ea26706462415529defdf344b499acec`, Codex provider tree `1165147f8a474b7562b138c363519e123674bdae`, fixture tree `d6341434b6dd3cbabffb66a058894a248c34140b`. The build verifies the [official 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` with Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Run from the checkout with GNU `timeout` and a local Docker socket with Buildx. The measured environment used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The build can fetch pinned inputs. Execution uses nonroot containers with `--network none`, without host mounts, with a synthetic API key and attempt-local executor bearer. Model traffic stays on loopback. The existing case, cleanup and attach deadlines are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bash-trace
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bash-startup
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
```

| Mode | Cases | Direct children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Selected Bash trace | 4 | 8 | 4 | 0 | 34.44 s |
| Selected Bash startup | 2 | 4 | 2 | 0 | 15.91 s |
| Selected launch regressions | 4 | 8 | 4 | 0 | 29.97 s |

All modes closed their owned streams and local model server. Probe-container inventory was empty before and after each mode. The combined stdout/stderr log SHA-256 values in table order were `752d8f9a99c23f80563f04733e2c49cbb0d27fd7d7c4791d23c98faf0c024347`, `89b1dea6a454f889ba4f018cf5d59067dc9e7e425756804dc41c1530279d707a` and `c24de7403d2c3ff598aafed9a1cc92c3a21d3ea67181f03da33cc2d7ddae8326`.
