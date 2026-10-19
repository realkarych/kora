# Claude Agent SDK behaviour the `claude` adapter relies on

Checked on 2026-10-19 with real sessions, for issue #114. This record answers six questions about the Claude Agent SDK for Python that the `claude` harness adapter and the target-repository trust boundary depend on ([protocols.md](../specs/protocols.md) sections 3 and 14, [ADR-0004](../../docs/decisions/0004-harness-adapters-over-vendor-sdks.md), [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md), [harness policy hooks](harness-policy-hooks.md)). Each answer gives the prototype code path, the observed output trimmed to what matters, and how it relates to the current specification text.

## Environment and method

| Item | Value |
|---|---|
| Host | macOS 27.0.1, arm64; Python 3.11.17 in a `uv` virtual environment outside the repository |
| SDK | `claude-agent-sdk` 0.2.168 from PyPI, which bundles Claude Code CLI 2.1.301 |
| Installed CLI | Claude Code 2.1.302 at `~/.local/bin/claude` (used only where stated) |
| Model | `claude-haiku-5-5` |
| Authentication | The existing claude.ai subscription login of the local CLI; `ANTHROPIC_API_KEY` not set. The init message reported `apiKeySource: "none"`. No credential was read or printed. |
| Sessions | 11 sessions that sent a prompt (one of them failed authentication before any model call) and 3 connect-only CLI starts that sent no prompt. Reported cost at list price: about USD 0.01 in total. |

Every session ran with `cwd` set to a throwaway Git repository created by `common.make_target`. Each target repository contained canaries that reveal whether repository configuration was loaded:

| File | Canary |
|---|---|
| `CLAUDE.md` | "Always end every answer with the word PINEAPPLE-7731." |
| `AGENTS.md` (question 1 only) | "Always start every answer with the word MANGO-5521." |
| `.claude/settings.json` | `SessionStart`, `UserPromptSubmit` and `PreToolUse` command hooks that each `touch` a marker file in `markers/`; `permissions.deny: ["Read(./canary-deny.txt)"]`; `enableAllProjectMcpServers: true` |
| `.mcp.json` | stdio server `bogus-canary` whose command touches `markers/mcp-spawned` and exits 1 |
| `.claude/skills/canary-skill/SKILL.md`, `.claude/agents/canary-agent.md`, `.claude/commands/canary-cmd.md` | a project skill, subagent and slash command |

Every session also carried an SDK `PreToolUse` callback (`common.make_guard`, registered without a matcher) that denied any file path or search root outside the target repository, any Bash command not on an exact per-script allowlist, and any tool other than the probe's own MCP tool. Built-in tools were restricted with `tools=[…]` per question, and `extra_args={"no-session-persistence": None}` kept transcripts off disk. Scripts and raw logs (`logs/*.jsonl`, one JSON object per SDK message) stay with the prototype.

## 1. `setting_sources=[]` ignores the target repository's configuration

**Answer.** Yes. With `setting_sources=[]` the repository's `CLAUDE.md`, settings hooks, settings permissions, `.mcp.json` server, skills, subagents and slash commands were all absent; with `setting_sources=["project"]` every canary fired. `AGENTS.md` was loaded in neither run. Three things outside the repository were still loaded with `setting_sources=[]`: MCP servers attached to the claude.ai account, Claude Code's built-in plugins, and the user's login-shell snapshot used by the Bash tool (see "Further observations").

**Code path.** `q1_setting_sources.py`, run once with `[]` and once with `["project"]`:

```python
ClaudeAgentOptions(model=MODEL, cwd=str(repo), setting_sources=sources, tools=["Read"],
                   permission_mode="default", hooks={"PreToolUse": [HookMatcher(hooks=[guard])]},
                   extra_args=NO_PERSIST, max_turns=4)
```

