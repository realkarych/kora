#!/bin/sh
set -eu

case "$#:${1:-}" in
    0:|1:--check-cleanup|1:--check-write-stdin|1:--check-provider-env-key|1:--check-shell-controls|1:--check-executor-shell|1:--check-selected-launch|1:--check-selected-sqlite-home|1:--check-selected-bash-startup|1:--check-selected-bash-trace|1:--check-selected-tls-roots|1:--check-selected-proxy|1:--check-selected-noise-registry|1:--check-selected-tool-inventory|1:--check-selected-tool-search|1:--check-selected-startup|1:--check-selected-hook-inventory|1:--check-selected-gated-stdio|1:--check-selected-bound-environment|1:--check-selected-bound-slots|1:--check-selected-bound-fence|1:--check-selected-bound-installation|1:--check-selected-bound-checkout|1:--check-selected-turn|1:--check-selected-hook-failures|1:--check-selected-patch-hook-failures|1:--check-selected-shell-patch-policy|1:--check-app-server-rpc|1:--check-app-server-startup|1:--check-pre-tool-use|1:--check-app-server-usage|1:--check-app-server-interrupt|1:--check-app-server-errors|1:--check-app-server-messages|1:--check-app-server-output) ;;
    *)
        printf '%s\n' 'usage: sh run_hook_probe.sh [--check-cleanup|--check-write-stdin|--check-provider-env-key|--check-shell-controls|--check-executor-shell|--check-selected-launch|--check-selected-sqlite-home|--check-selected-bash-startup|--check-selected-bash-trace|--check-selected-tls-roots|--check-selected-proxy|--check-selected-noise-registry|--check-selected-tool-inventory|--check-selected-tool-search|--check-selected-startup|--check-selected-hook-inventory|--check-selected-gated-stdio|--check-selected-bound-environment|--check-selected-bound-slots|--check-selected-bound-fence|--check-selected-bound-installation|--check-selected-bound-checkout|--check-selected-turn|--check-selected-hook-failures|--check-selected-patch-hook-failures|--check-selected-shell-patch-policy|--check-app-server-rpc|--check-app-server-startup|--check-pre-tool-use|--check-app-server-usage|--check-app-server-interrupt|--check-app-server-errors|--check-app-server-messages|--check-app-server-output]' >&2
        exit 2
        ;;
esac

probe_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH='' cd -- "$probe_dir/../../../.." && pwd)
rpc_dir=$probe_dir/../../../../src/kora/plugins/harness/codex
if [ -n "${DOCKER_HOST:-}" ] && [ -z "${DOCKER_CONTEXT:-}" ]; then
    endpoint=$DOCKER_HOST
else
    context=$(timeout -k 2s 10s docker context show)
    endpoint=$(timeout -k 2s 10s docker context inspect "$context" --format '{{.Endpoints.docker.Host}}')
fi
case "$endpoint" in
    unix:///*) ;;
    *)
        printf '%s\n' '{"level":"ERROR","event":"probe.local_docker_required"}' >&2
        exit 1
        ;;
esac
buildx=$(timeout -k 2s 10s docker info --format '{{range .ClientInfo.Plugins}}{{if eq .Name "buildx"}}{{.Path}}{{end}}{{end}}')
if [ ! -x "$buildx" ]; then
    printf '%s\n' '{"level":"ERROR","event":"probe.buildx_required"}' >&2
    exit 1
fi
reservation_root=$(CDPATH='' cd -- "${TMPDIR:-/tmp}" && pwd -P)
reservation=$(mktemp -d "$reservation_root/kora-hook-probe.XXXXXXXX")
container="kora-hook-probe-${reservation##*.}"
container_id=
attach_pid=
create_pending=no

local_docker() {
    deadline=$1
    shift
    timeout -k 2s "$deadline" env -i PATH="$PATH" HOME="$reservation" \
        DOCKER_CONFIG="$reservation" DOCKER_HOST="$endpoint" docker "$@"
}

