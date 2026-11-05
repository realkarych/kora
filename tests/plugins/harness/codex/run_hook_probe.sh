#!/bin/sh
set -eu

case "$#:${1:-}" in
    0:|1:--check-cleanup|1:--check-write-stdin|1:--check-provider-env-key|1:--check-shell-controls|1:--check-executor-shell|1:--check-app-server-rpc|1:--check-app-server-startup|1:--check-pre-tool-use|1:--check-app-server-usage|1:--check-app-server-interrupt|1:--check-app-server-errors|1:--check-app-server-messages) ;;
    *)
        printf '%s\n' 'usage: sh run_hook_probe.sh [--check-cleanup|--check-write-stdin|--check-provider-env-key|--check-shell-controls|--check-executor-shell|--check-app-server-rpc|--check-app-server-startup|--check-pre-tool-use|--check-app-server-usage|--check-app-server-interrupt|--check-app-server-errors|--check-app-server-messages]' >&2
        exit 2
        ;;
esac

probe_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
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
reservation=$(mktemp -d "${TMPDIR:-/tmp}/kora-hook-probe.XXXXXXXX")
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

run_variant() {
    variant=$1
    shift
    image=$(
        tar -cf - -C "$probe_dir" Dockerfile requirements.toml probe.py managed_hook.py interrupt_child.py \
            -C "$rpc_dir" app_server_rpc.py app_server_startup.py app_server_turn.py app_server_usage.py app_server_errors.py app_server_messages.py pre_tool_use.py |
            local_docker 300s build --quiet --platform linux/amd64 --network none --target "$variant" -
    )
    case "$image" in
        sha256:*) ;;
        *) return 1 ;;
    esac
    create_pending=yes
    container_id=$(
        local_docker 10s create --name "$container" --platform linux/amd64 \
            --pull never --network none --init --cap-drop ALL --security-opt no-new-privileges \
            "$image" "$@"
    )
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
