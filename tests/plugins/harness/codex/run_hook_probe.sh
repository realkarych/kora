#!/bin/sh
set -eu

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--check-cleanup" ]; }; then
    printf '%s\n' 'usage: sh run_hook_probe.sh [--check-cleanup]' >&2
    exit 2
fi

probe_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
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
    local_docker 10s container rm --force "$target" >/dev/null || return 1
    remaining=$(local_docker 10s container ls --all --quiet --filter "name=^/$container$") || return 1
    [ -z "$remaining" ]
}

cleanup() {
    status=$1
    trap - EXIT
    trap '' INT TERM
    if ! remove_container; then
        printf '{"level":"ERROR","event":"probe.container_cleanup_unconfirmed","container":"%s"}\n' "$container" >&2
        status=1
    elif [ -n "$container_id" ]; then
        printf '%s\n' '{"level":"INFO","event":"probe.container_removed"}' >&2
    fi
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

trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$reservation/cli-plugins"
ln -s "$buildx" "$reservation/cli-plugins/docker-buildx"
image=$(
    tar -C "$probe_dir" -cf - Dockerfile requirements.toml probe.py managed_hook.py |
        local_docker 300s build --quiet --platform linux/amd64 --network none -
)
case "$image" in
    sha256:*) ;;
    *) exit 1 ;;
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
cleanup "$status"