The prompt asked the model to read `canary-deny.txt` and to quote the first line of every instruction file in its context. After the response the script called `client.get_context_usage()` (its `memoryFiles` lists every loaded `CLAUDE.md` or memory file with its path) and `client.get_mcp_status()`, then listed `markers/`.

**Observed.**

| Signal | `setting_sources=[]` | `setting_sources=["project"]` |
|---|---|---|
| `get_context_usage().memoryFiles` | `[]` | `[{"path": ".../q1-project/CLAUDE.md", "type": "Project", "tokens": 35}]` |
| Answer | `1. DENY-CONTENT-4242  2. NONE` | `1. DENIED  2. # Project rules (from the project CLAUDE.md)  PINEAPPLE-7731` |
| Settings deny rule on `canary-deny.txt` | not applied, file read | applied; `ResultMessage.permission_denials` lists the `Read` call |
| Markers written by settings hooks and `.mcp.json` | none | `hook-sessionstart`, `hook-userpromptsubmit`, `mcp-spawned` |
| init `mcp_servers` | `claude.ai Claude Docs` (`source: "claudeai"`, connected) | `bogus-canary` (`source: "project"`, failed) and the same claude.ai server |
| init skills, agents, slash commands | no `canary-skill`, `canary-agent`, `canary-cmd` | all three present |
| init `plugins` | `cc-plugin-agents-md`, `cc-plugin-telemetry`, `cc-plugin-plugin-authoring` (all `builtin`) | the same |

In the `["project"]` run neither the SDK `PreToolUse` callback nor the repository's `PreToolUse` command hook ran for the denied `Read` (empty callback log, no `hook-pretooluse` marker): the settings deny rule was applied before any hook. The positive control used `["project"]` instead of the SDK default because `setting_sources=None` loads every source, including the user's own settings, hooks and MCP servers on the probe machine (SDK docstring: "When `None`, all sources are loaded (matches CLI defaults)"; the transport then passes no `--setting-sources` flag).

With `tools=["Read"]` the init tool list still contained eight `mcp__claude_ai_Claude_Docs__*` tools: `tools` restricts the built-in tool set only. Every later session set `strict_mcp_config=True`, and their init `mcp_servers` then contained only the servers passed through the SDK (`[]` or the in-process `probe` server).

**Relation to the specification.** Confirms the `trust_repo_config: false` row of section 14 (`setting_sources=[]`) for every repository-level item tested. Refines it: `strict_mcp_config=True` is needed in addition, because account-level MCP servers are loaded regardless of `setting_sources` when the CLI runs under a claude.ai login. Whether account-level servers appear under `ANTHROPIC_API_KEY` authentication was not tested.

## 2. A `PreToolUse` callback sees every tool call kind and its deny prevents execution

**Answer.** Yes for every kind exercised: `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep` and a tool of an in-process SDK MCP server. Each allowed call reached the callback before running; each denied call never executed, as the side-effect checks show. This held under `permission_mode="bypassPermissions"`, the most permissive mode. The callback also ran under `default` mode (questions 1 and 3). Four refinements follow the evidence.

**Code path.** `q2_pretooluse_hook.py`. A probe policy, layered on the repository guard, denies the calls whose target contains `denied` (or, for `Grep`, the pattern `SECRET`). The MCP tool `mark` writes `markers/mcp-<name>` and records each invocation in a Python list.

```python
ClaudeAgentOptions(model=MODEL, cwd=str(repo), setting_sources=[],
                   tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
                   mcp_servers={"probe": create_sdk_mcp_server("probe", tools=[mark])},
                   strict_mcp_config=True, permission_mode="bypassPermissions",
                   hooks={"PreToolUse": [HookMatcher(hooks=[guard])],
                          "PostToolUse": [HookMatcher(hooks=[recorder])],
                          "PostToolUseFailure": [HookMatcher(hooks=[recorder])]},
                   include_partial_messages=True, extra_args=NO_PERSIST, max_turns=30, max_budget_usd=0.5)
```

