# ADR-0026: Package layers

- Status: direction and implementation plan accepted, 2026-10-19; document wording pending owner review
- Deciders: project owner
- Resolves: issue #104
- Related: [ADR-0002](0002-ai-first-api-and-thin-clients.md), [ADR-0003](0003-scheduler-and-workers-over-http.md), [ADR-0006](0006-graph-definition-dsl-and-yaml-representation.md), [ADR-0013](0013-implementation-stack.md), [ADR-0016](0016-trusted-execution-and-publication.md)

## Context

[ADR-0013](0013-implementation-stack.md) ships one distribution, `kora`, with a `src/` layout and extras. Its processes run in different places:

- the API and the scheduler run in the control plane;
- workers run on hosts that reach the control plane only through the HTTP API (ADR-0003);
- `kora-runner` and the plan executor run inside a runtime, next to the agent (ADR-0016);
- the CLI and the stdio MCP server run on the author's machine and compile graphs there (ADR-0002).

Several components are shared between these processes: the API client and its models, persistence, the transition functions, the IR validator, the CEL parser and checker, the runner and the plan executor, and the logging bootstrap. Without a rule, code next to the agent or on a worker host can come to import server code and its dependencies. `kora compile` can come to need the `kora[cel]` extra (ADR-0006). Nothing would notice.

## Decision

**Packages by role.** Each shared component is a top-level package under `src/kora/`. The normative table "Package layers" in [knowledge/architecture/detailed.md](../../knowledge/architecture/detailed.md#package-layers) lists every package, what it holds and the packages it may import. In outline:

- `observability`, `cel`, `ir` and `protocols` form the base and import no server code;
- `models` and `client` sit above them for the HTTP surface;
- `executor` and `runner` sit above them for execution next to the agent;
- `db`, `cel_eval` and `engine` sit above them for the control plane;
- `api`, `scheduler`, `worker`, `dsl`, `cli`, `mcp` and the provider families under `plugins` sit at the top.

`runner`, `executor`, `client` and `models` never import `db`, `engine`, `api` or `scheduler`.

**The import rule.**

- An import of `kora.<x>` from a module of package `kora.<y>` is allowed only when `x` is `y` or is listed in `y`'s row.
- A module under `plugins` belongs to the longest listed family prefix.
- Relative imports are resolved before checking.
- Imports under `if TYPE_CHECKING:` count, because a type-only dependency still couples the layers.
- **Composition roots** may import any package and each other; no package imports a root. There are three:
  - `kora.__main__` assembles the CLI and MCP entry points, including the commands that need server components: `kora standalone`, the server subcommands such as `kora db upgrade`, and the MCP servers mounted on the API;
  - `kora.standalone` composes the API, scheduler and worker in one process;
  - `kora.testing` runs scenarios on an embedded standalone (`kora test`, the stdio `kora_test` and `run_scenario`, dsl.md section 4).
- Third-party imports are governed by the stack table, with one exception that also binds the composition roots: only `cel_eval` may import the CEL binding (`cel`), so that `cel`, `ir` and `dsl` work without the `kora[cel]` extra.

**Enforcement.**

- A pytest test reads every module under `src/kora` with the standard library `ast` module and fails on any static import outside the table, listing each `file:line`. Dynamic imports (`importlib`, entry-point loading) are not checked; providers are loaded through entry points by design, and authority still comes from credentials and the HTTP boundary (ADR-0003, ADR-0016), not from which code is present.
- A second test keeps the test's table and the documented table identical.
- CI runs pytest in every scaffold lane.

**Packages enter with their code.** A package is created only when its first code arrives. The change that creates it adds its row. A new import direction changes the table in a reviewed change.

**One base installation.** `pip install kora` remains the only installation, with the extras of ADR-0013. The layers make a lighter extra or a separate distribution possible later without refactoring. That split would need its own record.

## Alternatives considered

- **A single `kora.core` package for everything shared.** Simpler at the start. It becomes a catch-all, and the runner would import whatever the server needs.
- **Several distributions in a uv workspace** (client, runner, server). This gives hard isolation, but it changes the one-distribution row of ADR-0013 and adds release and version coordination before any code exists.
- **`import-linter`.** It expresses the same contracts, but it is a new development dependency. The standard library check is short and needs none.
- **A documented convention only.** Nothing would detect a violation.

## Consequences

- Two developers can work on the server and execution sides along the table, with shared contracts owned on one side (#127).
- Protocol data types that cross the worker routes live in `protocols`, so `models` imports `protocols`.
- Provider families split by what they may import, not by which process loads them. Execution-side families may import `runner` and `executor`; control-plane families may import `db`. A family's backend can still be loaded by `kora api`, such as the GitHub App issuer of ADR-0023 in the `vcs` family, and gets no database access through this table.
- The test passes trivially until packages exist. A seeded violation in the introducing pull request shows that it fails.
- A needed exception changes the table. A type that both sides need moves down to `ir`, `protocols` or `models` rather than widening an upper row.

## Where it is applied

- [knowledge/architecture/detailed.md](../../knowledge/architecture/detailed.md#package-layers): the package layer table, the module and contract map, and the repository layout row.
- `tests/architecture/test_import_layers.py` and the `scaffold` job in `.github/workflows/ci.yml`.
- [AGENTS.md](../../AGENTS.md#code): a new package or import direction updates the table in the same change.
