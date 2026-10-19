# How the MVP harnesses let a host intercept tool calls

Checked on 2026-10-16 against the vendors' documentation and, for Codex, the `openai/codex` repository on its default branch. This note records the facts behind the tool-policy coverage matrix in [protocols.md](../specs/protocols.md) section 12 and the amendment of [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md). It answers one question: through which mechanism can kora's session runner see every tool call a harness is about to make, and deny it, under every permission mode kora offers?

## Claude Code through the Claude Agent SDK

- `PreToolUse` hooks are callbacks passed in `ClaudeAgentOptions.hooks`. The hooks reference states that they "run before every tool call, whether or not it needs permission". A matcher selects tools by name; built-in tools are matched by their names (`Bash`, `Edit`, `Write`, `WebFetch`, `Agent` and others), MCP tools by `mcp__<server>__<tool>`, and a hook without a matcher runs for every tool call.
- A hook returns `permissionDecision: "deny"` to block the call; "if any hook returns `deny`, the operation is blocked regardless of other hooks", and the precedence is deny over defer over ask over allow.
- Permission modes do not bypass hooks or deny rules: "Deny rules block in every mode, including `bypassPermissions`"; `dontAsk` auto-denies every call that would prompt and still runs "calls approved by a `PreToolUse` hook".
- Hooks fire inside subagents: the hook input carries `agent_id` and `agent_type` when it runs in a subagent.
- Conclusion for kora: on the `claude` adapter the `PreToolUse` callback covers shell commands, file edits, MCP tool calls, web tools and subagent tool calls in every kora permission mode (`plan`, `accept_edits`, `dont_ask`, `bypass`).
- Refined on 2026-10-19 by the [Claude Agent SDK probe](claude-agent-sdk-probe-2026-10-19.md): the callback runs before every call the harness does not reject itself; a tool's own input validation and a settings deny rule blocked calls before it, and denies were observed under `bypassPermissions` only, for shell and file-edit calls and a tool on an in-process SDK MCP server. Stdio and HTTP MCP servers, the other permission modes, subagent calls and web tools rest on the documentation above and on the [documented controls](claude-code-documented-controls-2026-10-19.md) of the same day.