A deny is returned as `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "probe policy: … Do not retry."}}`. The prompt listed fifteen calls in order.

**Observed** (callback log, one line per call, with the tool result the model received):

```text
init tools: ['Bash', 'Edit', 'Glob', 'Grep', 'Read', 'Write', 'mcp__probe__mark']   permissionMode: bypassPermissions
PreToolUse Bash  allow {"command": "touch allowed-bash.marker"}  -> (Bash completed with no output); PostToolUse Bash
PreToolUse Bash  deny  {"command": "touch denied-bash.marker"}   -> is_error=True "PreToolUse:Bash hook error: probe policy: denied bash command. Do not retry."
PreToolUse Read  allow edit-allowed.txt / edit-denied.txt         -> "1	alpha"
PreToolUse Read  deny  read-denied.txt                            -> is_error=True "PreToolUse:Read hook error: …"
PreToolUse Write allow allowed-write.txt; deny denied-write.txt
PreToolUse Edit  allow edit-allowed.txt;  deny edit-denied.txt
PreToolUse Glob  allow "*.txt" -> file list;  deny "denied*"
PreToolUse Grep  allow "needle" -> "Found 1 file notes.md";  deny "SECRET"
PreToolUse mcp__probe__mark allow {"name": "allowed"} -> "marked allowed";  deny {"name": "denied"}
ResultMessage.permission_denials: 7 entries [Bash, Read, Write, Edit, Glob, Grep, mcp__probe__mark]
side effects: allowed-bash.marker exists, denied-bash.marker absent; allowed-write.txt exists, denied-write.txt absent;
              edit-allowed.txt = "beta", edit-denied.txt = "alpha"; MCP tool invoked only with ["allowed"];
              no tool result contains SECRET-READ-9911
```

No `PostToolUse` or `PostToolUseFailure` callback fired for a denied call.

Refinements:

1. **Tool input validation runs before the hook.** In a session for question 5 the Bash tool rejected `sleep 30` on its own with `<tool_use_error>Blocked: standalone sleep 30. To wait for a condition, use Monitor with an until-loop …`, and the `PreToolUse` callback was never called for that call. Calls the harness rejects by itself therefore never reach the policy.
2. **Settings deny rules run before the hook** (question 1): the denied `Read` produced neither a callback invocation nor a command-hook marker, only a `permission_denials` entry. If the adapter derives `disallowed_tools` from the policy as a fast path, those denials will most likely bypass the callback in the same way (inferred, not run). The `permission_denied` event therefore has to come from `ResultMessage.permission_denials` (or from the tool results) as well as from the callback.
3. **Structured output is a tool call.** With `output_format` set, the CLI adds a synthetic `StructuredOutput` tool even when `tools=[]`, and the `PreToolUse` callback sees it (question 3). A policy that denies unknown tools by default would break structured output.
4. **Shape of a deny.** The model receives a `tool_result` with `is_error: true` and the content `PreToolUse:<Tool> hook error: <reason>`. The denied call also appears in `ResultMessage.permission_denials` as `{tool_name, tool_use_id, tool_input}`.

**Relation to the specification.** Confirms section 3 `policy_coverage` for `shell`, `file_edit` and `mcp_tool`, and the section 14 `tool_policy` row: the callback decides every call and its deny holds in `bypassPermissions`. The `subagent` and `hosted_tool` kinds were not exercised. Refinements 1 to 3 add facts that section 14 does not state.

## 3. Structured output against a JSON Schema

**Answer.** It is requested with `output_format={"type": "json_schema", "schema": …}`. The SDK passes the schema to the CLI as `--json-schema <json>`, and the CLI exposes a `StructuredOutput` tool whose input the CLI validates against the schema. A conforming answer arrives as a parsed object in `ResultMessage.structured_output`, and the same JSON appears as text in `ResultMessage.result`. When the model cannot conform, each failed attempt returns a validation error to the model as a tool result. In the observed run the model gave up after two attempts and the session ended with `subtype: "success"`, `is_error: false`, `structured_output: null` and prose in `result`.

