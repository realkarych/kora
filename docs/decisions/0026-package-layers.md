# ADR-0026: Package layers

- Status: direction and implementation plan accepted, 2026-10-19; document wording pending owner review
- Deciders: project owner
- Resolves: issue #104
- Amends: [ADR-0013](0013-implementation-stack.md) (repository layout)
- Related: [ADR-0002](0002-ai-first-api-and-thin-clients.md), [ADR-0003](0003-scheduler-and-workers-over-http.md), [ADR-0004](0004-harness-adapters-over-vendor-sdks.md), [ADR-0005](0005-eleven-protocols-with-conformance-suites.md), [ADR-0006](0006-graph-definition-dsl-and-yaml-representation.md), [ADR-0016](0016-trusted-execution-and-publication.md)

## Context

[ADR-0013](0013-implementation-stack.md) ships one distribution, `kora`, with a `src/` layout and extras. Its processes run in different places:

- the API and the scheduler run in the control plane;
- workers run on hosts that reach the control plane only through the HTTP API (ADR-0003);
- `kora-runner` supervises execution, and the plan executor runs in `runner` or on the worker host (ADR-0016);
- the CLI and the stdio MCP server run on the author's machine and compile graphs there (ADR-0002).

Several components are shared between these processes: the API client and its models, persistence, the transition functions, the IR validator, the CEL parser and checker, the runner and the plan executor, and the logging bootstrap. Without a rule, code that parses candidate-controlled data or runs on a worker host can come to import server code and its dependencies. `kora compile` can come to need the `kora[cel]` extra (ADR-0006). Nothing would notice.

## Decision

