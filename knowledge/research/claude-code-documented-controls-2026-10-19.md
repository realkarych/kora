# Claude Code controls the `claude` adapter relies on, as documented

Checked on 2026-10-19 against the Claude Code documentation at code.claude.com, for issue #125. These are documented statements, not observations. The [Claude Agent SDK probe](claude-agent-sdk-probe-2026-10-19.md) of the same day ran real sessions; this note covers what the probe did not run and the [protocols.md](../specs/protocols.md) section 3 defaults mark as not yet observed. Each statement names its page; the Claude adapter contracts in protocols.md section 13 verify the ones kora depends on per tested version.

## Environment variables

From the environment-variable reference (`/docs/en/env-vars`):

| Variable | Documented effect |
|---|---|
| `ENABLE_CLAUDEAI_MCP_SERVERS` | `false` stops Claude Code from fetching the claude.ai MCP servers of a logged-in user |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1` disables auto memory |
| `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` | `1` disables all background task functionality, including `run_in_background` on Bash and subagent tools and auto-backgrounding |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | any non-empty value disables nonessential traffic: auto-updates, telemetry, error reporting |
| `DISABLE_AUTOUPDATER` | `1` disables automatic background updates |
| `CLAUDE_CODE_MAX_RETRIES` | overrides the number of retries of failed API requests (default 10) |
| `ANTHROPIC_BASE_URL` | overrides the API endpoint, to route requests through a proxy or gateway |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | the proxy servers for network connections and the hosts that bypass them |
| `CLAUDE_CODE_SHELL_PREFIX` | a command prefix wrapping the shell commands Claude Code spawns, including Bash tool calls, hook commands and stdio MCP server start commands |
| `CLAUDE_CODE_OAUTH_TOKEN` | an OAuth access token for claude.ai authentication in SDK and automated environments |

The settings reference (`/docs/en/settings-reference`) adds that `disableClaudeAiConnectors: true` in any settings file and `ENABLE_CLAUDEAI_MCP_SERVERS=false` each turn the claude.ai connectors off, and that servers passed with `--mcp-config` are unaffected.

## Hooks and permissions

From the hooks reference (`/docs/en/hooks`) and the permissions page (`/docs/en/permissions`):

- All matching hooks run in parallel. When hooks disagree, the precedence is `deny` over `defer` over `ask` over `allow`.
- `updatedInput` in a `PreToolUse` result replaces the whole tool input before the tool runs. Permission rules are evaluated against the input the hook returns, not the input the model sent.
- `PreToolUse` hook decisions do not bypass permission rules. Deny and ask rules are evaluated whatever the hook returns, so a hook's `allow` does not override a matching deny rule.
- A `PreToolUse` `allow` skips the permission prompt. In plan mode it has no effect on a file edit or another call that is not read-only, apart from `ExitPlanMode`.
- `dontAsk` denies every call that would otherwise prompt. Calls that need no approval, and tools pre-approved through `permissions.allow` rules, still run.
- `disableAllHooks: true` in a settings file disables hooks. The value left after settings precedence applies counts, so a project `.claude/settings.json` can set it. The documentation does not say whether it affects Agent SDK callback hooks.

## Settings reload

From the settings page (`/docs/en/settings`):

- Claude Code watches its settings files and reloads them when they change, so edits reach the running session, including edits to `permissions`, `hooks` and `apiKeyHelper`.
- A settings file created mid-session is loaded if its folder existed when the session started. A project `.claude/` folder's file is loaded even when the folder itself is created during the session.
- The reload covers user, project, local and managed settings.

## Relation to the specification

- These statements back the "not yet observed" items of the `claude` defaults in protocols.md section 3: the disabling variables, the reserved proxy variables, and the claude.ai connector exclusion.
- They also back the `trust_repo_config: true` residual of section 12 and [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md): repository hooks can rewrite input, a project setting can disable hooks, and settings written during a session are reloaded.
- The plan-mode and `dontAsk` rules decide what the callback's explicit allow achieves in each permission mode. Whether a callback allow lets a call run under `dontAsk` is not stated directly and is not yet observed.
