# Claude session surface observed on 2026-11-16

Checked on 2026-11-16 with Claude Agent SDK 0.2.168 and its bundled CLI 2.1.301 on macOS (arm64). Every session used a fresh `CLAUDE_CONFIG_DIR`, `HOME` and `TMPDIR`, `setting_sources=[]`, `strict_mcp_config=True`, the `claude_code` system prompt preset, a matcherless `PreToolUse` callback, the environment of protocols.md section 3, and an API key against an in-process scripted Messages API server on `127.0.0.1`. The scripted Messages server received every model request; no model provider was configured. Whether the CLI attempted any other outbound connection was not observed: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is documented to suppress such traffic, and the network-less container lane of #1646 is to establish it. These observations supply the reviewed surface of issue #1645; the Linux values are checked by the scripted isolation test on the Linux CI job.

## Where the lists arrive

`get_server_info()` after connect carries `claude_code_version` but neither a plugin list nor a tool list. Both arrive only in the init `SystemMessage` of the first turn, which the CLI emits after the prompt is sent and before its first model request. The adapter therefore checks them there and interrupts the session on a mismatch.

## Built-in plugins

Init `plugins` was the same in every session:

```json
[{"name":"cc-plugin-agents-md","path":"builtin","source":"cc-plugin-agents-md@builtin"},{"name":"cc-plugin-plugin-authoring","path":"builtin","source":"cc-plugin-plugin-authoring@builtin"}]
```

The [probe of 2026-10-19](claude-agent-sdk-probe-2026-10-19.md), run under a claude.ai login, also listed `cc-plugin-telemetry`; vendor feature gates and the credential kind can change this list within one CLI version.

## Built-in tools depend on the model

The offered built-in tool set does not depend on the permission mode (`plan`, `acceptEdits`, `dontAsk` and `bypassPermissions` gave identical lists) but does depend on the selected model:

| `model` option | Built-in tools |
|---|---|
| unset (the CLI's default), `sonnet`, `opus`, `haiku`, `claude-opus-4-1` | the 20 names below |
| `claude-sonnet-4-5`, `claude-opus-4-5`, `claude-haiku-4-5`, `claude-sonnet-4-6` | the same 20 plus `TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate` |

The 20 names: `Task`, `Bash`, `CronCreate`, `CronDelete`, `CronList`, `Edit`, `EnterWorktree`, `ExitWorktree`, `ListAgents`, `NotebookEdit`, `Read`, `ReportFindings`, `ScheduleWakeup`, `SendMessage`, `Skill`, `TaskStop`, `WebFetch`, `WebSearch`, `Workflow`, `Write`.

With `output_format`, `StructuredOutput` is added; it is absent otherwise. `MultiEdit` was absent in every session. Init `mcp_servers` was `[]`.

Because the offered set varies with the model, the reviewed set in `kora.plugins.harness.claude.trust` is the union of all 24 observed names, and a session fails only when it offers a built-in tool outside that set. A reviewed tool that a session does not offer is not a mismatch: it cannot run. Of the reviewed tools, `Edit`, `Write` and `NotebookEdit` belong to the policy's file-edit family.

Being reviewed means only that the name is known for this version; every reviewed tool still reaches the matcherless `PreToolUse` callback and the policy by name. Several have effects outside the file-edit family and `Bash`, so neither file-edit patterns nor `protected_paths` govern them: by the Claude Code tool descriptions (not observed here), `EnterWorktree` creates a Git worktree with a branch and moves the session into it, `ExitWorktree` can remove one, `CronCreate` and `ScheduleWakeup` schedule later work, and `Workflow` starts orchestrated subagents. A namespace policy that must exclude them needs a pattern whose name matches them, such as their names, `*Worktree` or `*`; whether the first slice should not offer them at all is an open question of #1645.

## Mid-session settings writes

Under `setting_sources=[]` and `permission_mode` `dontAsk`, with the callback allowing every call:
- a `Write` and a `Bash` redirection targeting the repository's `.claude/settings.json` were both refused by the CLI itself; each appeared as a `ResultMessage.permission_denials` entry and an error result beginning "Permission to use … has been denied because Claude Code is running in don't ask mode", and the file kept its content;
- a `Bash` redirection writing `$CLAUDE_CONFIG_DIR/settings.json` succeeded, and the `PreToolUse` and `PostToolUse` command hooks it declared did not run for the following `Bash` call.

Repository and invoking-account canaries (instruction files, settings hooks, a deny rule, settings `env`, `.mcp.json`, a skill, a subagent, a command, `~/.claude.json` servers, login-shell profiles, pre-seeded `.kora/claude` and `.kora/home`, and a symlink to an account configuration inside the checkout) left no marker, reached no model request and did not appear in init. A `Bash` call saw `SHELL=/bin/bash` and `HOME` set to the per-attempt home, and no variable exported by the account's shell profile.

## Launch arguments and environment

With the `claude_code` preset and no append, the SDK passes no `--system-prompt` argument; the CLI then uses its default prompt. The arguments carried `--permission-mode dontAsk`, `--setting-sources=` and `--strict-mcp-config`, and no API key. On macOS a Python process started with an exact environment also holds `__CF_USER_TEXT_ENCODING`, which the operating system adds at process start, so the recorded environment of the executable started in place of the CLI contains that one name besides the constructed set.
