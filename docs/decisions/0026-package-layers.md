# ADR-0026: Package layers

- Status: direction and implementation plan accepted, 2026-10-19; document wording pending owner review
- Deciders: project owner
- Resolves: issue #104
- Amends: [ADR-0013](0013-implementation-stack.md) (repository layout)
- Fake-module amendment dated 2026-10-21: the import rule for fake modules and the testing roots, under [issue #136](https://github.com/realkarych/kora/issues/136); implementation plan approved by the project owner, wording pending line-by-line owner review.
- Discovery amendment dated 2026-10-21: the consequence on provider discovery, under [issue #150](https://github.com/realkarych/kora/issues/150); wording pending line-by-line owner review.
- Related: [ADR-0002](0002-ai-first-api-and-thin-clients.md), [ADR-0003](0003-scheduler-and-workers-over-http.md), [ADR-0004](0004-harness-adapters-over-vendor-sdks.md), [ADR-0005](0005-eleven-protocols-with-conformance-suites.md), [ADR-0006](0006-graph-definition-dsl-and-yaml-representation.md), [ADR-0016](0016-trusted-execution-and-publication.md)

- First-slice amendment dated 2026-10-23: dry-run import activation is deferred under #140; scope and plan approved, wording reviewed line by line and [approved by the project owner](https://github.com/realkarych/kora/pull/220#issuecomment-6361959908) on 2026-10-23.

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
- The package root `kora` imports no kora package at module level, and no package imports the root itself (`import kora`, or `from kora import name` where `name` is not a module). `kora.plugins` is a namespace package that imports nothing. How the DSL's `from kora import graph, …` (dsl.md section 1) is exposed is open: the DSL work item decides it without loading the compiler into the API, scheduler, worker or runner, consistent with `test_control_plane_never_imports_compiler` (dsl.md section 2), and changes the root row in that change.

**Composition roots.** `kora.__main__`, `kora.standalone`, `kora.testing` and `kora.conformance` may import anything. No package imports a root.

- `kora.__main__` assembles each process. That includes the API application with the HTTP-mounted MCP servers, which reach the API through the shared client's in-process ASGI transport (api-and-agent-surface.md section 4). `api` therefore imports neither `mcp` nor `client`.
- `kora.standalone` composes the API, scheduler and worker in one process.
- `kora.testing` is a root because `run_scenario` starts an in-process `kora standalone` (dsl.md section 4).
- `kora.conformance` is a root because the end-to-end conformance suite needs the whole engine (ADR-0005).

Fake modules (amended 2026-10-21, [issue #136](https://github.com/realkarych/kora/issues/136)). A module within `kora.plugins.<family>.fake` is imported only by `kora.testing`, `kora.conformance`, its own fake module and `tests/`, and no fake module imports a fake module of another family. `kora.standalone` imports neither testing root, and `kora.__main__` imports them only inside its top-level `test`, `conformance` and `mcp` command functions; dry-run is outside the first slice under [#140](https://github.com/realkarych/kora/issues/140), and any future author-side testing composition changes this explicit list only in its own owner-approved implementation change. Composition roots make no dynamic-import call (`importlib.import_module`, `importlib.util.spec_from_file_location`, `importlib.util.module_from_spec`, `__import__`, `pkgutil.resolve_name`, `runpy.run_module`, `runpy.run_path`), whatever its argument. Elsewhere the rule sees static imports and literal dynamic imports only, and no checked form covers an entry point's `load()` outside provider loading, as the limits below state. The rule and its seeded self-test are part of the enforcement below ([fakes.md section 2](../../knowledge/specs/fakes.md#2-fakes-never-reach-a-serving-process)).

**Third-party checks.**

- Only `cel_eval` may import the CEL binding `cel`, and this binds the composition roots too, so `cel`, `ir` and `dsl` work without the `kora[cel]` extra. A root may still import `cel_eval` or `api`, so that the CLI entry point starts without the extra depends on `kora.__main__` deferring those imports; the CLI work item adds the pending contract `test_cli_compile_runs_without_cel_extra`, which runs `kora compile` in an environment without `kora[cel]`.
- `fastapi`, `starlette`, `uvicorn`, `sqlalchemy`, `alembic`, `asyncpg` and `aiosqlite` may not be imported by `observability`, `cel`, `ir`, `protocols`, `models`, `client`, `executor`, `runner` or `worker`. `mcp` is left out until the MCP SDK's HTTP stack is settled.
- A call to `importlib.import_module`, `__import__`, `pkgutil.resolve_name` or `runpy.run_module` whose module argument is a string literal naming a kora module is refused outside the provider-loading module `kora.protocols.loading`. The check follows aliases of these functions and of their modules, `builtins.__import__`, the `name` keyword and a relative literal with a literal package. Dynamic edges do not count towards the invariant, which is about static imports.

**Enforcement.** `tests/architecture/test_import_layers.py` parses every module under `src/kora` with the standard library `ast` module. It checks the import rule against the table and the invariant over the transitive closure of the actual static import graph, independently of the table. It also checks the third-party rules, keeps the test's table identical to the documented one, and keeps its server-free set, server set and composition roots identical to this record. CI runs pytest in every scaffold lane.

**How the table changes.**

- A new package enters the table in the change that creates it.
- Widening an existing row's allowed imports is stated in that change's issue plan and approved by the project owner.
- A change that breaks the invariant amends this record.

**One base installation.** `pip install kora` remains the only installation, with the extras of ADR-0013. A lighter extra or a separate distribution would need its own record.

### What layering does and does not guarantee

Layering is a maintainability and packaging rule, with defence in depth for trusted processes. It is not an isolation control.

- **The ADR-0016 boundary does not rest on it.** That boundary rests on runtime authority, not on which modules a process imports. One distribution installs all server code and its dependencies wherever `kora-runner` is installed (ADR-0004, ADR-0013).
- **Process placement.** On both reference runtimes the supervisor runs on the host ([protocols.md section 2](../../knowledge/specs/protocols.md#2-runtime-fr-81)). The plan executor runs in `runner` or `worker_host`. Kora code that runs inside the candidate domain includes the Codex `kora-runner policy-hook` ([protocols.md section 12](../../knowledge/specs/protocols.md#12-policy-dd-29)) and the candidate-side steps of a publish plan ([protocols.md section 4](../../knowledge/specs/protocols.md#4-vcs-fr-52)); whatever those import is reachable from the candidate.
- **What it gives.** Trusted processes that parse candidate-controlled data load less code. `runner` has no code path to `client`, matching "the supervisor has no API credential" in the [threat model's actor table](../threat-model.md#assets-actors-and-assumptions).

It does not guarantee:

- what a process can import in an environment where kora is installed;
- dynamic loading through `kora.provider` entry points, `importlib`, `__import__`, `pkgutil.resolve_name` and `runpy`, beyond the literal check above and the composition-root rule of the fake-module amendment, which also refuses `runpy.run_path` and the `importlib.util` spec loaders in roots;
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
- Entry-point discovery imported every registered provider module, so a runner resolving its harness could load a control-plane provider at run time. Amended 2026-10-21 ([issue #150](https://github.com/realkarych/kora/issues/150)): entry-point names encode the protocol, and `runner`, `executor` and `worker` load only their configured providers with `load_provider`, which imports only the requested entry point's module and its parent packages. Full discovery runs only for listing commands and conformance sessions. A provider's own import graph remains its responsibility, so this keeps a process from importing providers it did not ask for and does not contain a hostile provider. The pending contract `test_runner_process_import_footprint` of [provider loading](../../knowledge/specs/protocols.md#provider-loading-issue-150) checks the runner process.
- The real-tree check passes trivially until packages exist. The checker's own tests on small source trees show that each rule fails when broken.

## Where it is applied

- [knowledge/architecture/detailed.md](../../knowledge/architecture/detailed.md#package-layers): the package layer table, the module and contract map, the repository layout row and the open questions.
- `tests/architecture/test_import_layers.py` and the `scaffold` job in `.github/workflows/ci.yml`.
- [AGENTS.md](../../AGENTS.md#code): how the table changes.
