# Offline configured-conformance inputs

Checked 2026-10-26 for [#280](https://github.com/realkarych/kora/issues/280), a library prerequisite within the [approved #215 implementation plan](https://github.com/realkarych/kora/issues/215#issuecomment-6369162911). The [owner authorization](https://github.com/realkarych/kora/issues/215#issuecomment-6369354298) covers the dependency pins and exact dialect/reference specification paragraph. The [precise schema-admission limitation](https://github.com/realkarych/kora/issues/280#issuecomment-6415188268) was additionally [approved by the project owner](https://github.com/realkarych/kora/issues/280#issuecomment-6415317657) on 2026-10-26. This change does not activate the configured command or its owned child.

## Dependencies and authority

`kora[conformance]` adds the already locked `jsonschema==4.26.0` and directly used `referencing==0.37.0`. The standard library parses JSON and existing Pydantic models generate their schemas; neither supplies validation of arbitrary provider-advertised JSON Schemas. No decided ADR-0013 stack row changes. The existing `types-jsonschema==4.26.0.20261006` development dependency provides strict typing; no format extra is enabled.

Installed distribution metadata and `uv.lock` were inspected on the date above:

| Distribution | Version | License expression | Python requirement |
| --- | --- | --- | --- |
| jsonschema | 4.26.0 | MIT | >=3.10 |
| referencing | 0.37.0 | MIT | >=3.10 |
| attrs | 26.1.0 | MIT | >=3.9 |
| jsonschema-specifications | 2025.9.1 | MIT | >=3.9 |
| rpds-py | 2026.9.1 | MIT | >=3.11 |
| types-jsonschema | 4.26.0.20261006 | Apache-2.0 | >=3.10 |

This reuses the [artifact-schema dependency set](vcs-ref-models-2026-10-23.md); no transitive version changes. uv manages the optional dependency edges and lock. The lock contains hashed rpds-py wheels for CPython 3.11 through 3.15 on Linux x86-64/aarch64 and macOS x86-64/arm64. The existing native-wheel-only policy includes rpds-py. Final matrix installation and scanner results remain required; wheel presence alone is not execution evidence.

The upstream [validation documentation](https://python-jsonschema.readthedocs.io/en/stable/validate/) describes schema checking and default annotation-only instance formats. Its [reference documentation](https://python-jsonschema.readthedocs.io/en/stable/referencing/) describes an explicit immutable registry. Installed source confirms that `Registry()` has no retriever; the validator combines it with packaged specification resources. Submitted `$ref` values never grant network or file access. Only encountered references must resolve; unused branches are not preflighted.

The installed typing stub excludes `None` from `validator_for`'s default parameter although the implementation supports it. A narrow callable protocol describes that real call and nullable return, without replacing validation or changing library behavior. Recognized and unknown dialect tests exercise the advertised root boundary. Nested schemas and embedded resources follow the upstream validator's evaluation rules, including fallback to its current dialect for an unknown nested `$schema`. The approved root selector is not a recursive dialect-admission mechanism, and no custom preflight traversal is added.

## Snapshots and refusal behavior

`kora.conformance.inputs.ConformanceInputs` privately retains canonical schema, configuration and exercise bytes. It uses the existing one-MiB/depth-16 JSON-object checks and rejects raw configuration/exercise inputs above one MiB before decoding. The schema snapshot precedes validation. Fresh parsed views and exact canonical instance-schema comparison preserve nested authority and all fields, including defaults and ordered arrays.

The module retains neither validators nor upstream exceptions. Scoped handlers return fixed reasons; the public error is raised after those handlers have exited, so no library exception survives as its cause or context. Malformed reference/dialect URLs can otherwise expose private hostnames through `urllib` errors. The negative corpus includes those paths, malformed schema patterns, encoding failures and recursive references. A reference may also encounter annotation contents that root meta-schema checking did not validate as a schema: unknown types, malformed regexes and non-schema targets are translated from the concrete upstream UnknownType, regex, TypeError, AttributeError and zero-division failures into schema_invalid. Unvisited annotation contents remain ordinary data. Default traceback rendering contains only the fixed reason; raw input and library messages do not enter operational diagnostics. The later command still owns capture of external pytest/provider output.

Root meta-schema checking does not independently validate every schema reached through an annotation pointer. The schema `{"type":"object","default":{"required":"x"},"$ref":"#/default"}` accepts configuration `{"x":1}` in the pinned validator even though the target's required value is malformed. The owner-approved guarantee is the upstream root check plus configuration validation, not complete referenced-target well-formedness. A named regression records this behavior. Such acceptance may permit later authorized fixture preparation/construction; installed providers remain trusted code. No existing check is bypassed or removed.

Two upstream evaluation limits are explicit refusals, not successful validation: recursive evaluation can exceed the interpreter recursion limit, and jsonschema 4.26.0 can raise `OverflowError` when validating a bounded integer such as `10**400` against fractional `multipleOf: 0.5`. Its division happens before the keyword's overflow handler. `schema_evaluation_unavailable` distinguishes this valid-input limitation from `configuration_invalid`; no keyword is bypassed and no authority to construct a provider is granted. Tests also retain an ordinary successful numeric case.

Byte/depth limits bound admitted snapshots, not validation time or peak memory. Recursive schemas and expensive patterns remain possible. The approved owned-child phase must bound execution before #215 activates the command; this pure library is not a serving-process validator or hostile-code sandbox. Threat coverage is TM-04, TM-06/TM-07, TM-10, TM-11 and TM-12 under the existing decisions.

## Verification boundary

`tests/conformance/test_conformance_inputs.py` exercises parser and real validator behavior, canonical bounds, defensive views, actual draft-dependent keyword semantics, embedded references, unused reference branches, fixed refusals and exact instance-schema matching. An in-process HTTP server and a real temporary schema file are available but unresolved references are refused; the HTTP server receives no requests. Existing installed-listing tests build wheels and run without the conformance extra. The lightweight-plugin import check additionally excludes jsonschema and referencing.

Correctness and security independently review this boundary. No controller, runtime resource owner, numerical acceptance method or CI topology changes. Full owned command/fixture lifecycle and operational event activation remain #215; existing lifetime evidence is neither rerun nor presented as proof of that future integration. The implementing PR records actual checks, reviewed revision and final-head CI.
