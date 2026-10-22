# AGENTS.md

Rules for every coding agent and human contributor in this repository. They take precedence over the defaults and habits of any model or tool. If a rule blocks the task, stop and say so instead of working around it.

## Project

kora is an open-source (Apache-2.0) control plane that runs graphs of coding-agent sessions. Documentation has two layers. `docs/` is written for people: `positioning.md`, `requirements/functional.md`, `requirements/non-functional.md`, `technical-introduction.md`, the decision records in `decisions/` and the deferred changes in `proposals/`. `knowledge/` is the reference layer loaded section by section: the normative specifications in `knowledge/specs/`, the component diagrams, deployment and module maps, stack table, glossary and open implementation questions in `knowledge/architecture/detailed.md`, review records, analyses and research. `knowledge/README.md` says which file to load for which task. Read the specification of a component and the decision records it cites before implementing it; when a specification and a decision record disagree, the decision record wins.

## Issues and pull requests

The repository holds what is durably true about kora; a GitHub issue holds why and how one change happens. A future session reads only the repository, so everything it will need must land there. The issue form in `.github/ISSUE_TEMPLATE/` and the pull request template in `.github/pull_request_template.md` carry the structure below; a change to these rules updates them in the same pull request.

- **Lead with impact.** Every issue and pull request body starts with an `Impact` section: one short paragraph of 1–3 sentences explaining what changes in the project, for whom, and why it matters where relevant. Write in plain language for a project reader who has not read the implementation or prior discussion. An issue describes the intended effect; a PR describes the effect delivered by its current scope, including material limits. Describe changes to capabilities, behavior, guarantees, constraints, decisions or contributor workflows. Do not discuss code or implementation, list files or symbols, report checks, or narrate the work in `Impact`. For design, documentation or process work, state its actual effect without implying implemented runtime behavior or inventing benefits. Put technical details below; the brevity rule does not reduce required acceptance or verification evidence.

  Example: "Issues and pull requests open with a brief explanation of what changes for kora and who it affects. Reviewers can understand the significance of a change before reading implementation and verification details."

- **Every change goes through an issue and a pull request**, code and documentation alike. No direct commits to `master`. Each pull request closes exactly one issue (`Closes #N` in its description); an issue too large for one reviewable pull request is split into sub-issues.
- **The issue body** states the problem, the desired outcome, what is affected (users, components, specifications), constraints and open questions. It links the requirements, decision records and specifications involved instead of restating them.
- **Acceptance criteria are checkable without asking a person**: a named test passes, a command prints a given result, an endpoint returns a given field. They are listed in the issue body.
- **Plan before code.** For anything beyond a trivial fix (a typo, a broken link, an obvious one-line bug), post an implementation plan as an issue comment: the files to change, the order of work, the risks, and the tests that prove each acceptance criterion. Do not edit files until the project owner approves the plan. When the implementation diverges from it, update the plan comment before continuing.
- **Decisions never live only in an issue.** Discussion and draft decisions happen in the issue; once decided, the decision lands in the pull request as a decision record, a requirement or a specification change. Large or uncertain changes start as a proposal in `docs/proposals/`.
- **The pull request** contains the code together with the document updates it requires and lists every acceptance criterion with the evidence that it holds (a test name, command output). Review happens in pull request comments. When the same review finding recurs, add the rule that prevents it to this file.
- **Every pull request lands on `master` as one squash commit.** The repository allows no other merge method, and a ruleset requires linear history on `master` for everyone, administrators included. The commit subject is the pull request title followed by `(#N)` and the commit body is empty, so write the title as a commit subject that names the change on its own; the description, evidence and review stay in the pull request. Merge with `gh pr merge --squash`.

### Labels

**Agents must maintain GitHub labels.** When creating a change issue or a pull request, assign exactly one primary `type:*` label and any applicable supplemental labels. When updating an issue or PR, check its labels, reconcile them with its current scope and status, and verify the saved assignments. A PR and the issue it closes must have the same primary type. Labels describe the work; they never replace owner approval of a plan.