**Packages by role.** Each shared component is a top-level package under `src/kora`. Provider implementations live in families under `src/kora/plugins`. The per-package table "Package layers" in [knowledge/architecture/detailed.md](../../knowledge/architecture/detailed.md#package-layers) lists every package, what it holds and the packages it may import. It is a reference that the test mirrors, not a second normative source.

**The invariant.**

- The **server-free set** is `observability`, `cel`, `ir`, `protocols`, `models`, `client`, `executor`, `runner`, `worker`, `dsl`, `cli`, `mcp`, `mcp.stdio`, `plugins.runtime`, `plugins.harness`, `plugins.vcs`, `plugins.policy` and `plugins.secrets`.
- The **server set** is `db`, `engine`, `api`, `scheduler`, `cel_eval`, `plugins.authentication` and `plugins.approval`.
- No package of the server-free set reaches the server set through any chain of static imports.

**The import rule.**

- A module of package `kora.<y>` may import `kora.<x>` only when `x` is `y` or is allowed by `y`'s row. A module under `plugins` belongs to the longest listed family prefix, and `mcp.stdio` is matched before `mcp`.
- `from kora import x` resolves to `kora.x` when that module exists, and to the package root otherwise. Relative imports are resolved against the package for an `__init__.py` and against the module's parent otherwise.
- Imports inside functions and under `if TYPE_CHECKING:` count, because a type-only dependency still couples the layers.
- The package root `kora` imports no kora package at module level.

**Composition roots.** `kora.__main__`, `kora.standalone`, `kora.testing` and `kora.conformance` may import anything. No package imports a root.

- `kora.__main__` assembles each process. That includes the API application with the HTTP-mounted MCP servers, which reach the API through the shared client's in-process ASGI transport (api-and-agent-surface.md section 4). `api` therefore imports neither `mcp` nor `client`.
- `kora.standalone` composes the API, scheduler and worker in one process.
- `kora.testing` is a root because `run_scenario` starts an in-process `kora standalone` (dsl.md section 4).
- `kora.conformance` is a root because the end-to-end conformance suite needs the whole engine (ADR-0005).

**Third-party checks.**

- Only `cel_eval` may import the CEL binding `cel`, so `cel`, `ir` and `dsl` work without the `kora[cel]` extra.
- `fastapi`, `starlette`, `uvicorn`, `sqlalchemy`, `alembic`, `asyncpg` and `aiosqlite` may not be imported by `observability`, `cel`, `ir`, `protocols`, `models`, `client`, `executor`, `runner` or `worker`. `mcp` is left out until the MCP SDK's HTTP stack is settled.
- A call to `importlib.import_module`, `__import__` or `pkgutil.resolve_name` whose first argument is a string literal starting with `kora.` is refused outside the provider-loading module `kora.protocols.loading`.

**Enforcement.** `tests/architecture/test_import_layers.py` parses every module under `src/kora` with the standard library `ast` module. It checks the import rule against the table and the invariant over the transitive closure of the actual import graph, independently of the table. It also checks the third-party rules, and it keeps the test's table identical to the documented one. CI runs pytest in every scaffold lane.

**How the table changes.**

- A new package enters the table in the change that creates it.
- Widening an existing row's allowed imports is stated in that change's issue plan and approved by the project owner.
- A change that breaks the invariant amends this record.

**One base installation.** `pip install kora` remains the only installation, with the extras of ADR-0013. A lighter extra or a separate distribution would need its own record.

### What layering does and does not guarantee

Layering is a maintainability and packaging rule, with defence in depth for trusted processes. It is not an isolation control.

- **The ADR-0016 boundary does not rest on it.** That boundary rests on runtime authority, not on which modules a process imports. One distribution installs all server code and its dependencies wherever `kora-runner` is installed (ADR-0004, ADR-0013).
- **Process placement.** On both reference runtimes the supervisor runs on the host ([protocols.md section 2](../../knowledge/specs/protocols.md#2-runtime-fr-81)). The plan executor runs in `runner` or `worker_host`. The Codex `kora-runner policy-hook` is the one piece of kora code that runs inside the candidate domain ([protocols.md section 12](../../knowledge/specs/protocols.md#12-policy-dd-29)).
- **What it gives.** Trusted processes that parse candidate-controlled data load less code. `runner` has no code path to `client`, matching "the supervisor has no API credential" (ADR-0016).

It does not guarantee:

- what a process can import in an environment where kora is installed;
- dynamic loading through `kora.provider` entry points, `importlib`, `__import__` and `pkgutil.resolve_name`, beyond the literal check above;
- process placement and credential separation, since an execution-side provider may run supervisor code inside the worker process;
- third-party imports beyond the checked list above.

The threat model was checked for TM-05 and TM-12 and needs no change, because layering is not a control there.

## Alternatives considered

- **A single `kora.core` package for everything shared.** Simpler at the start. It becomes a catch-all, and the runner would import whatever the server needs.
- **Several distributions in a uv workspace** (client, runner, server). This gives hard packaging isolation, but it changes the one-distribution row of ADR-0013 and adds release and version coordination before any code exists.
- **`import-linter`.** It expresses the same contracts, but it is a new development dependency. The standard library check is short and needs none.
- **A documented convention only.** Nothing would detect a violation.

## Consequences

- Two developers can work on the server and execution sides along the table, with shared contracts owned on one side (#127).
- Protocol data types that cross the worker routes live in `protocols`, so `models` imports `protocols`.
- Only the providers that read their own rows, `static_token` authentication and the `cli` approval channel, may import `db`. No provider writes execution or approval rows.
- Entry-point discovery imports every registered provider module, so a runner resolving its harness could still load a control-plane provider at run time. The open question "Provider discovery footprint" in `detailed.md` tracks this.
- The real-tree check passes trivially until packages exist. The checker's own tests on small source trees show that each rule fails when broken.

## Where it is applied

- [knowledge/architecture/detailed.md](../../knowledge/architecture/detailed.md#package-layers): the package layer table, the module and contract map, the repository layout row and the open questions.
- `tests/architecture/test_import_layers.py` and the `scaffold` job in `.github/workflows/ci.yml`.
- [AGENTS.md](../../AGENTS.md#code): how the table changes.
