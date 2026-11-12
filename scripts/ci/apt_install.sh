set -euo pipefail

attempt() {
  local options=(-q -o Acquire::Retries=0 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)
  if compgen -G '/var/lib/dpkg/updates/*' > /dev/null; then
    printf 'Recovering an interrupted dpkg run\n' >&2
    if ! dpkg --configure -a; then
      printf 'dpkg could not recover from an interrupted run\n' >&2
      return 1
    fi
  fi
  apt-get "${options[@]}" update
  apt-get "${options[@]}" install --yes --no-install-recommends "$@"
}

test "$(id -u)" = 0
export DEBIAN_FRONTEND=noninteractive
if [[ ${1:-} == --attempt ]]; then
  shift
  test "$#" -gt 0
  attempt "$@"
else
  test "$#" -gt 0
  python3 "$(dirname -- "${BASH_SOURCE[0]}")/acquire.py" run 90 -- \
    bash "${BASH_SOURCE[0]}" --attempt "$@"
fi