| Label | Meaning |
|---|---|
| `type:bug` | A confirmed defect in implemented behavior or an established contract. |
| `type:enhancement` | A functionality, maintenance, tooling or process improvement. |
| `type:design` | Work whose result is an architecture decision, specification or behavioral contract. |
| `type:docs` | Editorial documentation work that does not change behavior or architectural decisions. |
| `status:deferred` | Confirmed work intentionally postponed to a stated condition or stage. |
| `question` | A concrete unresolved request for information or clarification. |
| `duplicate` | A confirmed duplicate with a link to the canonical issue or PR. |
| `help wanted` | A prepared task for which maintainers explicitly welcome outside contributions. |
| `good first issue` | Bounded, explained work suitable for a first contribution, with maintainer support. |

Choose the primary type by the intended result, not the file extension: changing a normative contract is not editorial documentation. Supplemental labels do not imply approval, priority or readiness. Do not add `question` merely because a task concerns architecture; add it for a concrete unanswered question and remove it once answered. Link a duplicate to its canonical issue or PR before closing it, and keep `duplicate` for discoverability.

For `status:deferred`, record the reason and a concrete revisit trigger in the issue body. Keep the issue open, revisit it when that trigger occurs, and remove the label when work resumes. Deferral does not mean rejection or low priority. Do not close accepted work solely because it is inactive. Only apply contribution labels when the task meets their definitions. Preserve historical label assignments when renaming labels.

## Development verification

- At task entry, before the implementation plan, the coordinating agent reads [kora-verify](.claude/skills/kora-verify/SKILL.md) and selects reviews and checks from the proposed scope. Before reporting readiness, repeat selection from the complete diff and run the required independent reviews and checks. After fixes, re-review affected conclusions and rerun affected checks; CI evidence must cover the final PR revision.
- Identify directly and transitively affected components from the complete change and its contract, data-flow and resource-lifecycle consequences. Record why each component is included and who reviews each integration boundary. Assign a fresh independent agent to each affected component and applicable review profile; a single context applying several profiles does not satisfy this requirement.
- Reviewer delegation is authorized at every level. Children may subdivide their assigned component/profile scope with fresh contexts and inherited permissions; they do not restart the whole review or grant themselves implementation authority. There is no repository-imposed agent-count or delegation-depth cap. The coordinator schedules within host limits, collects every required result and remains responsible for resolving findings.
- Performance review covers resource ownership and release as well as latency, throughput and complexity. Follow the profile's lifecycle measurement method and the component checklist's workloads; inspect memory, tasks, connections, descriptors, subprocesses and other applicable resources across success, failure, cancellation and shutdown.
- Record review scope, reviewed revision, command results and evidence links in the PR. Distinguish passed, failed, blocked, pending implementation and not applicable with reasons. Missing tools or unimplemented components are never passing checks. Follow the skill's stricter-base-policy rule when changing verification itself.

## Code Review Rules

- For native GitHub Codex Review, including automatic reviews and `@codex review`, read [kora-verify](.claude/skills/kora-verify/SKILL.md) and coordinate its GitHub reviewer mode. Use native subagents for the component/profile assignments; native delegation support is an operating assumption, not a prerequisite experiment.
- Review directly and transitively affected components in separate fresh profile contexts, allowing further scoped delegation. All descendants remain read-only and inherit the native execution restrictions. Aggregate their actual results through the native response; the implementation coordinator still resolves findings and completes the change.
- Use approved/base review instructions as authority and assess proposed instruction changes against them. Inspect revision-matched CI and measurement evidence. Native specialist results may satisfy their matching review assignments; neither a request nor a generic clean native response substitutes for component/profile results or executable checks.
- Report concrete defects in the host's review format and severity range. Unavailable evidence is unverified, not passing; do not inflate severity to fit the host's reporting threshold.

## Code

