# Codex private launch construction and combined Linux evidence

[#781](https://github.com/realkarych/kora/issues/781) adds one private, standard
library constructor for both native launch recipes. The Linux fixture imports
the exact production source. No configured harness, runtime launch,
Policy client, complete native facts or common runner is activated.

## Inputs and reproduction

Native executable: Codex CLI 0.166.0, upstream
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The Linux amd64 musl archive
SHA-256 is `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The Python 3.11.17 amd64 image digest is
`sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`.
The [original probe](codex-managed-hook-probe-2026-11-04.md) describes Docker
Engine, Buildx and build asset prerequisites. The new mode uses the existing
unprivileged shell-controls image with managed one-shot execution and disabled
login shells. It runs without host mounts, credentials or external model
calls. The two processes connect only to the test container's loopback.
The image and binary are downloaded on an uncached build.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
```

Each of the three cases uses a separate disposable container. The launcher
constructs both role recipes once, creates their private homes, then starts
exec-server with a digest in argv and registers its fresh synthetic bearer
through app-server stdio. The driver checks that the local executor is
unavailable and the registered executor reports the expected Bash, then
selects only that environment through app-server.
The fixture supplies a synthetic API key to the app-server environment only,
or leaves it absent for the negative case. The two model replies for allow and
deny contain the same fixed shell call and no secret value. The 30-second
case, 5-second shared cleanup and 75-second host attach limits are unchanged.
The Responses listener remains open to count connections until native
cleanup. Child settlement and listener settlement each use the same absolute
cleanup cutoff, including the timeout path. Docker removal is the host
fallback and a separate observed check.

## Observed behavior

Observed on 2026-11-05 (Europe/Moscow), Linux amd64 Python 3.11.17 inside an
aarch64 LinuxKit 7.0.14 VM under Docker Desktop 29.9.2 and Buildx 0.38.1.

| Case | Observed boundary and outcome |
| --- | --- |
| Allow | Two exact synthetic-authenticated Responses requests. One managed Bash hook received the fixed command. The actual selected command verified that `CODEX_API_KEY` was unset and that it was non-login Bash before writing the marker with its Bash path and executor role home. Native turn completed. |
| Deny | Two exact synthetic-authenticated Responses requests. One managed Bash hook received the identical command, and Codex reported the fixed hook refusal. The marker was absent after the completed turn and cleanup. |
| Missing key | Codex returned the pinned missing-variable error and a failed turn. Zero model connections and requests were observed through native shutdown; no hook receipt, invocation or marker appeared. |

Each case reaped both direct native children, settled the Responses listener
and confirmed container removal. Neither role home contained `auth.json` or
`environments.toml` at the final inspection. These are named fixture
observations, not a blanket assertion about every native file or channel.

The executable result is associated with clean kora commit
`6d9e7b7683e3273cc2fd1ab989ff5d2ccfd3ec4b`, Codex provider tree
`60ceba780a47fd5d26c54640463a783dfbb9cfbe` and Linux fixture tree
`0e0f1bc37791a761da8b3ff98d231aa09279351b`. The output does not embed Git
identity; the association is coordinator-recorded. The command exited zero
in 22.820 seconds wall time. Fixed diagnostic log SHA-256:
`7bc8782c7c104362d707a75523c9258098bb6aa204e0dce46bf883e6531be235`.
The initial log has the same fixed records and hash; its execution took
27.598 seconds. A final read-only Docker inventory found no remaining probe
containers. Timings are diagnostic.

## Focused checks and previous paths

At the initial executable revision `09c41bb5e16be8582389a448e45eccba2caef3b8`,
all three combined Linux cases passed and left no probe containers.
The following previous real native modes passed at that executable input
(provider-key three cases/two removals, selected executor six cases/six
removals, original hook two cases/one removal):

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-provider-env-key
sh tests/plugins/harness/codex/run_hook_probe.sh --check-executor-shell
sh tests/plugins/harness/codex/run_hook_probe.sh
```

All three regression commands exited zero (9.064, 68.73 and 8.178 seconds,
respectively). Their fixed diagnostic logs have SHA-256 values
`fa200a5b024b81e75a9e2d355d4b65b729ed45faee5558164953d3101aaf39b5`,
`b2dc9c3d7cf27839e627acdf919270f602d3611baa1f8f3435239eb919a0f2f4` and
`3625b8358765f8d9b931a694d33a52df3565c36582dbaf0357764b0089a5d788`.

The old selected-executor cases retain the deliberately contrary requested
login/interactivity controls, alternate shell checks and stalled hook. The
subsequent source delta changes only the new constructor's invalid name input
and the new combined mode's cleanup branch. It does not change those previous
fixture paths. The original results are retained at their actual revision.

Python 3.11.17 on macOS 27.2 arm64, locked uv 0.13.1 environment:

```sh
uvx --from uv==0.13.1 uv run --no-sync pytest -q \
  tests/plugins/harness/codex tests/architecture --junitxml=/tmp/kora-781-focused.xml
uvx --from uv==0.13.1 uv run --no-sync pytest -q \
  tests/plugins/harness/codex/test_launch_configuration.py \
  --junitxml=/tmp/kora-781-launch-final.xml
```

At initial executable inputs, 1,511 tests passed (930 architecture and 581
Codex), with zero failure/error/skip, wall 41.06 seconds and XML SHA-256
`82a88610a50e38ab9c182f565a185b84c45e8a234f2219668b025ce767d96969`.
The changed launch inputs then passed 35 focused tests including both scalar
owner refusals, zero failure/error/skip, in 0.57 seconds wall time, with XML
SHA-256 `8d96e8792c68324d2a377fec6bf412b08a04dca97c47712b31c8292be93a3f98`.
Both pytest commands exited zero. XML associations are coordinator-recorded.
Scoped Ruff format/lint with `--ignore-noqa`, strict Pyright, shell syntax and
Git whitespace checks passed. No settings or suppressions changed.

The real TOML parser checks the closed native values and the
fresh interpreter audit observes no resource operations during construction,
with a real file read as its positive control. Source inspection checks for
operations the audit hook cannot observe. The tests use no model credentials.

## Limits and pending owners

Lexical path and URL validation supplies no physical path binding, endpoint
authentication, trusted provider routing, TLS/egress or network admission.
This probe establishes matching-bearer execution, not wrong/stale/cross-attempt
bearer refusal, hostile local isolation, independent descendant closure or
complete native resource bounds. The selected Bash unset-name assertion applies
to this command and profile only. It does not establish broader candidate-tool
key secrecy under DD-30. Native macOS qualification remains pending.

[#199](https://github.com/realkarych/kora/issues/199) owns actual launch, Policy,
enforcement and resource closure. [#549](https://github.com/realkarych/kora/issues/549)
owns selected instance projection and consuming validation. Complete
`reserved_env` and `policy_coverage` remain unavailable. No new dependency,
public configuration or platform is added.