**Code path.** `q3_structured_output.py valid|impossible`, with `setting_sources=[]`, `tools=[]`, `strict_mcp_config=True` and the guard allowing `StructuredOutput`:

```python
VALID_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "population_millions": {"type": "number"},
                "landmarks": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
                "required": ["city", "population_millions", "landmarks"], "additionalProperties": False}
IMPOSSIBLE_SCHEMA = {"type": "object", "properties": {"code": {"type": "string", "minLength": 5, "maxLength": 3}},
                     "required": ["code"], "additionalProperties": False}
ClaudeAgentOptions(..., output_format={"type": "json_schema", "schema": schema}, max_turns=12, max_budget_usd=0.3)
```

**Observed.**

```text
valid:      init tools: ['StructuredOutput']
            tool_use StructuredOutput {"city": "Paris", "population_millions": 2.1, "landmarks": ["Eiffel Tower", "Louvre Museum", "Notre-Dame Cathedral"]}
            tool_result "Structured output provided successfully"
            result: subtype=success is_error=False num_turns=2 stop_reason=tool_use terminal_reason=completed
            structured_output: {"city": "Paris", "population_millions": 2.1, "landmarks": [...]}
impossible: tool_use StructuredOutput {"code": "HELO"}
            tool_result is_error=True "Output does not match required schema: /code: must NOT have more than 3 characters (got 4), /code: must NOT have fewer than 5 characters (got 4)"
            tool_use StructuredOutput {"code": "HELLO"}
            tool_result is_error=True "Output does not match required schema: /code: must NOT have more than 3 characters (got 5)"
            assistant text: "I couldn't get a valid value accepted. The schema requires `code` to be at least 5 characters and at most 3 ..."
            result: subtype=success is_error=False num_turns=4 stop_reason=end_turn terminal_reason=completed
            structured_output: null
```

The CLI binary also defines a result subtype `error_max_structured_output_retries` ("no attempt produced a surviving valid output") and an environment variable `MAX_STRUCTURED_OUTPUT_RETRIES`. They were found as strings in the binary; this run did not reach that limit, and the default retry count was not determined.

**Relation to the specification.** Confirms the section 14 `output_schema` row. Refines the section 3 first-slice default "taken from `ResultMessage.structured_output`, else from the final text parsed as JSON, else the attempt fails `artifact_invalid`". When the schema is not met, the result reports success and `structured_output` is `null`. The text fallback must therefore validate the parsed JSON against the schema, or be dropped for this adapter. Otherwise a final text containing non-conforming JSON would be accepted. `error_max_structured_output_retries` also needs a mapping, presumably to `artifact_invalid`.

## 4. The usage report

**Answer.** Usage arrives in three places with different meanings:

| Source | Content | Scope |
|---|---|---|
| `StreamEvent` `message_delta` (needs `include_partial_messages=True`) | `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens_details.thinking_tokens`, `iterations` | final counts of one API call |
| `AssistantMessage.usage` | the same fields as `message_start` | snapshot at the start of the API call: `output_tokens` is not final |
| `ResultMessage.usage` | the token fields plus `cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, `server_tool_use`, `service_tier`, `speed`, `inference_geo` | sum over the API calls of this query; `iterations` holds only the last call |
| `ResultMessage.model_usage[model]` | `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `thinkingTokens`, `webSearchRequests`, `costUSD`, `costBasis: "list"`, `canonicalModel`, `provider`, `contextWindow`, `maxOutputTokens` | cumulative over the CLI process (every query of the client) |
| `ResultMessage.total_cost_usd` | USD | cumulative over the CLI process |