Sources: Agent SDK hooks (<https://code.claude.com/docs/en/agent-sdk/hooks>), Claude Code hooks reference (<https://code.claude.com/docs/en/hooks>), permission modes (<https://code.claude.com/docs/en/permission-modes>).

## Codex through the app-server

### Approval requests are not an interception point

- The app-server raises `item/commandExecution/requestApproval` for a shell command and `item/fileChange/requestApproval` for proposed edits; both are sent only when the agent needs to leave the sandbox. Under `sandbox_mode = workspace-write` "the agent can read files, edit within the workspace, and run routine local commands inside that boundary" without stopping for confirmation; `codex-rs/core/src/safety.rs` auto-approves a patch inside the writable roots under `on-request`, `never` and granular policies.
- `item/permissions/requestApproval` is the request of the built-in `request_permissions` tool for additional network or filesystem access, not a notification before any tool.
- `approval_policy` accepts `on-request` ("the agent works inside the sandbox by default and asks when it needs to go beyond that boundary"), `never` ("the agent doesn't stop for approval prompts") and a granular table; the configuration reference states that "`untrusted` is unsupported, and `on-failure` is deprecated". There is therefore no policy value that routes every command or edit through an approval request.
- MCP tool calls have their own approval configuration, `mcp_servers.<id>.default_tools_approval_mode` and `mcp_servers.<id>.tools.<tool>.approval_mode` (`auto | prompt | writes | approve`), surfaced as prompts when the approval policy allows prompts; `approval_policy = never` suppresses them.

### Hooks are the interception point

- Codex has lifecycle hooks (`features.hooks`, enabled by default; `features.codex_hooks` is a deprecated alias) with the events `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`, `SessionStart` and `SessionEnd`.
- "`PreToolUse` can intercept Bash, file edits performed through apply_patch, MCP tool calls, and other local function tools"; hosted tools such as web search are not covered. The hook input carries `tool_name` ("`Bash`, `apply_patch`, or an MCP name like `mcp__fs__read`"), `tool_input`, `tool_use_id`, `session_id`, `cwd`, `turn_id`, `permission_mode` and `model`; the matcher is a regular expression over `tool_name` and its aliases (`apply_patch` also matches `Edit` and `Write`).
- A hook denies through `hookSpecificOutput.permissionDecision: "deny"` or by exiting with code 2 and the reason on stderr. `PermissionRequest` hooks, by contrast, run only when an approval is about to be requested and "don't run for commands that don't need approval".
- In the source, `run_pre_tool_use_hooks` is called from the tool dispatcher (`codex-rs/core/src/tools/registry.rs`) for every tool that provides a hook payload, before the tool runs and independently of the approval policy.
- Hooks are external commands declared in `hooks.json` or in `[hooks]` tables of `config.toml`, from the user scope (`~/.codex`), the repository scope (`<repo>/.codex`, loaded only when that layer is trusted), managed sources (`requirements.toml`, system or MDM) and plugins. "Non-managed hooks must be reviewed and trusted before they run" and trust is recorded against the hook's hash; managed hooks "are marked as managed, trusted by policy, and can't be disabled from the user hook browser"; `allow_managed_hooks_only = true` skips user, project, session and plugin hooks; `--dangerously-bypass-hook-trust` runs enabled hooks without persisted trust for one invocation.
- Conclusion for kora: on the `codex` adapter the interception point is a `PreToolUse` command hook that kora's runner installs as a managed hook of the attempt (a per-attempt `requirements.toml` with `allow_managed_hooks_only = true`, which also keeps repository hooks out). It covers shell commands, `apply_patch` edits, MCP tool calls and local function tools in every approval policy and sandbox mode; it does not cover hosted web search, which kora controls through the harness configuration and the runtime's network policy instead. How the app-server session picks up a per-attempt managed hook source has been read from the documentation and the source, not run; the first vertical slice confirms it with a real Codex binary and records the version.

Sources: app-server protocol (<https://developers.openai.com/codex/app-server>), configuration reference (<https://developers.openai.com/codex/config-reference>), sandboxing (<https://developers.openai.com/codex/concepts/sandboxing>), hooks (<https://developers.openai.com/codex/hooks>); `openai/codex` files `codex-rs/core/src/tools/registry.rs`, `codex-rs/core/src/hook_runtime.rs`, `codex-rs/core/src/safety.rs`, `codex-rs/core/src/mcp_tool_call.rs`, `codex-rs/protocol/src/protocol.rs` (`AskForApproval`, `GranularApprovalConfig`), `codex-rs/hooks/src/lib.rs`, `codex-rs/app-server-protocol/src/protocol/v2/item.rs` and `v2/config.rs` (`allow_managed_hooks_only`, `ManagedHooksRequirements`).

## Agent Client Protocol, for the adapter after MVP

The protocol lets an agent ask the client for permission through `session/request_permission` when a tool call needs it; it establishes no mandatory interception point before every call. An ACP adapter's coverage therefore depends on the agent and has to be declared per agent and verified before that adapter ships (ADR-0004).

Source: <https://agentclientprotocol.com/protocol/v1/tool-calls>.

## What changed in kora because of this note

The policy protocol's decision point is the harness `PreToolUse` hook on both MVP adapters, not an approval request; each adapter declares which call kinds its hook covers, validation rejects a required policy on an uncovered kind, and the conformance suite checks coverage per declared kind rather than "every tool call under every permission mode" ([protocols.md](../specs/protocols.md) sections 3, 12 to 14, [ir.md](../specs/ir.md) section 10, ADR-0009).
