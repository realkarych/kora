# Research: MCP Tasks in the official Python SDK and in coding-agent clients

Checked on **2026-10-19** on macOS 27 (arm64), for issue #117. The probe ran outside the repository in a throwaway `uv` environment (Python 3.11.17, `mcp` 2.3.0, `mcp-types` 2.3.0, FastAPI 0.146.0, Starlette 1.9.0, uvicorn 0.54.0, httpx 0.28.1); nothing was added to `uv.lock`. Clients examined: Claude Code CLI 2.1.302 and Codex CLI 0.163.0. Every version, status and default below is a volatile external fact as of this date.

The specification text under test is [api-and-agent-surface.md section 4](../specs/api-and-agent-surface.md#4-mcp-servers-fr-01-dd-28): "Long operations (`kora_plan`, `kora_trigger`, `kora_retry`) are exposed as **MCP Tasks** per the 2026-07 specification, so an agent starts them, receives a task id and polls or awaits completion instead of blocking on a tool call", the `(task)` rows of its tool table, the MCP server row of the [stack table](../architecture/detailed.md#stack-per-component) ("official `mcp` Python SDK (FastMCP) … long operations as MCP Tasks (2026-07 spec)") and the matching bullet of [ADR-0002](../../docs/decisions/0002-ai-first-api-and-thin-clients.md).

## Summary

| Question | Answer | Effect on the specification |
|---|---|---|
| Current MCP revision | **2026-07-28** is current. Tasks are no longer part of the core protocol: they moved to the official extension `io.modelcontextprotocol/tasks` (SEP-2663, Final). | The month matches; "MCP Tasks per the 2026-07 specification" is inaccurate. |
| Official Python SDK | `mcp` **2.3.0** does not implement Tasks on the server or the client. FastMCP is renamed `MCPServer`. Tasks can be hand-built on the SDK's extension API; the probe did so over stdio and over Streamable HTTP mounted in FastAPI. | Contradicts "official `mcp` Python SDK (FastMCP)" and the assumption that the SDK serves Tasks. |
| Clients | Neither Claude Code 2.1.302 (observed live) nor Codex 0.163.0 (not run live; concluded from its source and documentation) declares the Tasks extension. Both see only the plain tool. | No relevant client can call a task-augmented tool today. |
| Fallback | Return the run id at once and wait through a bounded, separate call. Do not block until the run is terminal. | Section 4, the stack row, ADR-0002, FR-0.1 and dsl.md need edits; listed below. |

## 1. Specification revisions and the Tasks feature

**Answer.** The [versioning page](https://modelcontextprotocol.io/specification/versioning) states: "The **current** protocol version is **2026-07-28**". The documentation index and the SDK's version registry list these revisions: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 and 2026-07-28, plus a `draft`. From 2026-07-28 on, MCP is stateless: there is no `initialize`, and every request carries its version and client capabilities in `_meta`. 2025-11-25 and earlier use the handshake.

Tasks first appeared as an experimental core utility in [2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks) (SEP-1686). That design used a `task` request field, a `tasks` capability, `tasks/get`, a blocking `tasks/result`, `tasks/list` and `tasks/cancel`. The [2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog), major change 6, says: "Move experimental tasks out of the core protocol and into an official extension (`io.modelcontextprotocol/tasks`). The redesigned extension replaces the blocking `tasks/result` method with polling via `tasks/get` …, removes `tasks/list`, and allows servers to return task handles unsolicited without per-request opt-in ([SEP-2663](https://modelcontextprotocol.io/seps/2663-tasks-extension))." SEP-2663 has status **Final** (Extensions Track). The normative text lives in [ext-tasks `specification/2026-07-28/tasks.md`](https://github.com/modelcontextprotocol/ext-tasks/blob/main/specification/2026-07-28/tasks.md); the repository README marks the `2026-07-28` schema Stable. The [extension overview](https://modelcontextprotocol.io/extensions/tasks/overview) summarises it. The mechanics in the 2026-07-28 extension:

- **Capability negotiation.** The client lists `"io.modelcontextprotocol/tasks": {}` under `extensions` in the per-request `io.modelcontextprotocol/clientCapabilities`. The server advertises the same key in its `server/discover` capabilities. A server "MUST NOT return `CreateTaskResult` to a client that did not include the extension capability on its request". If it cannot serve such a client without a task, it returns error `-32021` (Missing Required Client Capability).
- **Task creation from a tool call.** Only `tools/call` supports tasks. The server alone decides, per request, whether to answer with a `CreateTaskResult` (`resultType: "task"`, `taskId`, `status`, `createdAt`, `lastUpdatedAt`, `ttlMs`, `pollIntervalMs`) or with the ordinary `CallToolResult`. It must create the task durably before it responds.
- **Status polling and result retrieval.** `tasks/get` returns the current task. A `completed` task inlines the original `CallToolResult` in `result`, and a tool result with `isError: true` still counts as `completed`. A `failed` task carries the JSON-RPC `error`. `input_required` carries `inputRequests`, and the client answers through `tasks/update`. Clients should honour `pollIntervalMs` and persist task ids. The separate `tasks/result` method no longer exists.
- **Cancellation.** `tasks/cancel` returns an empty acknowledgement. Cancellation is cooperative and eventually consistent, and `notifications/cancelled` must not be used for tasks.
- **Progress.** Servers can push the full task state as `notifications/tasks` over `subscriptions/listen`. Per the spec, "`notifications/progress` and `notifications/message` … are not supported on tasks in general". Progress text goes into `statusMessage`.
- **Routing and security.** Over Streamable HTTP, `tasks/*` requests must set `Mcp-Name` to the task id so load balancers can route to the replica that holds the task. Task ids must be unguessable, and each `tasks/*` request is authorised again.

**Spec check.** This only partly confirms section 4. The revision month is right (2026-07-28), but Tasks are an extension of that revision, not part of it. With SEP-2663 the client host does the polling: a host may resolve the task internally and show the model only the final result. The section 4 sentence "an agent starts them, receives a task id and polls" therefore describes host behaviour, not model behaviour. No progress notifications are available while a task runs.

## 2. Tasks in the official Python SDK

**Answer: no implementation in `mcp` 2.3.0** (the latest on PyPI, uploaded 2026-10-02). The 1.x maintenance line, last released as 1.30.0, still carries only the removed 2025-11-25 experimental runtime (`src/mcp/server/experimental/task_*.py` and `src/mcp/client/experimental/tasks.py` at tag `v1.30.0`). Evidence:

- The installed source keeps the Task models for types only. `mcp_types/_types.py` reads: "Tasks: introduced in 2025-11-25, removed from the core spec in 2026-07-28 (continuing as an extension). Defined here types-only; their methods are not in the request/notification unions below, so they are never dispatched." The method tables in `mcp_types/methods.py` mark "2025-11-25 (tasks/* deliberately absent)". No `tasks/get`, `tasks/cancel` or `tasks/update` handler exists in `mcp.server` or `mcp.client`.
- The SDK's [What's new in v2](https://py.sdk.modelcontextprotocol.io/whats-new/) lists among the removed APIs "the **experimental Tasks** API (`mcp.*.experimental`) … ([SEP-2663]) which this SDK does not implement yet". The [migration guide, "Experimental Tasks support removed"](https://py.sdk.modelcontextprotocol.io/migration/#experimental-tasks-support-removed) replaces v1's `enable_tasks()`, `run_task()` and `call_tool_as_task()` with "run the work inline in the tool handler and replace `ServerTaskContext.update_status()` with progress reporting".
- Upstream status: [python-sdk#2806 "Implement SEP-2663: Tasks Extension"](https://github.com/modelcontextprotocol/python-sdk/issues/2806) is open. On 2026-10-01 a maintainer wrote it is planned "this month", without a promise. [PR #3005 "Add the SEP-2663 Tasks extension (core)"](https://github.com/modelcontextprotocol/python-sdk/pull/3005) has been open and unmerged since 2026-06-26 (last updated 2026-07-31). It defers `tasks/update`, `taskSupport` gating, `notifications/tasks` and routing headers. Client-side support ([#3226](https://github.com/modelcontextprotocol/python-sdk/issues/3226)) was closed as a duplicate of #2806. The TypeScript SDK [ROADMAP](https://github.com/modelcontextprotocol/typescript-sdk/blob/main/ROADMAP.md) also lists Tasks as not yet implemented ([#2189](https://github.com/modelcontextprotocol/typescript-sdk/issues/2189)), and its conformance baseline marks the `tasks-*` server scenarios as expected failures; `tasks-status-notifications` is left out of that list on purpose because the conformance runner skips it. The ext-tasks repository ships only a TypeScript package, `@modelcontextprotocol/ext-tasks`. It provides requester (client) APIs, and its receiver support covers only the 2025-11-25 Tasks; there is no 2026-07-28 server.
- **FastMCP is gone in 2.x.** `from mcp.server.fastmcp import FastMCP` raises `ModuleNotFoundError: … This is mcp 2.x, where FastMCP was renamed to MCPServer (from mcp.server.mcpserver import MCPServer) …`.

**What the SDK does provide** is a pluggable extension API (SEP-2133, [Extensions guide](https://py.sdk.modelcontextprotocol.io/advanced/extensions/)). On the server: `Extension.methods()` (`MethodBinding`), one `intercept_tool_call` hook, and `require_client_extension` for the `-32021` gate. The runner passes non-core `resultType` values through on 2026-07-28 connections. On the client: `ClientExtension.claims()` (`ResultClaim` on `tools/call`), `session.send_request` for extension verbs, and `name_param` for the `Mcp-Name` header. The probe used this API to build a minimal SEP-2663 server and client.

**Probe code** (`tasks_ext.py`, about 320 lines including the client half and request logging, plus `server.py`, `client.py`, `run_http.py`; throwaway scripts that are not retained in the repository). The server-side core is this:

```python
class Tasks(Extension):
    identifier = "io.modelcontextprotocol/tasks"

    def methods(self) -> Sequence[MethodBinding]:
        return [
            MethodBinding("tasks/get", TaskIdParams, self._get, protocol_versions=MODERN_ONLY),
            MethodBinding("tasks/cancel", TaskIdParams, self._cancel, protocol_versions=MODERN_ONLY),
            MethodBinding("tasks/update", UpdateTaskParams, self._update, protocol_versions=MODERN_ONLY),
        ]

    async def intercept_tool_call(self, params, ctx, call_next) -> HandlerResult:
        if params.name not in self._task_tools or not _declared_tasks(ctx):
            return await call_next(ctx)
        record = self._store.create()
        self._store.spawn(record, lambda: call_next(ctx))
        return record.created()
```

The tool runs inside the store's task group, which is opened in the `MCPServer` lifespan. One helper switches between `ctx.report_progress(...)` (plain path) and `statusMessage` (task path):

```python
@mcp.tool()
async def kora_trigger(seconds: int, label: str, ctx: Context) -> str:
    for step in range(seconds):
        await report(ctx, step, seconds, f"{label}: step {step + 1}/{seconds}")
        await anyio.sleep(1)
    return f"run '{label}' succeeded after {seconds}s"
```

The client claims `resultType: "task"`. It also uses `allow_claimed=True` to drive `tasks/get` and `tasks/cancel` by hand. For HTTP, `MCPServer.streamable_http_app(streamable_http_path="/")` is mounted at `/mcp` in a FastAPI app whose lifespan enters `mcp.session_manager.run()`, next to an ordinary `/api/v1/health` route. uvicorn serves it on 127.0.0.1 at a free port and stops it after the run.

**Output, stdio** (`client.py stdio`, trimmed):

```text
[  0.64s] negotiated protocol: 2026-07-28
[  0.64s] server extensions: {"io.modelcontextprotocol/tasks": {}}
[  0.64s] tools/call returned: {"resultType": "task", "taskId": "HlMg…", "status": "working", "ttlMs": 3600000, "pollIntervalMs": 500, …}
[  1.15s]   tasks/get: {"status": "working", "statusMessage": "first: step 1/5"}
[  4.66s]   tasks/get: {"status": "working", "statusMessage": "first: step 5/5"}
[  5.67s]   tasks/get: {"status": "completed", "statusMessage": "first: done"}
[  5.67s] final result: run 'first' succeeded after 5s
[  5.67s] second task created: {"taskId": "gfji8KFN...", "status": "working"}
[  6.87s]   before cancel: second: step 2/30
[  6.87s] tasks/cancel acknowledged
[  7.37s]   tasks/get: {"status": "cancelled", "statusMessage": "cancelled by tasks/cancel"}
[  9.60s] Client.call_tool with claim resolver: run 'transparent' succeeded after 2s
[  9.60s] tasks/get unknown id: {"code": -32602, "message": "Unknown taskId"}
== client without the Tasks extension
[  9.95s]   notifications/progress: {"progress": 0.0, "total": 3.0, "message": "plain: step 1/3"}
[ 12.95s] blocking tools/call result: run 'plain' succeeded after 3s
[ 12.95s] tasks/get without declaring the extension: {"code": -32021, "data": {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}}}
== 2025-11-25 handshake client (mode=legacy)
[ 13.29s] negotiated protocol: 2025-11-25
[ 14.29s] tools/call result: run 'legacy' succeeded after 1s
```

**Output, Streamable HTTP inside FastAPI** (`run_http.py`). The same throwaway scripts, not retained in the repository. The client output matched the stdio run: task created at 0.20 s, `completed` at 5.29 s, the second task `cancelled` 0.5 s after `tasks/cancel`, the plain path with four progress notifications, and the `-32021` gate. A FastAPI middleware recorded the request headers:

```text
POST server/discover  MCP-Protocol-Version 2026-07-28   no Mcp-Session-Id
POST tools/call       Mcp-Name kora_trigger
POST tasks/get        Mcp-Name <taskId>        (16 requests for real tasks, 2 for unknown ids)
POST tasks/cancel     Mcp-Name <taskId>
POST/GET/DELETE       legacy 2025-11-25 client, with Mcp-Session-Id
[ 13.37s] uvicorn stopped: {"started": true, "should_exit": true}
```

**Limits of the probe.** Task state is in memory and in one process. `tasks/update` only acknowledges, and there is no `input_required` flow, no `notifications/tasks`, no TTL expiry and no auth binding of task ids. On a 2025-11-25 connection the task path cannot be used, because the SDK sieves every result to the core shape. The SDK's own TODO notes that the server runner does not check the client's capability for an extension `resultType`; the extension must do that itself. In kora, a task would be a projection of a run: `tasks/get` would read `GET /runs/{run}` with the caller's token, which keeps the MCP server stateless as section 4 requires and answers both durability and auth binding. That design was not built here.

**Spec check.** This contradicts the premise, stated in the [stack table row](../architecture/detailed.md#stack-per-component) and in [api-and-agent-surface.md section 4](../specs/api-and-agent-surface.md#4-mcp-servers-fr-01-dd-28), that the official SDK serves Tasks; [ADR-0013](../../docs/decisions/0013-implementation-stack.md) only chooses the official `mcp` SDK and is not contradicted. It also contradicts the name FastMCP: the 2.x class is `MCPServer`, and the 1.x line, which still has `FastMCP`, implements only the removed 2025-11-25 Tasks. Two parts of section 4 hold: the stdio server plus HTTP mount under `/mcp` works with `MCPServer` and FastAPI, and tool errors as `isError` results work.

## 3. Client support: Claude Code and Codex

**Answer.** Neither client declares `io.modelcontextprotocol/tasks`, so a spec-conformant server must send them the plain `CallToolResult`. Neither client is listed for Tasks in the [extension support matrix](https://modelcontextprotocol.io/extensions/client-matrix); the matrix has no Tasks column at all.

**Claude Code 2.1.302.** Its [MCP documentation](https://code.claude.com/docs/en/mcp) and [changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) say nothing about the Tasks extension. The v2 client runtime is built on TypeScript SDK 2.0, which has no Tasks support (section 2). Claude Code has a client-side substitute: "An MCP tool call in the main conversation that is still running after two minutes moves to a background task … Claude receives the task ID immediately", where "task ID" is Claude Code's own id. Such a background call "doesn't survive exiting the session". Non-interactive runs do not background unless `CLAUDE_AUTO_BACKGROUND_TASKS=1` is set, and neither do subagent calls. Timeouts: the idle timeout is 30 minutes for stdio and 5 minutes for HTTP, and each progress notification resets it. The wall-clock default is about 28 hours, and progress notifications do not extend the per-server `timeout`.

Two non-interactive sessions ran with `claude-haiku-5-5`, `--mcp-config <temp> --strict-mcp-config`, `--allowedTools mcp__kora-probe__kora_trigger`, `--permission-mode dontAsk`, `--setting-sources project`, `--no-session-persistence`, against the stdio probe server, with the probe directory as cwd. The existing login was used, and no settings were changed. The server logged every `tools/call`:

```text
session 1 (default negotiation)
  protocol 2026-07-28  clientInfo claude-code 2.1.302
  clientCapabilities {"elicitation": {"form": {}, "url": {}}, "roots": {"listChanged": true}}
  progress token present: yes   tasks extension declared: no   -> plain path
  tool_result "{\"result\":\"run 'cc-probe' succeeded after 6s\"}"   session 11.2 s, success
session 2 (MCP_SDK_GENERATION=v1, MCP_PROTOCOL_NEGOTIATION=legacy, set for that process only)
  protocol 2025-11-25 (initialize handshake)
  clientCapabilities {"elicitation": {"form": {}, "url": {}}, "roots": {"listChanged": true}}   (no 2025-11-25 "tasks")
  tool_result "{\"result\":\"run 'cc-legacy' succeeded after 4s\"}"   session 9.6 s, success
```

So Claude Code supports neither the 2026-07-28 Tasks extension nor the removed 2025-11-25 Tasks. A tool that would be task-capable is called as an ordinary blocking tool, with a progress token attached.

**Codex CLI 0.163.0** (released 2026-10-15). Only `--version` and `--help` were run, and the source was read at tag `rust-v0.163.0`; no Codex session was run. Codex uses `rmcp` 3.5.1. The extension allowlist in [`codex-rs/codex-mcp/src/client_capabilities.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/codex-mcp/src/client_capabilities.rs) admits only the MCP Apps UI extension and OpenAI form and elicitation extensions; Tasks is not among them. Searching the repository for `tasks/get`, `io.modelcontextprotocol/tasks`, `CreateTaskResult` and `task_support` returns nothing. The [Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) sets `tool_timeout_sec` to a default of **60** seconds. In [`rmcp_client.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/rmcp-client/src/rmcp_client.rs) that timeout runs in wall-clock "active time", pauses only while an elicitation is open, and is not extended by progress notifications. A tool that blocks until a run is terminal therefore fails in Codex after 60 seconds unless the user raises `tool_timeout_sec`. Codex was not exercised live, so this is inferred from its source and documentation.

**What a client without Task support sees.** The plain `tools/call` path: one blocking call that returns the final `CallToolResult`, with `notifications/progress` while it runs if the client sent a progress token. Both the probe client and Claude Code did. If the server instead insists on a task, the client gets error `-32021` with `requiredCapabilities`, which every client listed here would show as a failed tool call.

**Spec check.** This contradicts the section 4 premise that an agent session receives a task id and polls. With today's clients, a `(task)` tool behaves as a blocking tool.

## 4. Fallback that keeps API, CLI and MCP parity

| Option | Claude Code 2.1.302 | Codex 0.163.0 | Durability of the handle | Parity and cost |
|---|---|---|---|---|
| **Block until terminal, with progress notifications** (the SDK migration guide's port of v1 tasks) | Works. Interactive sessions move the call to the background after 2 minutes, not durably; headless sessions block. Idle timeout 30 min (stdio) or 5 min (HTTP), reset by progress | Expected to fail at the 60 s default `tool_timeout_sec`, since progress does not extend it (not run live; concluded from Codex source and documentation) | None: if the call is lost, the agent must find the run through `kora_runs` | One call per run. A run waiting on a human approval can hold the call for hours |
| **Return the StartOutcome at once, then wait through a bounded call** | Works | Works when the wait cap is below the client timeout | The run id is durable in the metadata database | Maps directly to `POST …/runs` plus `GET …/runs/{run}`. No new API operation if waiting is client-side polling; one read option if it is an API long-poll. The agent spends extra turns polling |
| **SEP-2663 Tasks, plus the plain path for clients that do not declare them** | Not used: Claude Code does not declare the extension | Not used | Durable if the task is the run | Hand-built until python-sdk PR #3005 lands; adds a second code path that no current client exercises |

Text that would change if Tasks are dropped from the first slice:

- [api-and-agent-surface.md section 4](../specs/api-and-agent-surface.md#4-mcp-servers-fr-01-dd-28): the sentence quoted at the top of this record; the `kora_plan` (task) row; the `kora_trigger` (task) row: "an accepted outcome is polled until the run reaches a terminal state or a requested waiting state; a saved no-run skip, including replay, completes the task immediately with that StartOutcome and performs no run polling; a quota refusal returns the structured tool error without run polling (`POST /graphs/{graph}/runs`, `POST /runs/{run}/rerun`, `GET /runs/{run}`)"; the `kora_retry` (task) row: "manual retry of a node (clear downstream) and wait for the node's next terminal state"; and the `kora_runs` row, if the wait moves there.
- [detailed.md stack table](../architecture/detailed.md#stack-per-component), MCP server row: "official `mcp` Python SDK (FastMCP)" becomes `MCPServer` (mcp 2.x), and "long operations as MCP Tasks (2026-07 spec)".
- [ADR-0002](../../docs/decisions/0002-ai-first-api-and-thin-clients.md), Decision: "long operations (`kora_plan`, `kora_trigger`, `kora_retry`) are MCP Tasks, so an agent starts them and polls or awaits completion". This is an owner-reviewed decision, so it needs an amendment.
- [FR-0.1](../../docs/requirements/functional.md): "long operations as MCP Tasks".
- [dsl.md](../specs/dsl.md) authoring loop, step 4: "Start `kora_plan` (an MCP Task)".
- [ADR-0020](../../docs/decisions/0020-execution-capacity-and-bounded-run-acceptance.md), "Expose the three facts through existing surfaces": "A trigger task receiving a no-run skip completes immediately without run polling." This is an owner-reviewed decision, so it needs an amendment; without Tasks, the no-run skip is simply the immediate `kora_trigger` result.
- [data-model.md](../specs/data-model.md) test table, `test_admission_state_has_surface_parity`: "immediate skip/replay MCP task completion without run polling" becomes an immediate skip/replay tool result.
- [Section 3 (CLI)](../specs/api-and-agent-surface.md#3-cli-fr-122): only if waiting gains a CLI form such as `kora run <run> --wait`, kept in step with the MCP wait.
- [Section 7 (parity checklist)](../specs/api-and-agent-surface.md#7-parity-checklist-fr-03-release-gate): coverage and schema checks stay as they are, because no API operation is added or removed. An optional new line could state that no MCP tool blocks longer than a stated cap.

## Recommendation for `kora_trigger` in the first slice

Do not use MCP Tasks in the first slice. `kora_trigger` should return the API's StartOutcome as soon as `POST …/runs` or `POST …/runs/{run}/rerun` answers: an accepted run with its id, a saved no-run skip, or the structured refusal as an `isError` result. It should not poll. Waiting should be a separate, bounded call on `kora_runs` (get one run), with a wait target (`terminal` or a named waiting state) and a timeout capped below Codex's 60-second default, for example 45 s. During the wait it emits `notifications/progress`. When the timeout passes, it returns the current run projection marked not finished, and the agent calls again. This keeps the tool count at fourteen and maps one-to-one onto API operations that the CLI already exposes (`kora trigger`, `kora run`). It also works unchanged in Claude Code, Codex and any other client, and it relies on the run id, which survives disconnects, rather than on a live tool call. The same pattern fits `kora_retry`, and `kora_plan` can stay an ordinary synchronous tool. The owner still has to decide whether waiting is client-side polling of `GET …/runs/{run}` or an API long-poll read, which would put the logic behind ADR-0002's single implementation. Revisit SEP-2663 once the official Python SDK ships the extension (python-sdk #2806, PR #3005) and Claude Code or Codex declares `io.modelcontextprotocol/tasks`. At that point the task path can be added for declaring clients only, with the task as a projection of the run, as section 2 of this record describes.
