# Selected Codex shell-routed patch Policy — 2026-11-08

[#1030](https://github.com/realkarych/kora/issues/1030) exercises a file edit submitted as `exec_command` on the selected Linux app-server and exec-server pair. All four cases use official Codex 0.166.0, one identical model call, a fresh workspace, a synthetic API key and one managed Bash hook. The model sends this command.

```bash
apply_patch <<'KORA_PATCH'
*** Begin Patch
*** Add File: kora-probe.marker
+direct patch marker
*** End Patch
KORA_PATCH
```

The loopback fixture checks the exact call identity and matches the managed hook receipt to the acknowledged thread and terminal turn. It also requires exactly one invocation and matches the native completion of that hook. The two Policy cases construct a Codex `Bash` request from the decoded input with the fixture-owned workspace and fixed node. They use the real `allow_deny` provider and empty Policy layers. The protected-path entry activates the [shell-routed patch guard](../specs/protocols.md#the-allow_deny-policy-issue-233). This case does not establish resolution of the heredoc's target path.

The driver independently checks the model-facing `function_call_output`, the native `commandExecution` and `fileChange` items through the matched terminal turn, and the workspace marker. The successful shell-routed edit result contains the nested patch exit and output fields inside the Bash function result. The outer duration is a fixed zero. The measured inner duration is checked for numeric format, independent of workload timing. The direct custom patch path has a different outer result shape. A native `fileChange` item alone cannot identify the model tool or the hook that authorized it. [Pinned shell interception](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs#L177-L207)

The Policy outcome record is published only after the exact decision and bounded private diagnostic capture have been checked and closed. The protected case requires `argument_invalid` with denial cause `rule`. Exit 1 and timeout are separate controls and require the Policy outcome record to be absent. The timeout case checks the exact receipt PID against the live barrier peer, observes helper exit and EOF while both native roles remain live, and then verifies closure of the two roles, owned streams and loopback model server. Each run removes its exact container.

| Case | Real Policy decision | Native hook outcome | Tool lifecycle through turn completion | Marker |
| --- | --- | --- | --- | --- |
| Unprotected | Allow, `allowed` | `completed` | Started and completed `fileChange` | Exact bytes |
| Protected | Deny, `argument_invalid` | `blocked`, explicit feedback | Neither observed tool family emitted an item | Absent |
| Hook exit 1 | No decision | `failed`, exact exit error | Started and completed `fileChange` | Exact bytes |
| Native hook timeout | No decision | `failed`, exact five-second timeout | Started and completed `fileChange` | Exact bytes |

No `commandExecution` item was observed in the three cases that wrote the marker.

## Reproduce

Run from the repository checkout with a local Docker socket, Buildx, GNU `timeout`, uv and CPython 3.11.17. The build can fetch pinned assets and the existing locked conformance wheels. Each case runs in a nonroot Linux amd64 container with `--network none` and a loopback-only model. The existing case, cleanup and Docker attach deadlines are 30, 5 and 75 seconds. The measured environment was macOS arm64 with Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-shell-patch-policy
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-hook-failures
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-patch-hook-failures
```

The [official release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) resolves to `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The build checks the Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978` and executable version. The Python image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.

The tested kora revision was `2b5968544259cf8abb1dc1f3dd64cdfeaf9695dc`. The source tree for the locked image was `85dafe989d4c4920a47d52c2164cb2729bc765b0` and the fixture tree was `f55d59214f7e626f8030f3947895b485d397fc5a`. The `uv.lock` blob was `8ef2bf841b99cedbb0fda606cbf2687cb57e3f97`.

| Mode | Cases | Direct native children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Shell-routed patch Policy | 4 | 8 | 4 | 0 | 46.22 s |
| Bash hook failures | 4 | 8 | 4 | 0 | 35.92 s |
| Direct patch hook failures | 4 | 8 | 4 | 0 | 35.44 s |

The three native timeout helpers settled independently. The before and after inventories were empty. The combined stdout/stderr log SHA-256 values in table order were `53408c2c59296e68dee02b776db1916aaa0aea9da644a427db4e407afcce8def`, `19309590424374c9ae4fd5bcb3bdb04b5616452b4583a4156437ceee773049d8` and `f1f475d69f1ba0e28eb609c80b486bfc3d29fae7071ad76ea62dd3a12a74fb1c`.

The first fixture revision `b3eacfe` rejected the successful allow case because its expected function result omitted the nested patch exit and output header. It reaped both children and removed the container. The log SHA-256 was `f54ed1c5b975320ec45c68650f3ea734f42b4350eb0259ad9a8af59e7ef8e083`. A separate bounded scratch capture of the fixed response confirmed the nested header and supplied the corrected exact expectation. The scratch log SHA-256 was `3638b55ada0b80ff2d08fc679c4e575be1c5d05d4a980c9e3a5253c1b4c84fdc`. The first complete run at `5ef827f` passed with the measured inner duration at zero. Independent review found that a valid edit can report nonzero duration and that capture thread startup failure needed a fixed refusal. The final matrix above used the corrected fixture. The five local duration regression cases cover whole and fractional nonzero values, zero and malformed values.

## Evidence limits

The fixture exercises fixed local inputs and observes terminal file effects. It does not trace the underlying filesystem requests, establish a runtime execution fence, or authenticate an effect against the hook decision. Required Policy activation, complete coverage declarations and runtime domain settlement retain their existing gates. [Required tool policy activation](../specs/codex-execution-profile.md#required-tool-policy-activation)
