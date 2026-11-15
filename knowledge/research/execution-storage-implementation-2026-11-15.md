# Execution storage implementation progress

Started on 2026-11-15 for [#1640](https://github.com/realkarych/kora/issues/1640), from `9429aabbcf1a013a9cfe86400ba86516fa354c5d`. This record describes unfinished implementation. It is not a storage activation, completed Worker API delivery or merge-readiness report.

## Authorized delivery

The owner authorized taking over the unowned storage prerequisite and exceeding 1,000 production changed lines for its first complete implementation. The exception covers native custody plus the real authenticated request-upload, ready/recovery and refusal-cleanup consumer. It is not a general repository exception, new architectural decision or relaxation of integration and qualification gates. The owner supplied no numerical upper cap. The planning estimate is 2,700–4,350 production changed lines; the final full diff remains to be measured.

The [implementation plan](https://github.com/realkarych/kora/issues/1640#issuecomment-6900600444) and [clarification](https://github.com/realkarych/kora/issues/1640#issuecomment-6900894097) retain sealing under #1453 and reports under #1454. No helper-only intermediate merge is intended.

## Original transaction outcome

`db.custody_transaction.OriginalCustodyTransaction` retains the actual original SQLAlchemy transaction and its immutable binding. One completion attempt is admitted. It captures committed, affirmative rollback or unknown inside the shielded completion task, before cancellation returns to the requester. Later rollback, close, repeat calls or a replacement transaction cannot change that result. A failed COMMIT remains unknown even if later cleanup succeeds.

The existing cancellation-completion functions move unchanged to `db.completion`; existing `db.connection` consumers keep their imports. This avoids a cycle when the database writer and future custody coordinator share native completion ownership.

This primitive does not establish qualified native identity, journal confirmation or permission for a storage effect. Its integration fixtures exercise real transactions; their inert binding values are not qualification or domain-authority producers. Journal admission, all-writer participation, provisioning, restore, upload and cleanup are still pending. The strict record domains in `db.custody_models` are preliminary internal types, with their persisted readers and migration evidence still pending.

## Local checks

The implementation checks ran on macOS with CPython 3.11.17, real SQLite and PostgreSQL, and the existing shared project environment. No dependencies, deadlines or check configurations changed.

- `pytest -q tests/db/test_custody_transaction.py`: 10 passed before the additional cancellation-with-failed-COMMIT cases; JUnit SHA-256 `cd72cc9680b65a261eddfe9e957fe4e1de8f8218074ca6ac14bcf2055dc789ad`.
- `pytest -q tests/db/test_custody_transaction.py tests/db/test_timed_writer_admission.py tests/db/test_read_snapshot.py tests/architecture/test_no_suppression.py tests/architecture/test_import_layers.py`: 127 passed in 34.45 seconds, including 12 new both-database native-outcome cases; JUnit SHA-256 `7dc9c63550d6e616d115866c1ba9e71dcd66a1783b0ba1bf67239ea79fa40d19`.
- Whole-tree `ruff check --ignore-noqa .`, `ruff format --check .` and strict Pyright: exit 0. Ruff format checked 1,652 files. These include the later narrowing of process identity strings to their existing no-space native domain; that preliminary process decoder is outside the transaction tests' exercised behavior.
- Targeted `markdownlint-cli@0.45.0 --ignore-path /dev/null` checks the environment requirements despite the ordinary research-directory ignore. Exit 0. This adds a local document check; it changes no repository policy.

Execution used `PYTHONPATH=src`, `KORA_TEST_DATABASES=sqlite,postgresql`, `TESTCONTAINERS_RYUK_DISABLED=true`, the pre-existing `UV_PROJECT_ENVIRONMENT`, and the already-approved Pyright `NODE_OPTIONS=--max-old-space-size=4096`. Tests and static checks were against an uncommitted working tree on the stated base, not a final published revision. Current source/test snapshot SHA-256 is `578a2a80f0df3eb129c21664ae856ae95517d85b09aef9c5a3d4bd09f7a304ef`; it includes the preliminary process-domain narrowing after the transaction run. The complete eventual final revision still requires its own mandatory CI.

Earlier local failures were repaired without changing checks: file-level Pyright reported three redundant enum type checks in the initial record draft; file-level Ruff reported two overlong test lines and a compound `pytest.raises` body. The final recorded static commands passed. The first Markdown invocation selected no research file because of its ignore; it is not counted as a successful file check.

The latest available successful master mutation audit remains [43705847856](https://github.com/realkarych/kora/actions/runs/43705847856), source `2dff3824df3b93d2a0937300219a3978c770cec5`, checked again during this work. Its previously inspected partial pricing/CEL scope contains no applicable custody finding. No PR mutation campaign is added.

## Qualification and remaining integration

The owner confirmed that there is no disposable qualification environment and requested [its requirements](custody-qualification-environment-2026-11-15.md). No destructive experiment has run and no production activation is available. Missing physical evidence does not stop ordinary implementation; it remains a required, separate input to final qualification.

The complete request validator and its admitted native allocation owner, a real eligible other-owner producer for shared-address cleanup, all owner-kind indexed lookups, bounded run-open catch-up and cleanup pacing must be reconciled before the affected consumer is implemented. The issue clarification names their existing obligations and the planned threat/diagnostic coverage. Unresolved prerequisites are not passing acceptance evidence. Full implementation review, final-head CI, scanners and postmerge master CI remain pending.
