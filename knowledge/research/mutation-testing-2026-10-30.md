# Bounded mutation audit

Checked: 2026-10-30. Scope and implementation plan approved under [#455](https://github.com/realkarych/kora/issues/455); [#456](https://github.com/realkarych/kora/issues/456) supplies the independently mergeable local executor. Hosted scheduling, native artifact provenance and agent-policy activation remain in #455.

## Scope and ownership

The local executor owns a disposable archive of one recorded master source revision, a fresh checkout-local dependency environment, generated native mutation inventory, pytest subprocess groups and bounded reports. It records the distinct tooling revision. Product packages and APIs are unchanged. Initial admitted scopes are `src/kora/ir/pricing.py` with `tests/ir/test_usage_pricing.py`, and `src/kora/cel/paths.py` with `tests/cel/test_cel_paths.py`. Broader components are not qualified by this audit.

Cosmic Ray 8.7.0 is pinned in the optional `mutation` group. It provides enumeration and one-at-a-time application; the executor owns test execution and classification. The native executor combines timeout and nonzero status into `KILLED`, so that value is not used as behavioral proof. A passing, nonempty original baseline is independently required. Owned fixture regressions prove that a real native mutation is detected and that an earlier inserted mutation site does not erase a preserved target's identity.

The dependency brings aiohttp, anybadge, attrs, click, decorator, exit-codes, gitpython, parso, rich, sqlalchemy, stevedore, toml and yattag, with their locked transitives. Existing runtime dependencies are unchanged. The pure-Python yattag dependency requires its legacy setuptools backend. Setuptools 84.2.1 is pinned in the build group. Installation first admits only locked wheels for that group without installing the project, then installs the optional mutation group with build isolation disabled. The backend runs from the admitted locked environment, with no separate unconstrained backend resolver. Existing ordinary wheel-only installations and native package build restrictions are unchanged. Lockfile generation uses the repository-pinned uv 0.13.1 rather than an older local executable that rewrites the lock format.

## Operating bounds

| Resource | Limit and meaning |
|---|---|
| Whole hosted job | 600 seconds; hosted activation belongs to #455 |
| Admission | At most 480 seconds including preparation and baseline, shortened by remaining job time |
| Setup/baseline command | 60 seconds within the shared admission deadline |
| Mutant test | 30 seconds within the shared admission deadline |
| Shutdown/reap | At most 5 seconds; live-child cleanup remains a blocking obligation |
| Captured ordinary command output | 4 MiB; archive transfer has its separate 128 MiB bound |
| Parsed state/native session file | 8 MiB |
| Source and mutable working data | 128 MiB; monitored execution refuses over-limit work |
| Locked dependency environment | Separate 256 MiB admission limit; excluded from mutable-state accounting |
| Mutation inventory | At most 10,000 entries |
| Hosted report retention | 14 days, activated under #455 |

The pre-existing uv package cache is intentional shared dependency data. It is not a mutation-result cache or owned disposable workspace, and no test payload is copied there deliberately. A cache location is configuration for the trusted package manager, not authority obtained from a downloaded report.

Limits are admission and refusal boundaries for this tooling, not throughput, RSS or hostile-process containment guarantees. The executor targets cooperative same-user processes on Linux/macOS. Output, timeout, interruption and mutable-space limits are separate inconclusive outcomes; they do not prove a behavioral assertion caught a mutant. An unfinished inventory remains pending. Only a fully attempted declared scope with conclusive outcomes can be called complete; it is never a whole-repository score.

## Identity, reports and follow-up

Source archives reject traversal, links, special files and oversized entries. Checkout-local imports are probed explicitly; source/test bytecode is removed before each test run. This also handles existing isolated Python children that ignore environment flags. A normal test exit, an infrastructure error and a closed output stream are distinct events. The process-group leader remains unreaped until signals have been sent to its owned group, preventing reuse of its identifier during cleanup.

The summary groups candidates by source definition and outcome; the JSON retains individual mutation addresses and fingerprints. A group can contain multiple distinct test gaps and requires contract-based triage. Baseline JUnit and current native inventory are retained. No artifact or diagnosis changes product authority, opens a PR automatically or grants merge permission.

Selector primitives distinguish regenerated native occurrence addresses from fingerprints based on source definition, operator and local expression context. Relevant source, tests and tooling inputs are hashed conservatively; unknown dependency reachability remains a limitation. Rotation and validated cross-run state activate with the hosted coordinator in #455; this local foundation does not claim a deployed periodic audit.

## Verification and limits

`tests/tooling/test_mutation_audit.py` exercises real pytest success, behavioral failure, setup/collection failure, empty input, output overflow, timeout, cancellation with a real descendant, bounded parsing, archive refusal and report semantics. `python -B -m tests.tooling.native_probe` exercises the pinned native tool on small fixture files. It is separate from product mutation campaigns and runs in ordinary CI on Linux/macOS.

Full coordinator execution and final-revision CI evidence are recorded in the implementing PR. Missing or failed evidence is not passing. The initial offline preparation failure and its owner-decision requirement remain recorded in issue comments; they are not relabeled as successful execution.

## External evidence

The [2021 scale study](https://homes.cs.washington.edu/~rjust/publ/practical_mutation_testing_tse_2021.pdf) supports bounded, actionable selection but evaluated review-time analysis, not a daily master census. The pinned [weekly reference workflow](https://github.com/rust-bitcoin/rust-bitcoin/blob/89cf8542e232ec3d9d3cf8b1fa36f2ac70466dea/.github/workflows/cron-weekly-cargo-mutants.yml) demonstrates a declared subset and retained findings. The [incremental-analysis limitations](https://stryker-mutator.io/docs/stryker-js/incremental/) motivate explicit input invalidation and fresh component passes; these are adaptations for kora rather than measured performance claims.