Reasoning tokens are reported separately as `thinking_tokens` and are included in `output_tokens`. On the first call of the run below, 122 of the 212 output tokens were thinking. `claude-haiku-5-5` produced a thinking block without any thinking option being set. Cache read and cache creation are separate fields, and cache creation is further split by 5-minute and 1-hour lifetime.

**Code path.** The `q2_pretooluse_hook.py` run (16 API calls, `include_partial_messages=True`) and the two queries of the `q5_interrupt.py interrupt` run, both analysed from `logs/*.jsonl`.

**Observed.**

```text
q2, 16 API calls:
  sum of message_delta usage:              input 32  output 2174  cache_read 109998  cache_creation 9147
  ResultMessage.usage:                     input 32  output 2174  cache_read 109998  cache_creation 9147  thinking_tokens 122
  sum of AssistantMessage.usage per call:  input 32  output 249   cache_read 109998  cache_creation 9147
  first call: message_start output_tokens 6 -> message_delta output_tokens 212, thinking_tokens 122
  model_usage: {"claude-haiku-5-5": {"outputTokens": 2174, "thinkingTokens": 122, "costUSD": 0.00402, "costBasis": "list", ...}}
q5, two queries on one ClaudeSDKClient:
  query 1 (interrupted): usage.output 92;  model_usage.outputTokens 92;  total_cost_usd 0.000245
  query 2:               usage.output 6;   model_usage.outputTokens 98;  total_cost_usd 0.000337
```

An interrupted query still ends with a `ResultMessage` that carries usage and cost (question 5). A session closed with `disconnect()` emits no `ResultMessage`, so usage after a kill exists only as the `message_delta` events received before it. The subscription login also produced one `RateLimitEvent` (`rate_limit_type: "five_hour"`) in every authenticated session, and `SystemMessage` frames with subtype `thinking_tokens` (`estimated_tokens`) appeared while the model was thinking.

**Relation to the specification.** Refines the section 14 "usage source" row and section 3 `reports_usage_per_turn`. Per-turn usage requires `include_partial_messages=True` and the `message_delta` events. Summing `AssistantMessage.usage` undercounts output tokens: 249 instead of 2174 in this run. `model_usage` and `total_cost_usd` accumulate over the client's lifetime while `usage` is per query, so the adapter must not mix them. Under one query per session the two scopes coincide. The behaviour across a `resume` was not tested. The cost basis named in section 14 is present as `costBasis` (`"list"` here). Subagent usage was not exercised.

## 5. Interruption

**Answer.** `ClaudeSDKClient.interrupt()` stops a running tool almost at once. The control request was acknowledged in 3 ms. The tool result and a `ResultMessage` with `subtype: "error_during_execution"`, `is_error: true` and `terminal_reason: "aborted_tools"` followed within about 10 ms. The tool's child process was gone by then, and the same client accepted a new query afterwards. Closing the client mid-tool with `disconnect()` instead ends the stream at once without any `ResultMessage`, and takes about 5.5 s because the CLI does not exit on stdin EOF while a tool runs, so the SDK terminates it.

**Code path.** `q5_interrupt.py interrupt|disconnect`, with `tools=["Bash"]`, `permission_mode="bypassPermissions"` and a guard allowing only `perl -e 'sleep 30'`. A foreground `sleep 30` was refused by the Bash tool itself (question 2, refinement 1). Three seconds after the callback allowed the call, the script listed the CLI's descendant processes, then called `interrupt()` or `disconnect()`.

**Observed.**

