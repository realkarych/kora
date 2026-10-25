# Codex `PreToolUse` tool input, as the source builds it

Checked on 2026-10-23 against the `openai/codex` repository at commit `970111f6cb62cc02fe36a82ea7d71b37a2889c97`, for issue #233. These statements are read from the source and have not been observed against a live harness; work item 15 confirms them per tested version. The [harness policy hooks note](harness-policy-hooks.md) records how the hook is installed and what it covers; this note records what the `allow_deny` policy receives from it.

## The shell tool

- `exec_command` builds its hook payload in `codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs` (`pre_tool_use_payload`) as tool name `Bash` with `tool_input` `{"command": cmd}`.
- `cmd` is a `String` field of `ExecCommandArgs` (`codex-rs/core/src/tools/handlers/unified_exec.rs`). The hook input never carries an argv list, and no shell wrapper is added to it: the shell the command later runs under is not part of the hook input.
- The same arguments carry `shell` (a shell the model may choose), `login`, `tty`, `workdir`, `timeout_ms`, `sandbox_permissions`, `additional_permissions`, `justification` and `prefix_rule`. None of them reaches the hook input. A command therefore reaches the policy without its working directory.
- `write_stdin` returns no hook payload (`codex-rs/core/src/tools/handlers/unified_exec/write_stdin.rs`). The source states that non-empty writes "continue a command that already ran PreToolUse as Bash". Input written to a running process, a `tty` session included, is never checked by the hook.

## The patch tool

- The native `apply_patch` tool is a freeform tool. Its hook payload (`codex-rs/core/src/tools/handlers/apply_patch.rs`) is tool name `apply_patch` with `tool_input` `{"command": patch}`, where `patch` is the raw patch text.
- Its paths are resolved against the turn environment's working directory, the session's working directory. The patch text carries no working directory of its own.
- The envelope is parsed by `parse_patch` in `codex-rs/apply-patch/src/parser.rs`. It splits lines with Rust's `str::lines`, which breaks only at `\n` and drops one trailing `\r`, so other line separators stay inside a line. The streaming parser (`codex-rs/apply-patch/src/streaming_parser.rs`) takes the `Add File:`, `Delete File:` and `Update File:` paths from the trimmed line, but the `Move to:` path from the untrimmed line, so surrounding white space stays part of a move target. The whole text is trimmed, and the first and last lines must be `*** Begin Patch` and `*** End Patch` after trimming. The headers are `*** Add File: `, `*** Delete File: `, `*** Update File: ` and `*** Move to: `. An optional `*** Environment ID: ` line may follow the first line, and is refused unless the session has several environments. A lenient mode also accepts the patch wrapped in a heredoc (`<<'EOF'` … `EOF`).

## Patches through the shell

- `exec_command` passes every command to `intercept_apply_patch` (`codex-rs/core/src/tools/handlers/apply_patch.rs`) after the `Bash` hook has run. When the command is a patch invocation, Codex applies the patch itself, with no second hook and no `apply_patch` hook.
- `maybe_parse_apply_patch` (`codex-rs/apply-patch/src/invocation.rs`) accepts:
  - a direct argv `[apply_patch | applypatch, <patch>]`;
  - a shell script under `bash`, `zsh` or `sh` with `-lc` or `-c` whose only top-level statement is `apply_patch <<'EOF' … EOF` or `applypatch <<'EOF' … EOF`;
  - the same statement preceded by `cd <path> &&`, in which case the patch's paths are resolved against `<path>`.

  Text before or after that statement (`echo …;`, `… && echo done`) does not match.
- Such a patch reaches the policy only as a `Bash` call whose `command` contains `apply_patch` or `applypatch`, and its working directory, from `workdir` or from `cd`, is not known to the hook.

## Consequences for `allow_deny`

| fact | rule in protocols.md section 12 |
| --- | --- |
| `Bash` input is `{"command": string}` | the `codex` `Bash` argument is `command`, a string; no unwrap rule applies |
| `apply_patch` input is `{"command": patch}`, resolved against the session's working directory | the `codex` `apply_patch` argument is `command`, parsed as the envelope, with paths relative to `WorkspaceRef.root` |
| a shell-routed patch has no hook of its own and no known working directory | a `Bash` call containing `apply_patch` or `applypatch` is refused when protected paths or edit patterns are in force |
| `write_stdin` emits no hook | input written to a running process is outside the policy on `codex`; the adapter (work item 15) disables or bounds it |
| the model may choose the shell and a login shell for `exec_command`, and neither reaches the hook | the shell rule assumes an sh-family shell; pinning or refusing `shell` and `login` is work item 15's |
| `parse_patch` splits lines only at `\n` | `allow_deny` splits the same way and refuses marker lines with other separators |
| a `Move to:` target keeps its surrounding white space | `allow_deny` refuses a `Move to:` line with surrounding white space |
| `*** Environment ID:` may appear in a patch | `allow_deny` refuses it with `argument_invalid`, because kora runs one environment per attempt |