- **No comments and no docstrings** in hand-written source code: Python, TypeScript, SQL, shell, Alembic migrations. Code documents itself through names, types, small functions and explicit data structures. When you feel a comment is needed, rename, extract a function, or introduce a type or an enum instead.
- Tool directives count as comments and are forbidden too: no `# noqa`, `# type: ignore`, `# pyright: ignore`, `// eslint-disable`, `// @ts-ignore`, `// @ts-expect-error`. Fix the code, or disable the rule for the whole project in the tool's configuration. The only allowed comment-like line is a shebang in an executable script.
- **These rules are checked by tooling, as a floor.** Ruff (`uv run --no-sync ruff check --ignore-noqa .`, with the `BLE`, `PT`, `PGH`, `ERA`, `T20`, `TID` and `RUF100` families and a banned-API table for skips, mocks and warning filters), ESLint (`pnpm --dir web run lint`, with the local rule `kora/no-comments`, `noInlineConfig` and every `@ts-*` comment banned) and `tests/architecture/test_no_suppression.py` (comments, docstrings, directives, string markers, mock fixtures, dynamic imports, pytest hooks, blind handlers with pinned counts in the four allowed files, and the pinned Ruff, Pyright, pytest, TypeScript and frontend-script configuration) catch the common forms of comments, directives, blind handlers, skipped tests, mocks and weakened checks. The rules apply to every form, including ones the checks do not detect; an allowance or a configuration change edits that test and is approved by the project owner, with its reason in the [no-suppression research record](knowledge/research/no-suppression-checks-2026-10-20.md).
- Generated files (lockfiles, the generated OpenAPI TypeScript client) are exempt because nobody edits them by hand. Configure generators and templates (for example Alembic's `script.py.mako`) so they do not emit comments or docstrings into files that are then edited by hand.
- **All Python code is fully type-annotated**: every parameter, every return value (including `-> None`), every class and dataclass attribute, and module-level variables whose type is not obvious from the literal. No implicit `Any`, and no explicit `typing.Any` either: parse untyped data at the boundary into Pydantic models or precise types.
- **Static checks are strict and mandatory**: pyright in strict mode and ruff (lint and format) over all Python; TypeScript with `strict: true` and ESLint over `web/`. Code that does not pass them is not finished.
- **Fail fast.** No bare `except:`, no `except Exception` that swallows, logs-and-continues or returns a placeholder, no silent fallback values that hide a failure. Catch only what you can handle meaningfully and let everything else propagate. Every error that leaves the HTTP API is a structured error as specified in `knowledge/specs/api-and-agent-surface.md`.
- **The HTTP API is the single implementation** (ADR-0002). CLI, MCP servers and the web frontend are thin clients over the shared API client and contain no logic of their own. A new or changed API operation ships in the same change with its CLI command and MCP tool, and the parity coverage test passes.
- **Package layers are explicit** ([ADR-0026](docs/decisions/0026-package-layers.md)). Every package under `src/kora` except the composition roots and the `kora.plugins` namespace has a row in the [package layer table](knowledge/architecture/detailed.md#package-layers), and `tests/architecture/test_import_layers.py` enforces the table and the ADR-0026 invariant. A new package enters the table in the change that creates it. Widening an existing row's allowed imports is stated in that change's issue plan and approved by the project owner. A change that breaks the invariant amends ADR-0026. A type both sides need moves down a layer instead of widening an upper row.
- **Dependencies are deliberate.** Add one only when the standard library or an existing dependency cannot reasonably do the job, and record it with its reason in the stack table in `knowledge/architecture/detailed.md`; a dependency that changes a decided row of ADR-0013 needs a new decision record. Python dependencies are managed only with `uv` and frontend dependencies only with `pnpm`; `uv.lock` and `pnpm-lock.yaml` are committed and change only through those tools.

## Observability

- **Every executable component ships with structured, leveled diagnostics from its first implementation.** Follow [the observability specification](knowledge/specs/observability.md) for event ownership, correlation, levels, safe fields, bounded output and failure behavior. Process entry points configure logging; libraries and providers use bound loggers. CLI results and MCP stdio messages keep stdout free of diagnostic output.
- Keep operational diagnostics separate from durable domain/audit events and session content. Never use logs as execution authority, copy retained payloads into them or let output failure block scheduling. DEBUG does not relax secret/content protection.
- Component behavior changes include applicable observability impact and evidence in the issue plan and PR. The implementing issue owns the component's activation row and named tests; missing required evidence blocks readiness. Use the existing correctness, security and performance profiles through [kora-verify](.claude/skills/kora-verify/SKILL.md), preserving stricter baseline obligations.

## Documentation

- Everything committed to the repository is in English.
- Write every specification, design document, decision record, commit message and PR description for a reader who never saw the session that produced it. Never reference session-local context: subagent names, review rounds, ad-hoc labels such as "item K2", "question 5" or "hypothesis H3", "as discussed above", "the user asked". Give hypotheses, decisions, files and folders descriptive names.
- Identifiers that are defined inside the repository and resolve there (for example `FR-2.1`, `NFR-10.1`, `ADR-0007`, `DD-12`, issue and pull request numbers) are allowed; labels that exist only in a conversation are not.
- **Documents and code change together.** A change that alters behaviour described in a specification, decision record or requirement updates that document in the same commit. When code and its specification disagree and the task does not say which one is right, stop and ask instead of silently following either.
- **`docs/` is human-validated.** Every statement in `docs/` has been reviewed and accepted by the project owner, and each file records when. Agents change `docs/` only in a pull request the owner reviews line by line, and never place unreviewed findings there. Research, analyses, review records and drafts produced by agents go to `knowledge/`, which may hold agent-only material and is cited from `docs/` as evidence, never as a decision.
- **Volatile external facts** (versions, popularity, prices, release dates) live in `knowledge/research/` with the date they were checked; `docs/` states them qualitatively and links there.
- **No organisation-specific names.** Examples and adopter scenarios use neutral terms ("an in-house CI", "a non-git VCS", "a MapReduce cluster"), never the names of a specific company or its internal systems.

## Testing

- **Use Arrange–Act–Assert (AAA).** Write each test in three distinct, sequential phases: arrange inputs and dependencies, act on the behavior under test, and assert the results. Separate the phases with blank lines and make their purpose clear through descriptive names or extracted helpers, never comments or docstrings.
- **No mocks.** Forbidden: `unittest.mock`, `MagicMock`, `pytest-mock`, `monkeypatch`, patching modules, attributes or environment variables at runtime, stubbing HTTP clients at the transport level, and any other technique that replaces behaviour behind the code's back.
- **Fakes are the only test doubles.** A fake is a real implementation of a kora protocol (the fake harness adapter and fake runtime from `NFR-10.1` are the reference examples), selected through configuration or constructor injection and validated by the same conformance suite as production implementations. If code cannot be tested without patching, change its design so the dependency is injected.
- **Unit tests are rare.** Write them only for intricate logic where a bug hides easily: formulas and arithmetic (cost accounting, limits), parsers and checkers (CEL type checking, IR validation), state machines (attempt and retry transitions). Do not unit-test glue code, data mapping, trivial accessors or framework behaviour.
- **Integration tests are the bulk.** Exercise real components together: the real FastAPI application, the real metadata database, the real scheduler and worker, with fake harness and fake runtime where a model or sandbox would otherwise be needed.
- **Both databases.** Every integration test that touches the metadata database runs against PostgreSQL and against SQLite (standalone mode) through one parametrised fixture, and must behave the same on both.
- **Tests are deterministic.** No `sleep` for synchronisation: wait on an explicit condition with a deadline. No real network beyond local containers and in-process servers, and no calls to real model providers or code hosts. Time and randomness come from injected sources, and tests pass fake ones. A flaky test is a bug: fix its cause, never hide it with retries, reruns or longer timeouts.
- **E2E tests are targeted.** A small set drives kora through its public surfaces (CLI and HTTP API) and covers every main happy-path use case end to end.
- **CI finishes in under 10 minutes** wall-clock for the whole pipeline. Write tests so they can run in parallel. When a change threatens the budget, make tests faster or consolidate them; never drop coverage silently.

## Platforms

- Supported operating systems: **Linux and macOS**. Windows is not supported: no Windows code paths, compatibility shims, path handling or CI jobs. POSIX assumptions are fine.
- CI runs on Linux and macOS. GitHub-hosted macOS runners have no Docker, so tests that need Docker (Docker runtime, PostgreSQL through containers) run on the Linux job only.

## Definition of done

- Before reporting a task as done, run the same checks CI runs (format, lint, type check, all tests) and see them pass. Report failures as they are.
- Never make checks green by weakening them: no `skip` or `xfail` on failing tests, no deleted or loosened assertions, no relaxed linter, type-checker or test settings, no raised timeouts or CI budget, unless changing that check is the task itself.
