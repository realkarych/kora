# Codex provider credential handoff probe

Observed on 2026-11-04 (Europe/Moscow) for [#681](https://github.com/realkarych/kora/issues/681). The executable fixture tree at `tests/plugins/harness/codex` was `ab1d3a3988cf1107c9e13a9e2e3a3b8c8fb8f04e`, based on kora `7f439d31b50637442b3ad2a67c9a5e66ae5d2532`.

## Reproduce

Use the local Docker Engine and Buildx prerequisites from the [managed hook probe](codex-managed-hook-probe-2026-11-04.md). The probe uses `codex-cli 0.166.0` from upstream commit `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` and the Linux amd64 musl archive with SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-provider-env-key
sh tests/plugins/harness/codex/run_hook_probe.sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-cleanup
sh tests/plugins/harness/codex/run_hook_probe.sh --check-write-stdin
```

The new command runs the account control in one container and the two explicit-provider sessions in another. The launcher confirms removal between containers. Each session has a fresh home and Codex state directory, strict configuration and a local file credential store. The synthetic value is supplied through the constructed child environment only in the applicable sessions. The containers use loopback-only networking, no host mounts and unprivileged execution. The fixture exports case status only and discards the entire native state tree, including native logs.

## Observations

All four commands exited zero under Docker Desktop 29.9.2 and Buildx 0.38.1. The Linux amd64 containers ran through emulation on an aarch64 LinuxKit 7.0.14 VM on Apple Silicon.

| Case | Observed result |
| --- | --- |
| Built-in OpenAI provider with synthetic `CODEX_API_KEY` | The correlated `account/read` with refresh disabled returned explicit `account: null` and `requiresOpenaiAuth: true`. The probe issued no inference turn or login. |
| Explicit loopback provider with `env_key = "CODEX_API_KEY"` and the variable present | The fixture accepted exactly one connection and one `POST /v1/responses` with a single expected synthetic Bearer header. The matching turn in the ephemeral thread completed. |
| Same explicit provider with the variable absent | The matching turn in the ephemeral thread failed with the native missing-variable message for `CODEX_API_KEY`. The fixture accepted zero connections through app-server reaping. |
| Original probes | Direct Bash allow and deny, stalled-hook cleanup and the interactive versus managed one-shot stdin comparison passed. Docker confirmed removal of every probe container. |

The known `auth.json` store was absent before and after each credential case. The fixture checks the synthetic header in memory, rejects duplicate and alternate credential headers, and retains neither headers nor request data in the evidence.

## Scope

The pinned [app-server startup](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/lib.rs#L599-L603) disables implicit loading of `CODEX_API_KEY` by the account manager. The [provider environment lookup](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/model-provider-info/src/lib.rs#L518-L536) is separate and resolves the key before transport. The first observation concerns only the built-in provider's account state. The second and third establish the explicit provider handoff and refusal on the pinned Linux fixture.

Production endpoint selection, credential provisioning, authentication at a real provider and complete native declarations retain their own acceptance evidence. The [DD-30 mapping](../specs/protocols.md#14-harness-mapping-tables-fr-33-dd-27) owns the credential name, and the [validation facts contract](../specs/validation-facts.md#10-native-codex-declarations) owns the completeness and selection boundaries.
