# Scripted Claude session on the pinned SDK and bundled CLI

Checked on 2026-11-08 for [issue #980](https://github.com/realkarych/kora/issues/980). This note records what a local macOS run showed for the first scripted Claude session. It does not record a Linux CI result, a container network bound, or the rest of the scripted matrix in [protocols.md](../specs/protocols.md).

## What was run

The host was macOS, arm64, with Python 3.11 and `claude-agent-sdk` 0.2.168. That package bundles Claude Code CLI 2.1.301. No model provider was called. An in-process HTTP server on `127.0.0.1` answered `POST /v1/messages?beta=true` with `stream: true` and `POST /v1/messages/count_tokens`. The first Messages response was a `tool_use` stream. A follow-up request was recognized only when a JSON content block had type `tool_result`. A substring search is not enough, because the system prompt contains that word.

The session starts the bundled CLI through a private executable. That executable calls `setsid`, reads a constructed environment, and replaces the process with the CLI. The SDK's own environment merge cannot drop inherited variables, so the replacement is required. `KORA_CLAUDE_LAUNCH_DIR` is visible only to that executable. The CLI receives the constructed map, including `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY`. A non-loopback base URL is rejected before any process starts.

The tool decision is a `PreToolUse` hook. `bypassPermissions` is on, `setting_sources` is empty, and a missing or unacceptable decision denies the call. A `Write` of a new file is the decision under test. A `Write` of an existing file that the CLI has not read is rejected by the CLI before the hook, so that rejection is not a policy deny. A separate seeded file that the scripted call does not name stays unchanged.

## What held

- An allowed `Write` created the new file with the scripted bytes.
- A denied `Write` of a new file left that file absent.
- Completion reaped the CLI process tree.
- Cancellation of a `Bash` child running `perl` sleep reaped that child and did not create the output file. The Bash tool blocks a bare `sleep`; `perl` sleep is the command that reached the hook.
- A child that ignores `SIGTERM` was removed with `SIGKILL`. The signal goes to a process group only when that group is not the caller's group and a member command contains the launch directory, the working directory, or a recorded pid whose command is the bundled binary.
- The CLI environment contained the harness `HOME` and the loopback base URL. A different parent `HOME`, and a parent `ANTHROPIC_API_KEY` when one was set, did not appear.
- Sampled TCP peers were loopback. The scripted server recorded `127.0.0.1`.
- Diagnostics for the allowed run were `harness.claude.session_finished` with `outcome=completed`. They did not contain the fixture key, the prompt, the scripted bytes, or the target path. A refused base URL emitted one `harness.claude.session_failed` with `reason=launch_rejected` and started no CLI.

These checks are `test_scripted_claude_write_changes_the_file_when_allowed`, `test_scripted_claude_write_leaves_the_file_untouched_when_denied`, `test_scripted_claude_cancellation_closes_the_tool_child`, `test_scripted_claude_session_refuses_a_non_loopback_endpoint`, and `test_scripted_claude_session_kills_a_child_that_ignores_termination`.

## What this does not show

The private launcher is not the runtime launcher. No `HarnessAdapter` is registered. Events, usage, and result normalization are not implemented; those remain later changes checked against protocol recordings. Subagent calls, MCP servers, repository settings, and the container network bound are still unobserved. Linux remains the required platform proof and was not produced on this host.
