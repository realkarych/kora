# Official Codex policy activation evidence — 2026-11-06

[Issue #810](https://github.com/realkarych/kora/issues/810) records the
[owner's decision](https://github.com/realkarych/kora/issues/772#issuecomment-6672311553)
to retain official Codex builds and preserve required pre-tool policy.
This follows the [selected profile](../specs/codex-execution-profile.md)
and [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)
and [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md).

The [matched release comparison](codex-managed-hook-release-comparison-2026-11-08.md)
records the same Linux Bash and direct patch effects for the selected
0.166.0 control and official 0.167.0 trial.

## What is established

The [native evidence](codex-managed-hook-failures-2026-11-06.md) verifies
the official Codex 0.166.0 Linux amd64 release through the selected
app-server and exec-server, with an offline scripted Bash call and a
synthetic local API key. An explicit blocking exit 2 with a nonempty
reason stopped the command. Exit 2 without a reason, exit 1 and a
native hook timeout produced failed hook observations and the actual
marker. The timeout helper exited while both native roles were live.
The native result, model response and marker were checked separately.

The original executable input was `b9e129f16df4412423ebb7d4130b111fdac11274`;
the fixture tree was `b896a0c5e6d6daaa91d365d2c7dd5ba45737e682`.
The evidence landed in [PR #808](https://github.com/realkarych/kora/pull/808)
as squash `0c2b63b3f93419a0814a5683b60f1a197db74ec9`. Use the original
dated record for the archive digest, reproduction,
log hashes and exact cleanup evidence.

The [selected direct patch probe](codex-selected-patch-hook-failures-2026-11-06.md)
observed a separate filesystem effect under the same official Linux profile.
Explicit denial blocked the patch. After exit 1, the managed hook reported
failure, the native file-change item completed and the patch wrote the exact
marker. The pinned source routes the direct patch through the selected
filesystem client. These cases add no evidence for other direct patch fault
modes, other tool kinds, native macOS or a different release.

| Boundary | Pinned source observation | Implication and limit |
| --- | --- | --- |
| Managed hook result | [Hook parser](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/events/pre_tool_use.rs#L200-L301) and [command timeout](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/engine/command_runner.rs#L331-L400) distinguish block and failure | The current selected profile has observed failure paths without prevention. Reporting failure is separate from a blocking decision. |
| Notification and dispatch | [Hook runtime](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/hook_runtime.rs#L224-L246) emits completion before returning continue or block; [app-server](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/bespoke_event_handling.rs#L1154-L1163) projects a notification. [Dispatch](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/registry.rs#L839-L890) proceeds on continuation and can handle rewritten input. | There is no decision acknowledgement in the notification. The client receiving it and issuing an interrupt does not fence dispatch. Input rewrite is a qualification concern, not an observed bypass. |
| Process correlation | [Selected executor process protocol](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server-protocol/src/protocol.rs#L364-L385) makes thread and tool call metadata optional and separate from the client-chosen logical process id. | Attribution is not peer, path or decision authority; a matching identifier does not prove an admitted command. |
| File edits | [Direct patch runtime](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tools/runtimes/apply_patch.rs#L172-L201) uses the selected environment filesystem; [remote selection](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server/src/environment.rs#L810-L838) supplies the remote filesystem client. [Representative file-write messages](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/exec-server-protocol/src/protocol.rs#L685-L727) lack process attribution fields. | A process-only guard supplies no proof for this path or other covered contexts. The fixture did not capture a complete inventory of file operations. |

The existing [Policy contract](../specs/protocols.md#12-policy-dd-29)
does not make normal sandbox approval requests the pre-tool decision
point. The [official stable release](https://github.com/openai/codex/releases/tag/rust-v0.166.0)
and [open fail-closed feature request](https://github.com/openai/codex/issues/41979)
were checked on 2026-11-06. An upstream issue supplies no released
feature, compatibility promise or approval to change the pin.

## Qualification still needed

Two paths remain unqualified. An official release and selected
configuration could implement a native blocking facility. Alternatively,
an independently proved supplemental runtime control could enforce the
same authenticated managed `PreToolUse` decision with the official
binaries unchanged. A runtime guard that substitutes another decision
point or admits a denied call to the tool would not satisfy the
existing contract. Neither option is implemented or selected here.

Each path must match the exact native binary/role and current peer,
attempt, selected path namespace and workspace, and admitted input
to every actual covered action. Check successful allow and explicit
deny, missing or malformed input and decision, policy error and
timeout, stale or replayed authorization, unmatched input change,
wrong peer/attempt/path, cancellation and actual closure. For an
accepted multi-operation tool call, evidence binds the admitted set
of operations; a native handle or one matching ID is not authorization.
Only the claimed kinds and contexts on the exact supported profile
qualify; no unsupported native branch inherits coverage. This preserves
the documented shell and cooperative-local residuals and adds no
guarantee about arbitrary effects of an allowed script.

The [runner host](https://github.com/realkarych/kora/issues/199) retains
Policy and first-cause enforcement, the
[launcher](https://github.com/realkarych/kora/issues/184) retains
authority and actual release, and [#549](https://github.com/realkarych/kora/issues/549)
retains selected facts and disposition. [#593](https://github.com/realkarych/kora/issues/593)
retains saved execution contracts. No provider code, new channel,
declaration, public error, production control or verification gate is
established by this record.
