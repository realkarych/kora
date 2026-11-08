# Scripted Claude session on the pinned SDK and bundled CLI

Checked on 2026-11-08 for [issue #980](https://github.com/realkarych/kora/issues/980). This note records what a local macOS run showed for the first scripted Claude session. It does not record a Linux CI result, a container network bound, or the rest of the scripted matrix in [protocols.md](../specs/protocols.md).

## What was run

The host was macOS, arm64, with Python 3.11 and `claude-agent-sdk` 0.2.168. That package bundles Claude Code CLI 2.1.301. No model provider was called. An in-process HTTP server on `127.0.0.1` answered `POST /v1/messages?beta=true` with `stream: true` and `POST /v1/messages/count_tokens`. The first Messages response was a `tool_use` stream. A follow-up request was recognized only when a JSON content block had type `tool_result`. A substring search is not enough, because the system prompt contains that word.

The session starts the bundled CLI through a private executable. That executable calls `setsid`, reads a constructed environment, and replaces the process with the CLI. The SDK's own environment merge cannot drop inherited variables, so the replacement is required. `KORA_CLAUDE_LAUNCH_DIR` is visible only to that executable. The CLI receives the constructed map, including `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY`. A non-loopback base URL is rejected before any process starts.

The tool decision is a `PreToolUse` hook. `bypassPermissions` is on, `setting_sources` is empty, and a missing or unacceptable decision denies the call. On this SDK, an exception that leaves the callback becomes a hook error. The pinned CLI then ignores that error and, under `bypassPermissions`, runs the tool. The callback therefore catches the exception, returns deny, and the session fails. The exception text is not kept. A `Write` of a new file is the decision under test. A `Write` of an existing file that the CLI has not read is rejected by the CLI before the hook, so that rejection is not a policy deny. A separate seeded file that the scripted call does not name stays unchanged.

## What held

- An allowed `Write` created the new file with the scripted bytes.
- A denied `Write` of a new file left that file absent.
- Completion reaped the CLI process tree.
- Cancellation of a `Bash` child running `perl` sleep reaped that child and did not create the output file. The Bash tool blocks a bare `sleep`; `perl` sleep is the command that reached the hook.
- A child that ignores `SIGTERM` was removed with `SIGKILL`. The signal goes to a process group only when that group is not the caller's group and a member command contains the launch directory, or a recorded pid whose command is the bundled binary. A command that merely contains the workspace path is not selected. Once a group has been signaled, it stays selected after the recorded process exits, so an unmarked member that ignores termination still receives `SIGKILL`.
- A decision callback that raised left the target file absent and failed the session. Before that catch, the same callback let the scripted `Write` create the file and the session finished as completed.
- The private launcher's shebang starts Python with `-I`. A `PYTHONPATH` module named `json` did not run. The SDK version probe invokes the launcher with `-v` and the parent environment; the launcher exits before reading the launch directory.
- The CLI environment contained the harness `HOME` and the loopback base URL. A different parent `HOME`, and a parent `ANTHROPIC_API_KEY` when one was set, did not appear.
- Sampled TCP peers were loopback. The scripted server recorded `127.0.0.1`.
- Diagnostics for the allowed run were `harness.claude.session_finished` with `outcome=completed`. They did not contain the fixture key, the prompt, the scripted bytes, or the target path. A refused base URL emitted one `harness.claude.session_failed` with `reason=launch_rejected` and started no CLI.

These checks are `test_scripted_claude_write_changes_the_file_when_allowed`, `test_scripted_claude_write_leaves_the_file_untouched_when_denied`, `test_scripted_claude_cancellation_closes_the_tool_child`, `test_scripted_claude_session_refuses_a_non_loopback_endpoint`, and `test_scripted_claude_session_kills_a_child_that_ignores_termination`.

## What this does not show

The private launcher is not the runtime launcher. No `HarnessAdapter` is registered. A later change under #1069 plays one constructed CLI 2.1.301 recording through the pinned SDK and projects session start, one turn per message id, a tool call without tool input, and a tool result. Issue #1105 projects one partial usage event per API call from that recording shape. Issue #1127 projects one content-free result for a successful end and one complete usage observation when the recording carries a single matching final model entry. The remaining result normalization remains a later change. Subagent calls, MCP servers, repository settings, and the container network bound are still unobserved. Linux CI run 42435048771 on revision 565299c passed the scripted session and was merged as 44ba09e. The commands in this note were run on the local macOS host only.
