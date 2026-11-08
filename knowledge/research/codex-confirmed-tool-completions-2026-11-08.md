# Confirmed Codex tool completion handoff — 2026-11-08

[#1034](https://github.com/realkarych/kora/issues/1034) adds a private handoff for each admitted completion notification from the existing Codex turn session. The receiver sees the bounded thread, turn and item identifiers, notification phase, native item family and status, and optional command source and exit code. The session's decoder omits arguments, command text, paths, patch data, output and model context. The native phase and status remain independent.

An observation received between the start write and its matching acknowledgement remains staged. Once the session confirms the combined snapshot, it hands off the staged message and tool records in receipt order with consecutive message ordinals. Notices consumed after an early terminal while an acknowledgement is outstanding stay within the existing receive window. The session performs no post-return drain. Usage coalescing and callers without the new receiver keep their existing behavior.

The existing tool admission allows one completion per admitted item. Staging holds at most one additional tool record per admitted item. Each record has at most 768 UTF-8 bytes of identity text plus fixed-shape scalar data and object overhead. The existing message count and 1 MiB staging byte limits stay independent. Admission uses an amortized constant-time append and flush work grows linearly with the number of staged records. Exact admission, ordering and owned reference release are blocking checks. Wall time and process memory remain diagnostic.

The session clears staged observations after acknowledgement and on failure. It drops its borrowed receiver on exit. The reducer and current snapshot can retain the latest admitted observation. Any data retained by the receiver or its own exception traceback has a separate owner. The callback is a trusted synchronous consumer and must stay bounded and nonblocking. Deadline checks bracket delivery. Failure or expiry preserves the already invoked prefix.

## Native evidence

The fixture compares the private callback against the independent raw `commandExecution` and `fileChange` observer through the matched turn. Both selected patch families use the session under test. The model-facing result, managed hook, marker and cleanup remain separate checks. Native file-change metadata can originate from a Bash heredoc or a direct patch. It supplies neither the original Policy tool identity nor authorization. [Shell-routed patch record](codex-shell-routed-patch-policy-2026-11-08.md)

The selected fixed exit-7 command writes the expected marker and then returns 7. The native command completion reports `failed` and exit 7. The terminal turn still reports `completed`. This case separates an individual tool result from the turn outcome and the actual effect.

| Mode | Verified cases | Typed versus raw comparisons | Selected direct children reaped | Exact containers removed | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Selected launch | 5 | 2 | 10 | 5 | 0 | 39.27 s |
| Shell-routed patch Policy | 4 | 4 | 8 | 4 | 0 | 45.51 s |
| Direct patch hook failures | 4 | 4 | 8 | 4 | 0 | 35.57 s |
| Selected turn events | 2 | 0 | 4 | 2 | 0 | 23.38 s |
| Legacy PreToolUse controls | 5 | 5 | 0 | 4 | 0 | 21.51 s |

The 15 independent comparisons saw ten matching completions and five exact empty sequences on refusals. The two selected timeout helpers settled independently. The selected turn regression retained confirmed message and usage data on a transport cut without publishing a result. The legacy Bash allow and deny share one container. All before and after container inventories were empty.

The combined stdout/stderr log SHA-256 values in table order were `3bbfa6e8c945f402aa08961c3b2131b2cc3ae96c2d9bfd9eb56cfd3e31be431d`, `093f6459782b1891addc2ff23980c5117338574f1a2bff6cdc7943d714f4fea7`, `5bbf7b8f6d3a86d0e64f4ac323187a94db2851246e4f275c2139288d1f8fa1ed`, `2454cf46264e6bc6babb580f32156dcb3a04824d356236c1254799f21e393bdf` and `077e2ad12781bded801bc45e819cc8af49bc0a319f3b769c2c5592177cddf577`.

## Socketpair and static checks

The focused Codex, Policy and architecture selection passed 954 tests. The real socketpair cases cover mixed early and late publication, a consumed completion after an early terminal, cutoff after return, independent phase/status, nullable metadata, and omission of the raw payload canary. Wrong request ID, wrong turn, malformed acknowledgement, remote error, exact deadline, transport cut, cancellation, duplicate and overflow each leave a staged completion unpublished. Callback-driven expiry preserves the delivered prefix and releases a superseded staged record under a retained failure. A separate check releases an earlier staged record while the turn is still open, before a post-acknowledgement duplicate. Reuse refusal drops the borrowed callback while the refusal remains held.

The first local draft used a relative value where the RPC uses an absolute event-loop deadline. The corrected fixture supplies the loop deadline and advances its injected clock to that exact value. The focused rerun and final selection passed. Ruff format and lint, strict Pyright, lock and shell syntax checks passed on macOS arm64, CPython 3.11.17.

## Reproduce

Run from a repository checkout with a local Docker socket, Buildx and GNU `timeout`. The two locked package modes also require uv and CPython 3.11.17. The build can fetch the checked official Codex release and locked assets. The cases run in isolated Linux amd64 containers with the pinned 0.166.0 binary, synthetic API key and loopback Responses fixture. The case, cleanup and Docker attach deadlines remain 30, 5 and 75 seconds.

The tested revision was `23b3b186e337cb8e49e1f977284f3c04f640bdd6`, with source tree `1d78ddbfb6131f8e7f406c4be36c105d0df2b172`, fixture tree `6d09365ec608aab08a5c9749239900012faa3395` and lock blob `8ef2bf841b99cedbb0fda606cbf2687cb57e3f97`. The measured environment was macOS arm64 with Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14. The official pin, archive digest and Python manifest match the [selected patch fixture](codex-shell-routed-patch-policy-2026-11-08.md#reproduce).

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-shell-patch-policy
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-patch-hook-failures
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-turn
sh tests/plugins/harness/codex/run_hook_probe.sh --check-pre-tool-use
```

## Evidence limits

The handoff supplies private observational data for later result mapping. The authenticated decision ledger, normalized result and event count remain with the subsequent adapter and [runner host](https://github.com/realkarych/kora/issues/199). A native completion is neither before-effect authority nor proof that the execution domain is closed. [Selected profile activation](../specs/codex-execution-profile.md#required-tool-policy-activation)
