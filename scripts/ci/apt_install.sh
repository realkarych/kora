set -euo pipefail

test "$(id -u)" = 0
test "$#" -gt 0
export DEBIAN_FRONTEND=noninteractive
directory="$(dirname -- "${BASH_SOURCE[0]}")"
interpreter=python3
target=(--default "$@")
if [[ ${1:-} == --runner-ubuntu-apt ]]; then
  test "$#" -eq 2
  test "$2" = nfs-kernel-server
  interpreter=/usr/bin/python3
  target=(--scoped-nfs)
fi
python3 "$directory/acquire.py" run 90 --   "$interpreter" -I -B "$directory/ubuntu_nfs_apt.py" "${target[@]}"