remove_container() {
    target=$container_id
    if [ -z "$target" ]; then
        if [ "$create_pending" = no ]; then
            return 0
        fi
        target=$(local_docker 10s container ls --all --quiet --filter "name=^/$container$") || return 1
    fi
    [ -n "$target" ] || return 1
    if ! local_docker 10s container rm --force "$target" >/dev/null 2>&1; then
        remaining_id=$(local_docker 10s container ls --all --quiet --filter "id=$target") || return 1
        [ -z "$remaining_id" ] || return 1
    fi
    remaining=$(local_docker 10s container ls --all --quiet --filter "name=^/$container$") || return 1
    [ -z "$remaining" ]
}

release_container() {
    if ! remove_container; then
        printf '{"level":"ERROR","event":"probe.container_cleanup_unconfirmed","container":"%s"}\n' "$container" >&2
        return 1
    elif [ -n "$container_id" ]; then
        printf '%s\n' '{"level":"INFO","event":"probe.container_removed"}' >&2
    fi
    container_id=
    create_pending=no
}

cleanup() {
    status=$1
    trap - EXIT
    trap '' INT TERM
    release_container || status=1
    if [ -n "$attach_pid" ]; then
        kill "$attach_pid" 2>/dev/null || :
        wait "$attach_pid" || :
    fi
    find "$reservation" -depth -delete || status=1
    if [ "$status" -ne 0 ]; then
        printf '%s\n' '{"level":"ERROR","event":"probe.run_failed"}' >&2
    fi
    exit "$status"
}

prepare_locked_probe() {
    mkdir "$reservation/source" "$reservation/uv-home" "$reservation/uv-tmp"
    fixture_revision=$(env -i PATH="$PATH" HOME="$reservation/uv-home" git -C "$repo_dir" rev-parse HEAD)
    set -- src/kora tests/plugins/harness/codex pyproject.toml uv.lock .python-version README.md
    env -i PATH="$PATH" HOME="$reservation/uv-home" \
        git -C "$repo_dir" ls-tree -r "$fixture_revision" -- "$@" > "$reservation/source-tree"
    awk '$1 != "100644" && $1 != "100755" { exit 1 }' "$reservation/source-tree"
    env -i PATH="$PATH" HOME="$reservation/uv-home" \
        timeout -k 2s 20s git -C "$repo_dir" archive --format=tar \
            --output="$reservation/source.tar" "$fixture_revision" "$@"
    env -i PATH="$PATH" tar -xf "$reservation/source.tar" -C "$reservation/source"
    rm "$reservation/source.tar"
    env -i PATH="$PATH" HOME="$reservation/uv-home" TMPDIR="$reservation/uv-tmp" \
        timeout -k 2s 120s sh -eu -c '
            cd "$1"
            probe_python=$(uv --no-config --no-cache --no-python-downloads python find --system 3.11.17)
            uv --no-config --no-cache --no-python-downloads --offline export \
                --locked --no-default-groups --extra conformance --no-emit-project \
                --project "$1/source" --python "$probe_python" \
                --output-file "$1/requirements.txt" >/dev/null
            uv --no-config --no-cache --no-python-downloads pip install \
                --default-index https://pypi.org/simple --require-hashes --only-binary :all: --no-deps \
                --python "$probe_python" --python-platform x86_64-manylinux_2_28 --python-version 3.11.17 \
                --target "$1/linux-packages" -r "$1/requirements.txt"
        ' sh "$reservation"
    probe_dir=$reservation/source/tests/plugins/harness/codex
    rpc_dir=$reservation/source/src/kora/plugins/harness/codex
}

probe_context() {
    set -- -C "$probe_dir" Dockerfile requirements.toml probe.py managed_hook.py interrupt_child.py owned_loopback.py selected_launch.py selected_proxy.py selected_tls.py \
        -C "$rpc_dir" app_server_rpc.py app_server_startup.py app_server_turn.py app_server_session.py app_server_tools.py app_server_usage.py app_server_errors.py app_server_messages.py pre_tool_use.py launch_configuration.py selected_startup.py
    case "$variant" in
        selected-turn|selected-shell-patch-policy|selected-gated-*)
            set -- "$@" -C "$reservation/source/src" kora -C "$reservation" linux-packages
            ;;
    esac
    if [ "$variant" = selected-shell-patch-policy ]; then
        set -- "$@" -C "$probe_dir" selected_shell_patch.py
    fi
    case "$variant" in
        selected-gated-*)
            set -- "$@" -C "$probe_dir" selected_gated_stdio.py selected_bound_environment.py
            ;;
    esac
    env -i PATH="$PATH" tar -cf - "$@"
}