```text
interrupt:
   1.680  PreToolUse allowed long command
   4.737  process tree under cli: [(62040, '/bin/zsh -c source ~/.claude/shell-snapshots/snapshot-zsh-…'), (62041, 'perl -e sleep 30')]
   4.737  calling client.interrupt()
   4.740  interrupt() returned after 0.003s
   4.745  UserMessage tool_result is_error=True "The user doesn't want to proceed with this tool use. The tool use was rejected …"
   4.745  UserMessage TextBlock '[Request interrupted by user for tool use]'
   4.747  ResultMessage subtype=error_during_execution is_error=True stop_reason=tool_use terminal_reason=aborted_tools
          num_turns=3 cost=0.00024458 usage.output_tokens=92 errors=['[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use']
   4.791  sleep pids [62041] alive after result: [False]
   5.629  second query: ResultMessage subtype=success result='AFTER'
   6.199  disconnect() took 0.536s; cli alive=False
disconnect:
   4.665  calling client.disconnect() mid-tool
   4.665  receive_messages iterator ended            (no ResultMessage; last messages: init, AssistantMessage tool_use, RateLimitEvent)
  10.220  disconnect() returned after 5.555s; cli alive=False
  10.220  sleep pids [62130] alive right after disconnect: [False]
```

The SDK's `SubprocessCLITransport.close()` closes stdin, waits up to 5 s for exit, sends SIGTERM, waits another 5 s and then sends SIGKILL. The 5.555 s therefore consists of the 5 s wait followed by SIGTERM. The CLI's tool child had exited as well.

**Relation to the specification.** Refines the section 3 first-slice default "termination is interrupt, 30 s grace, then kill" and the section 14 `limits.timeout` row. The interrupt path takes milliseconds and ends with a `ResultMessage` carrying usage, so usage after an interrupt can be `complete`. The kill path yields no `ResultMessage`, so usage is `partial`, from `message_delta` events. The SDK adds its own 5 s + 5 s escalation inside `disconnect()`, so the runner's kill after the grace period should signal the CLI process directly. Waiting on `disconnect()` alone adds up to 10 s. The stop reason row should also read `terminal_reason` (observed: `completed`, `aborted_tools`, `api_error`; documented in the SDK: `aborted_streaming`, `max_turns`), because `subtype` alone does not distinguish an interrupt from other execution errors.

## 6. SDK and CLI versions, and how the SDK finds the CLI

**Answer.** `claude-agent-sdk` 0.2.168 ships its own CLI binary (`claude_agent_sdk/_bundled/claude`, version 2.1.301, recorded in `claude_agent_sdk._cli_version.__cli_version__`). `SubprocessCLITransport._find_cli()` returns the bundled binary first. It falls back to `shutil.which("claude")` and then to fixed locations only when no bundled binary exists. The installed CLI 2.1.302 on `PATH` was therefore ignored until it was passed as `cli_path`. The running version is reported as `claude_code_version` in the init message, and already in `get_server_info()` right after `connect()`, before any prompt.

**Code path.** `q6_versions.py`: prints the versions and `_find_cli()`, then opens a session with `cli_path="~/.local/bin/claude"` (absolute path in the script) and sends one prompt.

**Observed.**

```text
claude_agent_sdk.__version__: 0.2.168
bundled __cli_version__: 2.1.301
_find_cli() without cli_path: …/site-packages/claude_agent_sdk/_bundled/claude
shutil.which('claude'): ~/.local/bin/claude          (claude --version: 2.1.302 (Claude Code))
server_info keys include: claude_code_version, capabilities, commands, agents, models, pid, …
with cli_path: init {"claude_code_version": "2.1.302", "apiKeySource": "none", "permissionMode": "default", "tools": [], "mcp_servers": []}
without cli_path (all other sessions): init claude_code_version "2.1.301"
```

**Relation to the specification.** Confirms that the installed version is available from the SDK's init message (section 3 `installed_version()`, section 14 "installed version" row) and refines it: `get_server_info()["claude_code_version"]` provides it at connect time. Refines section 3 "spawns the pinned `claude` binary" and ADR-0004 "harness versions are fixed per installation": unless `cli_path` is set, the harness version is the one bundled in the SDK wheel, so pinning the SDK pins the CLI. `tested_versions` and the installation image must either follow `__cli_version__` or set `cli_path` explicitly to the image's binary.

## Turn and event counts for `max_events`

