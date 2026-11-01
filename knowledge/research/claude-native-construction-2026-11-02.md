# Native Claude launch-input construction

Checked 2026-11-02. Implementing issue: [#626](https://github.com/realkarych/kora/issues/626). Implementation started from `9d77d75db56e2b50d5f773b981c345ee2e7cbf13` and rebased onto `09bc63b1f1bd8e215cd8c2797e763ffa21acbe0c` after the independently approved API adoption in #624 merged. [ADR-0028](../../docs/decisions/0028-optional-claude-sdk-stack.md) records the owner's accepted optional dependency/import boundary; its exact text was accepted before repository edits.

## Delivered boundary

`kora.plugins.harness.claude.configuration` owns immutable internal settings with explicit Bash path, retry ceiling and telemetry choice. There is no public deployment schema or default. Construction checks required paths lexically using existing printable/path bounds; it does not inspect or create directories, resolve secrets, inspect an installation, import the SDK or open an execution host. Runtime placement creation and provenance retain their existing owners.

The resulting `LaunchInputs` supplies exactly HOME, TMPDIR, CLAUDE_CONFIG_DIR, SHELL, ENABLE_CLAUDEAI_MCP_SERVERS, CLAUDE_CODE_DISABLE_AUTO_MEMORY, CLAUDE_CODE_DISABLE_BACKGROUND_TASKS, CLAUDE_CODE_MAX_RETRIES, CLAUDE_CODE_ENTRYPOINT, CLAUDE_AGENT_SDK_VERSION and CLAUDE_CODE_SDK_READS_SESSION_STATE. Telemetry off adds CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC; telemetry on substitutes DISABLE_AUTOUPDATER. No runtime controls, workload values, credentials or tracing carriers enter this native assignment map.

`kora.plugins.harness.claude.sdk` is an explicit operational import boundary. It checks SDK `__version__` and the pinned private `_cli_version.__cli_version__`, then uses the public `ClaudeAgentOptions` dataclass to replace only environment, cwd and the supplied runtime-launcher path. Environment is copied; other session-option fields remain caller-owned. The helper neither completes nor validates the remaining session options, establishes launcher provenance, hashes a binary nor calls connect/query/transport. SDK options remain ordinary configuration, not launch authority.

The existing local assembler owns duplicate-name refusal before secret revelation. Caller admission/scopes remain active through dependent output sanitation. This change adds no cache, queue, background task, owned process or secret scope. Construction emits no diagnostic event; fixed failures exclude supplied values. Unknown setting keys are rejected before Pydantic places them in rendered error locations; internal validation-error objects are not public diagnostic payloads. Future connection/allocate retains operational event and third-party-content ownership.

## Dependencies and provenance

The optional `claude` extra pins Claude Agent SDK 0.2.168 and MCP 2.3.0. The same SDK is a development dependency so existing full-suite and strict-check commands install their test subject. A runtime-only installation without the extra remains independent of the SDK. No installed harness entry point is added.

The [SDK metadata](https://pypi.org/pypi/claude-agent-sdk/0.2.168/json), [MCP metadata](https://pypi.org/pypi/mcp/2.3.0/json) and [pinned native transport source](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.168/src/claude_agent_sdk/_internal/transport/subprocess_cli.py) were inspected. SDK import reaches operational machinery and exit cleanup. At connection the SDK inherits supervisor variables and may inject tracing carriers; constructing a safe map does not stop that behavior. The later private launcher must discard the whole inherited/SDK-created map rather than filtering only known trace names.

The lock retains all prior distribution versions and sources. The approved SDK closure contains the following supported-system distributions, using the existing PyPI registry. AnyIO, h11, Starlette and OpenTelemetry API are already present after #624; this change adds the other 14 distributions. All pins match the approved closure and preserve the shared API versions.

| Distribution | Pin | Purpose |
|---|---|---|
| claude-agent-sdk | 0.2.168 | Native public option type and future vendor integration; includes bundled CLI |
| mcp | 2.3.0 | Required SDK MCP compatibility and protocol implementation |
| mcp-types | 2.3.0 | MCP message models |
| httpx2 | 2.16.0 | Vendor MCP HTTP client |
| httpcore2 | 2.16.0 | Vendor HTTP connection implementation |
| h11 | 0.16.0 | HTTP protocol framing |
| truststore | 0.10.5 | Vendor transport certificate-store support |
| anyio | 4.15.1 | SDK/MCP asynchronous operations |
| sniffio | 1.3.1 | SDK asynchronous-library detection |
| starlette | 1.10.0 | MCP server transport dependency; no kora server activated here |
| sse-starlette | 3.6.0 | MCP streaming transport dependency |
| uvicorn | 0.54.0 | MCP serving dependency; no listener activated here |
| python-multipart | 0.0.33 | MCP transport request parsing dependency |
| pyjwt | 2.15.1 | MCP authentication dependency |
| cryptography | 50.0.2 | Authentication cryptographic primitives |
| cffi | 2.1.1 | Native cryptography interface |
| pycparser | 3.12 | CFFI dependency |
| opentelemetry-api | 1.45.2 | Vendor context API; no exporter installed by this extra |

The universal lock also contains owner-approved `httpx2-jsfetch==1.0`, solely under `python_full_version >= '3.12' and sys_platform == 'emscripten'`. Neither supported operating system selects it; this record introduces no Emscripten or Windows support. No repository-wide resolver setting changes. The owner separately accepted this exact record after universal resolution exposed it; the original supported-system resolution had not included it.

`uv.lock` contains registry artifact hashes. Native CFFI/cryptography and the bundled-binary SDK join the existing binary-only package discipline. Python advisory scanning does not audit dependencies embedded inside the CLI binary. Existing lock/RECORD/binary-integrity gates remain mandatory before real launch; package metadata and constructor success cannot replace them.

## Verification scope

The concrete plan and independent reviews are recorded in #626. Correctness and security cover configuration/native ownership, SDK options/local assembly, optional packaging and honest activation. No new production resource owner, numerical method or hot path selects separate performance review; existing cleanup obligations remain active. No public operation or browser behavior changes.

- `test_claude_environment_reaches_real_process`: both telemetry branches, actual SDK options, real ProcessLauncher and exact child environment/cwd, including no supervisor/old-option/trace canary, detached maps and caller-owned registered-secret lifetime. The Python recorder receives its locale and macOS interpreter setting as explicit fixture assignments; exact full-map equality is retained.
- `test_claude_environment_ownership_cannot_be_overridden`: every native assigned name, equal/different values and collision refusal before revealing an already-released token.
- `test_claude_configuration_has_no_execution_effects`: fresh interpreter, no SDK import, unchanged threads/no loop and no observed construction resource events, with a real-read positive control. Audit events do not cover every native operation, so source inspection remains necessary.
- Invalid configuration, including unknown keys, missing placements and invalid launch-path checks retain fixed safe refusals without input text. Invalid-configuration cases also inspect the actual default-INFO diagnostics capture and find no emitted records.
- `test_claude_sdk_version_refusal_is_content_free`: malformed/unsupported SDK and bundled-CLI declarations pass through the same checker used by the actual SDK-options constructor. No native module or package is patched; the actual installed pair is exercised by integration.
- `test_installed_claude_extra_preserves_pure_configuration`: a built wheel installed after an offline `uv sync --locked --no-default-groups --no-build --no-install-project` into its own environment has neither the SDK nor development tools and constructs pure inputs; a separate runtime-plus-extra installation constructs real SDK options without pytest/development dependencies.

An earlier isolated macOS/Python 3.11.17 import/options probe passed for the selected SDK/MCP/HTTPX2/AnyIO versions. It observed only enumerated Python audit launch/connect/bind events and did not establish native lifecycle or whole-platform compatibility. The implementing PR records final source identity, actual focused/static results, complete final-head Linux/macOS CI and its total duration; those results are not asserted by this document before they exist.

TM-03/05 policy and candidate observations, TM-06/07 environment/secrets/content and TM-12 dependency/provider loading retain their accepted protections and residuals. No hostile-process isolation or safe arbitrary SDK import is claimed. Pure construction and fixed refusals do not prove safe operational SDK logging, telemetry or network behavior.

## Remaining activation

No HarnessAdapter, complete `reserved_env`, policy coverage, provider registration, same-call projection or whole validator is activated. Runtime-owned launch attestation, private-channel transport, binary digest/version handshake, descriptor exclusion, real policy-hook matrix, SDK stream/diagnostic handling, process cleanup and provider conformance retain work-item-14/15 ownership. #549 still owns configured selection, complete native declarations, normalization and validator integration; #593 still requires those sources and the saved execution-contract producer.

The latest inspected master mutation audit, run 41297342266 at `6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`, is a partial-budget pricing/CEL audit with 113 executed and zero reused outcomes. It supplies no applicable Claude-construction evidence and adds no campaign to this PR.

## Local implementation evidence

On macOS/Python 3.11.17, 47 non-packaging construction cases passed, followed by both installed baseline/extra cases after their setup repair. The 76 selected architecture, existing local-environment and executor-release checks passed. Strict Pyright reported zero errors/warnings. Final source/static checks and revision-bound native CI are recorded in the PR; these local results do not substitute for those supported-environment gates.

The new unknown-key regression initially reproduced raw key text in a validation error. Early fixed root rejection repaired that boundary. Initial formatting/assertion-style findings were corrected without suppression; a strict typing finding was corrected by returning the precisely typed mapping. Local package construction initially used host pnpm 9 instead of pinned 12.14.0; the corrected tool environment built the wheel. Its first dependency-install attempt then failed because exported name/hash requirements needed registry index metadata absent from the offline uv cache, despite locked wheel payloads being cached. The final installed test uses the existing lock directly through offline, no-build uv synchronization into the fresh target environment; it still excludes development groups and installs the built core wheel separately. No network, skipped test, relaxed assertion, timeout increase or dependency fallback was introduced. Historical failed runs remain failures.
