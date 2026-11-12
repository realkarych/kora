set -euo pipefail

version=0.13.1
python_version=${1:?}

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    target=x86_64-unknown-linux-gnu
    architecture=x86_64
    ;;
  Darwin:arm64)
    target=aarch64-apple-darwin
    architecture=aarch64
    ;;
  *)
    printf 'Unsupported uv archive platform\n' >&2
    exit 1
    ;;
esac

cache_root=${RUNNER_TOOL_CACHE:?}
version_dir="$cache_root/uv/$version"
destination="$version_dir/$architecture"
complete="$destination.complete"

absent() {
  [[ ! -e $1 && ! -L $1 ]]
}

if [[ $cache_root != /* || ! -d $cache_root || $(cd -- "$cache_root" && pwd -P) != "$cache_root" ]]; then
  printf 'Invalid uv tool cache root\n' >&2
  exit 1
fi

for directory in "$cache_root/uv" "$version_dir"; do
  if ! absent "$directory" && [[ ! -d $directory || -L $directory ]]; then
    printf 'Invalid uv tool cache directory\n' >&2
    exit 1
  fi
done

existing=false
if [[ -d $destination && ! -L $destination && -f $complete && ! -L $complete &&
      -f $destination/uv && -x $destination/uv && ! -L $destination/uv &&
      -f $destination/uvx && -x $destination/uvx && ! -L $destination/uvx ]]; then
  existing=true
elif ! absent "$destination" || ! absent "$complete"; then
  printf 'Incomplete or invalid uv tool cache entry\n' >&2
  exit 1
fi

acquire="$(dirname -- "${BASH_SOURCE[0]}")/acquire.py"
store="${RUNNER_TEMP:?}/kora-acquisitions"
python3 "$acquire" fetch "$store" "cpython-$python_version-$target" > /dev/null
printf 'UV_PYTHON_INSTALL_MIRROR=file://%s\n' "$store" >> "${GITHUB_ENV:?}"
if [[ $existing == true ]]; then
  printf 'Using the existing uv %s tool cache entry\n' "$version"
  exit 0
fi

archive=$(python3 "$acquire" fetch "$store" "uv-$version-$target")

mkdir -p -- "$version_dir"
staging=$(mktemp -d "$version_dir/.kora-uv.XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
tar -xzf "$archive" -C "$staging"
extracted="$staging/uv-$target"
actual_version=$("$extracted/uv" --version)
[[ $actual_version == "uv $version "* ]]
mv -- "$extracted" "$destination"
: > "$complete"
