# VcsRef model boundary and verification dependencies

Checked 2026-10-23 for [issue #209](https://github.com/realkarych/kora/issues/209). The [approved implementation plan and exact documentation wording](https://github.com/realkarych/kora/issues/209#issuecomment-6354475309) are [owner-approved](https://github.com/realkarych/kora/issues/209#issuecomment-6354560818). This record supplies implementation evidence, not publication authority or a new artifact revision.

## Independent schema validator

The existing Pydantic runtime publishes the actual validation and serialization schemas. An independent implementation is needed to compare their structural domain with the frozen draft-2020-12 artifact schema. The standard library does not implement that validator; the existing check-jsonschema command uses the same upstream implementation but is not a project pytest dependency.

The development group adds [jsonschema 4.26.0](https://pypi.org/project/jsonschema/4.26.0/) and [types-jsonschema 4.26.0.20261006](https://pypi.org/project/types-jsonschema/4.26.0.20261006/). The latter supplies the upstream typeshed stubs because the validator wheel has no `py.typed` marker. A narrow typed interface describes its `is_valid` method at the test boundary; it calls the real independent validator. No replacement schema engine, runtime import, format extra or network retrieval is introduced.

uv 0.13.1 resolved the development dependencies into `uv.lock`. Locked versions introduced by this change are:

| Distribution | Version | Purpose |
|---|---|---|
| jsonschema | 4.26.0 | Independent full draft-2020-12 validation |
| types-jsonschema | 4.26.0.20261006 | Strict static typing of the test-only validator |
| attrs | 26.1.0 | Validator/reference-library records |
| jsonschema-specifications | 2025.9.1 | Packaged specification resources |
| referencing | 0.37.0 | Explicit in-memory schema reference registry |
| rpds-py | 2026.9.1 | Immutable reference-registry data structures |

The lock includes hashes and binary wheels, including rpds-py's supported Linux/macOS interpreter/platform combinations. Local installation used `uv sync --locked --no-build --group build --extra postgres --extra cel --no-install-project`, followed by the existing hash-constrained editable-project install. No dependency was built from source. CI repeats the locked no-build installation across the supported matrix; final scanner and matrix results remain required before merge.

## Immutable boundary

The frozen schema hash is `6cb29467d126b9a5f324408568d6925795946dde67ba927244a844f3169a86a8`. The published 298-byte canonical vector hashes to `e978ea1463001e37edfb3970b0e3352cc929fde73efe5958958aedd9c9b5c955`. Schema, manifest and vector bytes are pinned by the model tests. Schema comparisons remove only nonsemantic `title` annotations; defaults, nullability, references, pairing, uniqueness, patterns, required fields and bounds remain.

The URL predicate follows [RFC 3987 section 2.2](https://www.rfc-editor.org/rfc/rfc3987.html#section-2.2) and the artifact's fixed restrictions. Character ranges and ASCII port comparison are explicit. The standard library IPv6 parser is used only after the bounded ASCII IP-literal shape is established. No normalization, IDNA mapping, URI decoding or network resolution participates in identity.

## Measurement ownership

The existing required IR command includes separate phase/action workloads and three serial traced/untraced process pairs, each with 500 operations, quiescent points every 25 and retaining-path summaries at 250/500. The report records exact fixture dimensions, all action samples and resource trajectories. Caller-owned oversized objects are prepared before measured baselines; constructor keyword transfer remains part of the constructor action. Inputs include 100,000 extra keys, 10,001 paths, 10,000 unique 256-character paths and 8 MiB display/URL values.

Cold public imports compare exact base `85577d27e0fe38fb320571928b8a12e96fdc2e6a` with the current source in three fresh process pairs under one interpreter/dependency environment. The test materializes the base from local Git history and copies the head source without bytecode caches, so both import fresh source trees. The existing measurement checkout fetches full history; tests perform no network access. Temporary trees, pipes and child processes are released. Import allocations are intentionally retained module state, measured separately from repeated disposable values.

The ten-second child deadline, 256 KiB per-stream output cap, strict less-than-65,536-byte retained-Python gate and ten-minute workflow budget remain. Linux native-inclusive RSS/counters and final-revision artifacts remain required. Timings and RSS are first-implementation baselines; unexplained continuing growth blocks readiness. Publication, recovery, uploads, storage, authorization and rendering evidence remain with their implementing components.