run_variant() {
    variant=$1
    shift
    image=$(
        probe_context |
            local_docker 300s build --quiet --platform linux/amd64 --network none --target "$variant" -
    ) || return $?
    case "$image" in
        sha256:*) ;;
        *) return 1 ;;
    esac
    create_pending=yes
    container_id=$(
        local_docker 10s create --name "$container" --platform linux/amd64 \
            --pull never --network none --init --cap-drop ALL --security-opt no-new-privileges \
            "$image" "$@"
    ) || return $?
    create_pending=no
    timeout -k 2s 75s env -i PATH="$PATH" HOME="$reservation" \
        DOCKER_CONFIG="$reservation" DOCKER_HOST="$endpoint" docker start --attach "$container_id" &
    attach_pid=$!
    status=0
    wait "$attach_pid" || status=$?
    attach_pid=
    return "$status"
}

trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$reservation/cli-plugins"
ln -s "$buildx" "$reservation/cli-plugins/docker-buildx"
case "${1:-}" in
    --check-write-stdin)
        run_variant interactive --stdin-control
        release_container
        run_variant one-shot --stdin-one-shot
        ;;
    --check-provider-env-key)
        run_variant default --provider-account
        release_container
        run_variant default --provider-env-key
        ;;
    --check-shell-controls)
        run_variant shell-controls --shell-default
        release_container
        run_variant shell-controls --shell-login
        release_container
        run_variant shell-controls --shell-alternate
        ;;
    --check-executor-shell)
        for probe_case in default bash sh login deny stall; do
            run_variant shell-controls "--executor-$probe_case"
            release_container
        done
        ;;
    --check-selected-launch)
        for probe_case in allow deny key-absent command-metadata command-exit-seven; do
            run_variant shell-controls "--selected-$probe_case"
            release_container
        done
        ;;
    --check-selected-sqlite-home)
        for probe_case in default override; do
            run_variant shell-controls "--selected-sqlite-home-$probe_case"
            release_container
        done
        ;;
    --check-selected-bash-startup)
        for probe_case in default override; do
            run_variant shell-controls "--selected-bash-startup-$probe_case"
            release_container
        done
        ;;
    --check-selected-bash-trace)
        for probe_case in default shellopts command ps4; do
            run_variant shell-controls "--selected-bash-trace-$probe_case"
            release_container
        done
        ;;
    --check-selected-tls-roots)
        for probe_case in default codex-ca cert-file cert-dir; do
            run_variant shell-controls "--selected-tls-$probe_case"
            release_container
        done
        ;;
    --check-selected-proxy)
        for proxy_case in http-control upper-http lower-http upper-all lower-all \
            upper-bypass lower-bypass https-control upper-https lower-https; do
            run_variant shell-controls "--selected-proxy-$proxy_case"
            release_container
        done
        ;;
    --check-selected-noise-registry)
        for probe_case in default override; do
            run_variant shell-controls "--selected-noise-registry-$probe_case"
            release_container
        done
        ;;
    --check-selected-tool-inventory)
        for probe_case in allow disabled; do
            run_variant shell-controls "--selected-tool-inventory-$probe_case"
            release_container
        done
        ;;
    --check-selected-tool-search)
        run_variant shell-controls --selected-tool-inventory-allow
        release_container
        run_variant shell-controls --selected-tool-search
        ;;
    --check-selected-turn)
        prepare_locked_probe
        run_variant selected-turn --selected-turn-complete
        release_container
        run_variant selected-turn --selected-turn-cut
        ;;
    --check-selected-startup)
        for probe_case in ready bearer bash; do
            run_variant shell-controls "--selected-startup-$probe_case"
            release_container
        done
        ;;
    --check-selected-hook-inventory)
        run_variant shell-controls --selected-startup-ready
        release_container
        run_variant missing-managed-hook --selected-startup-hook-missing
        release_container
        run_variant wrong-managed-matcher --selected-startup-hook-matcher
        release_container
        ;;
    --check-selected-bound-environment|--check-selected-bound-slots)
        prepare_locked_probe
        run_variant selected-gated-stdio --selected-bound-ready
        release_container
        if [ "$1" = --check-selected-bound-slots ]; then
            set -- duplicate stopped
            refusal_reason=selected_bound_slot_refused
        else
            set -- path grant reserved
            refusal_reason=selected_bound_environment_refused
        fi
        for refusal do
            refusal_exit=0
            run_variant selected-gated-stdio "--selected-bound-$refusal" > "$reservation/refused" 2>&1 || refusal_exit=$?
            test "$refusal_exit" = 1
            printf '{"level": "INFO", "event": "probe.selected_bound_no_acquisition", "detail": "%s"}\n' "$refusal" > "$reservation/expected-refusal"
            printf '{"level": "ERROR", "event": "probe.case_failed", "detail": "%s"}\n' "$refusal_reason" >> "$reservation/expected-refusal"
            cmp -s "$reservation/refused" "$reservation/expected-refusal"
            cat "$reservation/expected-refusal"
            release_container
        done
        if [ "$refusal_reason" = selected_bound_slot_refused ]; then
            refusal_exit=0
            run_variant selected-gated-stdio --selected-bound-partial > "$reservation/refused" 2>&1 || refusal_exit=$?
            test "$refusal_exit" = 1
            printf '%s\n' \
                '{"level": "INFO", "event": "probe.selected_bound_executor_ready", "detail": "selected-startup-ready"}' \
                '{"level": "INFO", "event": "probe.selected_bound_executor_closed", "detail": "partial"}' \
                '{"level": "ERROR", "event": "probe.case_failed", "detail": "selected_bound_app_materialization_refused"}' > "$reservation/expected-refusal"
            cmp -s "$reservation/refused" "$reservation/expected-refusal"
            cat "$reservation/expected-refusal"
            release_container
        fi
        ;;
    --check-selected-bound-fence)
        prepare_locked_probe
        run_variant selected-gated-stdio --selected-bound-ready
        release_container
        for refusal in before-activation before-delivery; do
            refusal_exit=0
            run_variant selected-gated-stdio "--selected-bound-$refusal" > "$reservation/refused" 2>&1 || refusal_exit=$?
            test "$refusal_exit" = 1
            printf '%s\n' \
                '{"level": "INFO", "event": "probe.selected_bound_executor_ready", "detail": "selected-startup-ready"}' \
                '{"level": "INFO", "event": "probe.selected_bound_streams_closed", "detail": "selected-startup-ready"}' \
                '{"level": "INFO", "event": "probe.selected_children_reaped", "detail": "selected-startup-ready"}' \
                > "$reservation/expected-refusal"
            printf '{"level": "INFO", "event": "probe.selected_bound_fence_verified", "detail": "%s"}\n' "$refusal" >> "$reservation/expected-refusal"
            printf '%s\n' '{"level": "ERROR", "event": "probe.case_failed", "detail": "selected_bound_fence_refused"}' >> "$reservation/expected-refusal"
            cmp -s "$reservation/refused" "$reservation/expected-refusal"
            cat "$reservation/expected-refusal"
            release_container
        done
        ;;
    --check-selected-bound-checkout)
        prepare_locked_probe
        run_variant selected-gated-stdio --selected-bound-ready
        release_container
        for refusal in checkout-before-exec checkout-before-app; do
            refusal_exit=0
            run_variant selected-gated-stdio "--selected-bound-$refusal" > "$reservation/refused" 2>&1 || refusal_exit=$?
            test "$refusal_exit" = 1
            if [ "$refusal" = checkout-before-exec ]; then
                printf '%s\n' '{"level": "INFO", "event": "probe.selected_bound_no_acquisition", "detail": "checkout-before-exec"}' > "$reservation/expected-refusal"
            else
                printf '%s\n' \
                    '{"level": "INFO", "event": "probe.selected_bound_executor_ready", "detail": "selected-startup-ready"}' \
                    '{"level": "INFO", "event": "probe.selected_bound_executor_closed", "detail": "checkout-before-app"}' > "$reservation/expected-refusal"
            fi
            printf '%s\n' '{"level": "ERROR", "event": "probe.case_failed", "detail": "selected_bound_checkout_refused"}' >> "$reservation/expected-refusal"
            cmp -s "$reservation/refused" "$reservation/expected-refusal"
            cat "$reservation/expected-refusal"
            release_container
        done
        ;;
    --check-selected-bound-installation)
        prepare_locked_probe
        run_variant selected-gated-stdio --selected-bound-ready
        release_container
        for refusal in executable-pin executable-path; do
            refusal_exit=0
            run_variant selected-gated-stdio "--selected-bound-$refusal" > "$reservation/refused" 2>&1 || refusal_exit=$?
            test "$refusal_exit" = 1
            printf '{"level": "INFO", "event": "probe.selected_bound_no_acquisition", "detail": "%s"}\n' "$refusal" > "$reservation/expected-refusal"
            printf '%s\n' '{"level": "ERROR", "event": "probe.case_failed", "detail": "selected_bound_executable_refused"}' >> "$reservation/expected-refusal"
            cmp -s "$reservation/refused" "$reservation/expected-refusal"
            cat "$reservation/expected-refusal"
            release_container
        done
        ;;
    --check-selected-gated-stdio)
        prepare_locked_probe
        run_variant selected-gated-stdio --selected-gated-ready
        release_container
        run_variant selected-gated-missing --selected-gated-missing
        release_container
        run_variant selected-gated-matcher --selected-gated-matcher
        release_container
        refusal_exit=0
        run_variant selected-gated-stdio --selected-gated-refusal > "$reservation/refused" 2>&1 || refusal_exit=$?
        test "$refusal_exit" = 1
        printf '%s\n' \
            '{"level": "INFO", "event": "probe.selected_gated_refused_reaped", "detail": "gate_prepared"}' \
            '{"level": "ERROR", "event": "probe.case_failed", "detail": "selected_gated_start_refused"}' > "$reservation/expected-refusal"
        cmp -s "$reservation/refused" "$reservation/expected-refusal"
        cat "$reservation/expected-refusal"
        release_container
        ;;
    --check-selected-hook-failures)
        for probe_case in deny no-reason exit-one; do
            run_variant shell-controls "--selected-hook-$probe_case"
            release_container
        done
        run_variant selected-hook-failures --selected-hook-timeout
        release_container
        ;;
    --check-selected-patch-hook-failures)
        for probe_case in allow deny exit-one timeout; do
            run_variant selected-hook-failures "--selected-patch-$probe_case"
            release_container
        done
        ;;
    --check-selected-shell-patch-policy)
        prepare_locked_probe
        for probe_case in allow protected exit-one timeout; do
            run_variant selected-shell-patch-policy "--selected-shell-patch-$probe_case"
            release_container
        done
        ;;
    --check-app-server-rpc)
        run_variant default --client-complete
        release_container
        run_variant default --client-exit
        ;;
    --check-app-server-interrupt)
        for probe_case in model command kill; do
            run_variant one-shot "--interrupt-$probe_case"
            release_container
        done
        ;;
    --check-app-server-output)
        for probe_case in valid invalid kill; do
            run_variant default "--output-$probe_case"
            release_container
        done
        ;;
    --check-app-server-messages)
        for probe_case in plain stream kill; do
            run_variant default "--messages-$probe_case"
            release_container
        done
        ;;
    --check-app-server-errors)
        for probe_case in 401 429 503; do
            run_variant default "--error-$probe_case"
            release_container
        done
        ;;
    --check-app-server-startup)
        run_variant default --client-complete
        release_container
        run_variant default --startup-version
        release_container
        run_variant default --startup-home
        ;;
    --check-pre-tool-use)
        run_variant default
        release_container
        run_variant patch-hook --patch-allow
        release_container
        run_variant patch-hook --patch-deny
        release_container
        run_variant default --decoder-refuse
        ;;
    *) run_variant default "$@" ;;
esac
cleanup 0