`ResultMessage.num_turns` is the only count the SDK reports, and only at the end of a query. It does not match the number of API calls:

| Session | API calls (distinct `message_id`) | `num_turns` |
|---|---|---|
| question 2, 15 tool calls | 16 | 16 |
| question 3, valid schema | 1 | 2 |
| question 3, impossible schema | 4 | 4 |
| question 5, interrupted query | 1 | 3 |
| failed authentication | 0 | 1 |

The CLI emits one `AssistantMessage` per content block, so one API call can produce several messages: 17 messages for 16 calls in question 2, 7 for 4 in question 3. Each tool result arrives as a `UserMessage` with a `ToolResultBlock`. This confirms that kora counts events itself (section 3, section 14 `limits.max_events`). It also refines the first-slice default "`max_events` counts `turn`, `tool_call` and `tool_result` events": a `turn` should be counted per distinct `message_id`, not per `AssistantMessage`, or one API call with thinking, text and a tool use counts as three turns. `max_turns` remains usable only as the additional native limit.

## Further observations

- **Per-workspace `CLAUDE_CONFIG_DIR` and a subscription login.** With `env={"CLAUDE_CONFIG_DIR": "<repo>/.kora/claude"}` (the section 3 first-slice default) the session could not authenticate. `q0_config_dir_auth.py` observed: `AssistantMessage(model="<synthetic>", error="authentication_failed", text="Not logged in · Please run /login")`, then `ResultMessage(subtype="success", is_error=True, terminal_reason="api_error", duration_ms=36, total_cost_usd=0)`. The default holds only with `ANTHROPIC_API_KEY` in the environment, which DD-30 already prescribes. It cannot be used with a developer's subscription login. The CLI wrote `.claude.json`, `backups/` and `sessions/` into the new directory.
- **A failed run can report `subtype: "success"`.** The authentication failure above had `subtype: "success"` with `is_error: true`. The stop reason row of section 14 maps on `subtype` only. The CLI's result schema lists `success`, `error_during_execution`, `error_max_turns`, `error_max_budget_usd` and `error_max_structured_output_retries`, and the mapping should check `is_error`, `terminal_reason`, `api_error_status` and `AssistantMessage.error` as well.
- **Skills through `add_dirs` are not loaded with `setting_sources=[]`.** `q7_skills_add_dirs.py` (connect only, no prompt) materialised `kora-skill` in a separate directory passed as `add_dirs`. With `[]`, `get_server_info()["commands"]` did not list it. With `["project"]` it did, along with the repository's `canary-skill` and `canary-cmd`. `q7b_skills_plugin_dir.py` passed the same skill as a local plugin, `plugins=[{"type": "local", "path": …}]` with a `.claude-plugin/plugin.json`, under `setting_sources=[]`. The command list then contained `kora-node:kora-skill` and still no `canary-skill`. This **contradicts** the section 14 `skills` row ("materialised skill directories through `add_dirs` with `setting_sources=[]`"). A local plugin directory is a working alternative. Whether the model can invoke the plugin skill was not tested.
- **The SDK inherits the parent environment.** The transport builds the child environment as `{**os.environ (minus CLAUDECODE), "CLAUDE_CODE_ENTRYPOINT": "sdk-py", **options.env, "CLAUDE_AGENT_SDK_VERSION": …}` (`subprocess_cli.py`, `connect()`). `options.env` can add or override variables but cannot remove inherited ones. This refines the section 14 `env` row: the runner process itself must run with the constructed minimal environment.
- **No system prompt unless one is given.** When `system_prompt` is unset, the SDK passes `--system-prompt ""`; the process listing of a probe session showed `… --system-prompt --tools Bash …`. A node without `system_prompt_append` must still be given `system_prompt={"type": "preset", "preset": "claude_code"}` if it is meant to run with Claude Code's default system prompt.
- **The Bash tool runs in the invoking account's shell snapshot.** Commands ran as `/bin/zsh -c source <config dir>/shell-snapshots/snapshot-zsh-…`, a snapshot of the invoking account's login shell configuration, even with `setting_sources=[]`. A per-attempt config directory and the runner's constructed environment (`SHELL`, `HOME`) decide what the agent's shell inherits.
- **State written under the default config directory.** With `setting_sources=[]` and `--no-session-persistence` no transcripts were written. The CLI still updated `~/.claude.json` (with timestamped backups), `session-env/` and `sessions/`, and `get_context_usage()` created empty `projects/<target path>/memory/` directories. This supports the per-attempt config directory of the first-slice defaults.
- **Permission mode must be set explicitly.** With `permission_mode` unset, the init message reported `auto` in the fresh config directory and `default` under the existing one.
- **`--bare`.** CLI 2.1.301 has a `--bare` flag, reachable through `extra_args`. Its help text says it skips settings and plugin hooks, auto-memory, keychain reads and `CLAUDE.md` auto-discovery, and accepts only `ANTHROPIC_API_KEY` or an `apiKeyHelper`. It matches the hermetic mode under API-key credentials but was not run, because the probe had no API key.
- **Billing class.** Under the subscription login the init message reported `apiKeySource: "none"` and every authenticated session emitted a `RateLimitEvent`. These are the two signals available for the section 14 "billing class" probe; the API-key case was not observed.

