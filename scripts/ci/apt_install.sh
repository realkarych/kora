set -euo pipefail

test "$#" -gt 0
test "$(id -u)" = 0

acquire="$(dirname -- "${BASH_SOURCE[0]}")/acquire.py"
options=(-q -o Acquire::Retries=0 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)
export DEBIAN_FRONTEND=noninteractive
python3 "$acquire" run 45 -- apt-get "${options[@]}" update
python3 "$acquire" run 60 -- apt-get "${options[@]}" install --yes --no-install-recommends "$@"
