# Selected Codex SQLite state home — 2026-11-08

[#986](https://github.com/realkarych/kora/issues/986) verifies the native app-server state location on the selected Linux profile. The pure launch constructor now refuses the exact `CODEX_SQLITE_HOME` name in both workload name collections with `codex_launch_environment_conflict`. Both native role recipes retain their existing private home assignments and leave this control unset. Adjacent names remain available under the existing ownership rules.

## Pinned source and native observation

The [pinned configuration resolver](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/config/mod.rs#L4322-L4342) selects an explicit SQLite home, then a nonempty `CODEX_SQLITE_HOME`, then `CODEX_HOME`. The [app-server startup](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/lib.rs#L672-L681) initializes SQLite before serving. The [pinned state crate](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/state/src/sqlite.rs#L48-L90) locates `state_5.sqlite` under that selected directory.

| App-server environment | Regular `state_5.sqlite` with the SQLite signature | Competing state path |
| --- | --- | --- |
| Existing selected recipe | Private app-server `CODEX_HOME` | Absent under the alternate directory |
| Same recipe with an app-server-only `CODEX_SQLITE_HOME` | Alternate disposable directory | Absent under the private app-server `CODEX_HOME` |

Each case begins in a fresh fixture tree. The fixture supplies the alternate value only to the second app-server process. Both cases complete the same selected Bash call with an exact managed hook receipt and model-visible exit 0. The independent workspace marker matches the non-login Bash executable and executor home, and the command checks that the app-server key is absent from the command environment. The state check runs after both native children are reaped. It reads the 16-byte signature from the expected regular file and checks absence of the exact competing path, including dangling symlinks. Diagnostics contain fixed outcomes. The observations concern the state database on this selected Linux fixture. Production physical placement and domain closure remain with [#184](https://github.com/realkarych/kora/issues/184), Policy enforcement with [#199](https://github.com/realkarych/kora/issues/199), and complete declarations with [#549](https://github.com/realkarych/kora/issues/549).

## Reproduction

Executable revision `72c5cbeeceab5ff1e9ae4b6742275829b02eecfe`, Codex provider tree `cec0f97514b9f2cb95c9a08a7c747a6eaffbe9f6`, fixture tree `a907b72a63d60e4044869bf57db0c453096a229f`. The build verifies the [official 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` with Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

Run from the checkout with GNU `timeout` and a local Docker socket with Buildx. The measured environment used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The build can fetch pinned inputs. Execution uses nonroot containers with `--network none`, without host mounts, with a synthetic API key and attempt-local executor bearer. The model exchange is loopback. The existing case, cleanup and attach deadlines are 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-sqlite-home
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
```

| Mode | Cases | Direct children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Selected SQLite home | 2 | 4 | 2 | 0 | 17.42 s |
| Selected launch regressions | 4 | 8 | 4 | 0 | 31.33 s |

Both modes closed their owned streams and local model server. Probe-container inventory was empty before and after. The combined stdout/stderr log SHA-256 values in table order were `cd2c5c704645361aba4e19a30716d3558b498fc076c8c233d2bf332fc18e0fe5` and `564f9209284bf6e1f033c94f32f13d2f9c49b9438373772aadbb3a7bebf853da`. A preceding discovery run at `fb9a1117662a133574130787e9e9507e2b464c60` confirmed the same two placements before the builder change.
