# Run failure-summary ordering, 2026-11-13

This record explains the internal scheduler reader under [#1445](https://github.com/realkarych/kora/issues/1445) and the selected identifier contract of [#555](https://github.com/realkarych/kora/issues/555). It introduces no public route, persisted representation or collation registration.

## Selection precedes admission

The event includes the exact count of failing top-level instances and at most 20 entries. SQL orders `failed` before the other failing states, then orders original paths by code point, before applying the limit. Only selected original paths are admitted and decoded. A legacy path outside the selected IR identifier domain could otherwise displace the twentieth valid entry. A selected invalid value refuses the whole summary, never a smaller sample or substitute entry.

PostgreSQL uses `COLLATE "C"`. SQLite BINARY compares the database encoding's bytes, so UTF-16LE can order `a\u0100` ahead of `az`, although code-point order puts `az` first. Sorting an already limited Python result cannot repair selection.

The SQLite key is `(node_path || char(0)) COLLATE RTRIM`. The [SQLite implementation](https://raw.githubusercontent.com/sqlite/sqlite/master/src/main.c), checked on 2026-11-13, registers RTRIM only for UTF-8; its trailing-space removal is followed by length-aware binary comparison. Appending the minimal code point prevents space trimming while preserving lexicographic order, including prefixes and embedded NUL. UTF-8 comparison gives code-point order for valid Unicode. The suffix is neither returned nor stored. No Unicode-order claim is made for malformed byte sequences.

## Admission and evidence

The selected original must be text and fit its full native-byte allowance before projection. SQLite returns at most 128 bytes per path, enough for 64 ASCII characters in UTF-16; PostgreSQL returns at most 64 UTF-8 bytes. A finite encoding code comes from the same statement. Strict decoding and the exact IR identifier grammar reject malformed bytes, non-text values, NUL, whitespace, non-ASCII characters and overlong originals. SQL projects only closed applied status/error-class codes for the greatest physical attempt number; outcome and error payloads are never projected.

`test_failure_summary_selects_by_code_point_before_limit_in_each_encoding` exercises the twentieth-entry boundary on UTF-8, UTF-16LE and UTF-16BE, including non-ASCII, trailing-space and NUL competitors inserted before the selected valid path. `test_selected_native_sqlite_path_is_strictly_admitted` uses malformed, BLOB and NUL legacy values through native SQL. Both-database cases cover full original-byte admission, failed-first order, exact count, foreign scope, latest physical status/class and whole-result refusal.

The result holds at most 20 paths: at most 2,560 transported native path bytes on SQLite or 1,280 on PostgreSQL. SQL count/selection may scan and sort the complete owned top-level domain; concatenation and comparisons operate on retained widths. These result bounds establish no native sort-memory limit, constant or linear count-plus-sort work, or elapsed-time guarantee.

`test_failure_summary_observes_only_its_owned_top_level_domain` captures the actual statement and native plan on both databases with 30 failing top-level instances, 100 body instances and 1,000 foreign instances. Both plans use the run/top-level index for selection. An initial two-step scalar query selected the latest attempt ID before reading its fields; PostgreSQL then chose 40 scans of the small 60-row attempt table. The final query selects each finite status/class code directly under the node-instance predicate, descending attempt number and limit 1. Both final plans use the attempt-number index for these 40 reads, without the redundant attempt-ID scans. This fixture evidence verifies scoped selection and payload exclusion, not an arbitrary-history physical-work bound. The query and plan artifacts belong to the PR verification evidence; their timing and sort-memory observations are diagnostic only.