## Summary against protocols.md

| Specification text | Finding | Verdict |
|---|---|---|
| §14 `trust_repo_config: false` → `setting_sources=[]` | repository `CLAUDE.md`, hooks, permissions, `.mcp.json`, skills, agents, commands ignored; account MCP servers still loaded | confirms; refines (add `strict_mcp_config=True`) |
| §3 `policy_coverage`; §14 `tool_policy` | callback sees Bash, Read, Write, Edit, Glob, Grep, SDK MCP tools; deny prevents execution in `bypassPermissions` | confirms for `shell`, `file_edit`, `mcp_tool`; `subagent`, `hosted_tool` not tested |
| §14 `tool_policy` fast path through `allowed_tools`/`disallowed_tools` | settings deny rules and tool validation act before the callback; `StructuredOutput` passes through it | refines |
| §14 `output_schema`; §3 structured-output default | `structured_output` on success; non-conforming answer gives `success` with `null`; text fallback unsafe without schema validation | confirms; refines |
| §3 `reports_usage_per_turn`; §14 usage source | per-turn usage only from `message_delta`; `AssistantMessage.usage` undercounts; `model_usage`/cost cumulative per process; thinking and cache tokens separate | refines |
| §3 termination default; §14 `limits.timeout` | interrupt ends in ms with a usage-bearing result; `disconnect()` takes 5 s + SIGTERM and yields no result | refines |
| §14 stop reason / error class | extra subtypes; `success` with `is_error: true`; `terminal_reason` needed | refines |
| §3 pinned binary; §3 `installed_version()` | bundled CLI 2.1.301 preferred over `PATH`; version in init and `get_server_info()` | confirms; refines |
| §3, §14 `max_events` | `num_turns` unreliable; count per `message_id` | confirms; refines |
| §3 `CLAUDE_CONFIG_DIR=<workspace>/.kora/claude` | breaks a subscription login; fine for API-key credentials (DD-30) | refines |
| §14 `skills` → `add_dirs` with `setting_sources=[]` | skills not discovered; local plugin works | contradicts |
| §14 `env` | SDK inherits the parent environment | refines |

## Not covered

Subagent (`Agent`) and hosted tool (`WebSearch`, `WebFetch`) calls through the callback; the `plan`, `acceptEdits` and `dontAsk` modes for deny; `resume` and usage across a resumed session; `max_turns` and `max_budget_usd` terminations; any behaviour under `ANTHROPIC_API_KEY` authentication, including `--bare`; Linux.
