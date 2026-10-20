# CEL conformance data

The `.textproto` files in `testdata/` are copied byte for byte from the
[cel-spec](https://github.com/google/cel-spec) repository, directory
`tests/simple/testdata`, at commit `40a3c9007a9305d1f2638d5f538a4e011732cace`.
They are distributed under the Apache License 2.0; `LICENSE` is the upstream
licence file from the same commit. The repository has no NOTICE file.

`manifest.py` records the SHA-256 of every file and of the licence, and
`test_cel_conformance_data_matches_manifest` fails when a file differs, so the
copies stay unmodified. A change to `manifest.py` comes only with a new upstream commit, after the files are compared with that commit again. `selection.py` names the included files, the excluded
files with the features they need, and the cases kora rejects, each with the
feature and the diagnostic reason. The dated record of the source, the commit and
the digests is
[the conformance data research record](../../../knowledge/research/cel-conformance-data-2026-10-20.md).
