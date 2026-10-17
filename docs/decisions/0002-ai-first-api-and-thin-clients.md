# ADR-0002: AI-first — the HTTP API is the single implementation, every surface is a thin client, agent nodes never operate kora

- Status: accepted, 2026-10-15; amended 2026-10-16 (compilation is client-side and never an API operation; [architecture critique of 2026-10-16, finding A02](../../knowledge/reviews/architecture-critique-2026-10-16.md#a02-compile-isolation), issue #15)
- Reviewed: by the project owner, 2026-10-16
- Clarified: 2026-10-17, the existing fourteen-tool count from ADR-0014 is preserved by ADR-0017; wording pending owner review
- Deciders: project owner, after the design review of 2026-10-15 (both cycles)
- Absorbs: DD-5, DD-16, DD-18, DD-28, requirements decision 16, FR section 0 (FR-0.0 to FR-0.4)
- Related: [0003-scheduler-and-workers-over-http.md](0003-scheduler-and-workers-over-http.md), [0011-namespaces-and-personal-tokens.md](0011-namespaces-and-personal-tokens.md), [0013-implementation-stack.md](0013-implementation-stack.md)

## Context

kora's positioning states that it is operated by agents as much as by people: whatever an engineer can do through the UI, the CLI or the API, their own Claude Code or Codex session must be able to do through kora's MCP server and a published skill. NFR-0.3 makes this a design constraint on every operation.

Two questions had to be settled: how the agent-facing surface is produced (a first draft generated one MCP tool per API operation, which the [design review of 2026-10-15](../../knowledge/reviews/review-2026-10-15.md#curated-mcp-tools) judged noisy for agents), and whether agent nodes inside a graph may call kora.

## Decision

The HTTP API (`/api/v1`) is the only implementation of every operation. Its OpenAPI document at `/api/v1/openapi.json` is the generation source: the web TypeScript client is generated from it, the hand-written Python client is contract-tested against it, and the parity coverage test reads it.

Every other surface is a thin client with no logic of its own:

- the Typer CLI over the shared Python client, returning the API response or error envelope verbatim under `--json`;
- two curated MCP servers, `kora-authoring` and `kora-operating`, with fourteen task-shaped tools in total (ADR-0014); execution reconciliation uses existing `kora_admin` (ADR-0017); long operations (`kora_plan`, `kora_trigger`, `kora_retry`) are MCP Tasks, so an agent starts them and polls or awaits completion; both run as the stdio process `kora mcp` for a local session and are mounted on the API at `/mcp/authoring` and `/mcp/operating`;
- one skill package per harness (a Claude Code plugin, Codex skills), generated from the same reference material as the `kora_reference` tool and kept as files in each harness's own format;
- the `kora web` console, whose only HTTP dependency is the generated TypeScript client.

Parity is verified structurally: a CI coverage test checks that every API operation is reachable through at least one CLI command and at least one MCP tool, plus schema-coverage checks on the shared client; worker-facing routes and the frontend sign-in routes under `/auth/` are the only exclusions. A failing line blocks the release.

**Compilation is client-side** (amended 2026-10-16). Compiling Python sources into the intermediate representation is a pure function of the shared compiler library, executed in the author's own process: `kora compile`, or the stdio `kora mcp` authoring server. It is not an API operation, and it does not become one without a new decision record: importing a graph module executes its code, so the control plane never imports graph authors' Python. There is no compile route, the API accepts only compiled documents (`/validate`, deploy), and the HTTP-mounted authoring server omits `kora_compile`. Parity for compile is the presence of the CLI command and the stdio MCP tool; the coverage test lists compile as a client-side operation and fails if an operation that compiles sources appears in the OpenAPI document.

All errors are structured objects with a stable `code`, a message and a `location` (file and line for DSL errors, YAML path for IR errors, ids for runtime errors); clients branch on the code, never on prose.

The MCP servers present the user's token and therefore have exactly the user's permissions; they add no state and no permission model of their own.

Agent nodes inside a graph have no access to kora's MCP server or API. They receive typed inputs through the workspace and the prompt and leave typed outputs for the worker to collect; the intermediate representation (IR) gives them only the MCP servers their resolved config lists. Consequently there is no operator allowlist, no per-attempt token, no child-run cost attribution and no recursion limit; the earlier requirement FR-0.5 was removed.

## Alternatives considered

- **One generated MCP tool per API endpoint.** Parity for free, but dozens of low-level tools that agents handle poorly. Rejected for curated tools whose parity the coverage test proves.
- **Letting graph nodes call kora.** Would need a second token kind, cross-run cost lineage, recursion protection and an operation allowlist. Rejected: the MCP server exists for the user's own session only.
- **A remote compile route** (`POST /namespaces/{ns}/compile`, `kora compile --remote`), the design until 2026-10-16. Replaced: the route imported authors' modules inside the API process, with the API's database credentials and secret references in its environment, and a module that hangs at import blocked a request worker. Isolating it (a separate job without credentials, a read-only input bundle, resource and time limits, a bounded result) is a second execution system for a function the author's machine already performs ([finding A02](../../knowledge/reviews/architecture-critique-2026-10-16.md#a02-compile-isolation)); it remains the alternative if compilation ever has to run on the platform's side.

## Consequences

- Adding a capability means adding an API operation; CLI and MCP tools then fail the release checklist until they cover it.
- `kora_respond` needs a token carrying `approve`, which the MCP server's default token lacks (ADR-0011).
- The data model carries no `run.parent_attempt_id` and no attempt tokens.
- Skills are installed into the user's own session, never into the sessions kora runs as nodes.
- Nothing compiles in the browser in MVP: graphs are authored in Python and compiled with the CLI or the stdio MCP tool, consistent with FR-7.3, which keeps graph editing in the console post-MVP (FR-1.5). When console editing arrives, where compilation runs needs its own decision; the isolated compile job above is the recorded alternative.
- A session that uses the HTTP-mounted authoring server compiles with `kora compile` and passes the document to `kora_plan` and `kora_deploy`.

## Where it is applied

- [FR-0.0 to FR-0.4, FR-12.2](../requirements/functional.md); [NFR-0.3, NFR-5.1](../requirements/non-functional.md); [positioning.md, "AI-first"](../positioning.md)
- [api-and-agent-surface.md introduction, §1 to §7](../../knowledge/specs/api-and-agent-surface.md) (§2 and §7: compilation is not an API operation, client-side operations in the parity checklist)
- [detailed.md §1, §2, §8](../../knowledge/architecture/detailed.md) (§8: no control-plane process imports graph authors' Python)
- [protocols.md §10](../../knowledge/specs/protocols.md), [dsl.md §2 (compilation runs in the author's process), §5](../../knowledge/specs/dsl.md), [ir.md §4.1](../../knowledge/specs/ir.md)
- [design-decisions-2026-10-15.md, DD-5, DD-16, DD-18, DD-28](../../knowledge/reviews/design-decisions-2026-10-15.md)
