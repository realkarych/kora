# How harnesses resolve edit paths before writing (checked 2026-10-24)

The `allow_deny` policy judges file-edit calls by the path the hook receives. The harness then opens a file, and the policy's guarantee depends on the two naming the same file.

## Claude Code

- Read from the strings of the Claude Code 2.1.300 bundle, not from a live hook.
- The edit tools expand `file_path` and `notebook_path` before the call: the path is trimmed, a relative path is resolved against the session's working directory, and the result is an absolute path. The tools' observable input is replaced with that expanded path, and the `PreToolUse` hook appears to receive it.
- Unverified: whether the expansion collapses `..` textually before the file is opened, and whether every tested version hands the hook the expanded path. Work item 15 confirms both against the real harness.

## Codex

- `apply_patch` headers are trimmed lines. The research record `codex-hook-tool-input-2026-10-23.md` describes the envelope.
- Unverified: how the absolute-path type used for patch targets treats `..` after a symbolic link.

## Consequences for the policy

- The policy refuses a path whose lexical and kernel resolutions differ, so its decision does not depend on whether a harness collapses `..` before opening the file.
- The policy refuses a path with surrounding white space, so a harness that trims cannot write a different file than the policy judged.
