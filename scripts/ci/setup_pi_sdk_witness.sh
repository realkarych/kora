set -euo pipefail
umask 077

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
if [[ ${1:-} == changed-test && $# == 1 ]]; then
  status=0
  git -C "$repo" diff --quiet --no-ext-diff --no-textconv --diff-filter=AMR HEAD^1 HEAD -- \
    tests/plugins/runtime/local/test_pi_native_launch.py || status=$?
  case $status in
    0) exit 0 ;;
    1) ;;
    *) exit "$status" ;;
  esac
else
  [[ $# == 0 ]]
fi

case "$(uname -s):$(uname -m)" in
  Linux:x86_64) platform=linux-x64 ;;
  Darwin:arm64) platform=darwin-arm64 ;;
  *) exit 1 ;;
esac

export RUNNER_TEMP="$(cd -- "${RUNNER_TEMP:?}" && pwd -P)"
[[ $RUNNER_TEMP != "$repo" && $RUNNER_TEMP != "$repo/"* && -O $RUNNER_TEMP ]]
stage=$(mktemp -d "$RUNNER_TEMP/kora-pi-sdk.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

node_archive=$(python3 "$repo/scripts/ci/acquire.py" fetch \
  "$RUNNER_TEMP/kora-acquisitions" "node-24.22.0-$platform")
pnpm_archive=$(python3 "$repo/scripts/ci/acquire.py" fetch \
  "$RUNNER_TEMP/kora-acquisitions" "pnpm-12.14.0-$platform")
build="$stage/build"
bridge="$stage/installed/bridge"
node="$stage/installed/node"
mkdir -p -- "$build/home" "$build/bridge" "$bridge"

node_member="node-v24.22.0-$platform/bin/node"
tar -xOzf "$node_archive" "$node_member" > "$node"
tar -xOzf "$node_archive" "$node_member" | cmp -s -- "$node" -
tar --no-same-owner --no-same-permissions -xzf "$pnpm_archive" -C "$build"
tar -xOzf "$pnpm_archive" pnpm | cmp -s -- "$build/pnpm" -
chmod 700 "$node" "$build/pnpm"

private_env=(env -i "PATH=/usr/bin:/bin" "HOME=$build/home" "TMPDIR=$build/home" "CI=true")
[[ $("${private_env[@]}" "$node" --version) == v24.22.0 ]]
[[ $("${private_env[@]}" "$build/pnpm" --version) == 12.14.0 ]]

source="$repo/src/kora/plugins/harness/pi/bridge"
[[ $(cd -- "$source" && pwd -P) == "$source" ]]
for name in package.json pnpm-lock.yaml tsconfig.json bridge.ts; do
  [[ -f $source/$name && ! -L $source/$name ]]
  cp -- "$source/$name" "$build/bridge/"
done

install=(RAYON_NUM_THREADS=1 "$build/pnpm" install --frozen-lockfile --ignore-scripts --ignore-pnpmfile
  --ignore-workspace
  --registry=https://registry.npmjs.org/ --store-dir="$build/store"
  --config.manage-package-manager-versions=false --config.package-import-method=copy
  --config.node-linker=hoisted --config.bin-links=false)
(
  cd -- "$build/bridge"
  python3 "$repo/scripts/ci/acquire.py" run 75 -- "${private_env[@]}" "${install[@]}" >&2
  python3 "$repo/scripts/ci/acquire.py" run 40 -- "${private_env[@]}" "$node" \
    node_modules/typescript/bin/tsc --project tsconfig.json >&2
)
cp -- "$source/package.json" "$source/pnpm-lock.yaml" "$bridge/"
(
  cd -- "$bridge"
  python3 "$repo/scripts/ci/acquire.py" run 40 -- "${private_env[@]}" "${install[@]}" --offline --prod >&2
)
cmp -s -- "$source/pnpm-lock.yaml" "$bridge/pnpm-lock.yaml"
rm -- "$bridge/pnpm-lock.yaml"
rm -rf -- "$bridge/node_modules/.bin"
mkdir -- "$bridge/dist"
cp -- "$build/bridge/dist/bridge.js" "$bridge/dist/"
rm -rf -- "$build"

python3 -I "$repo/scripts/ci/pi_sdk_manifest.py" "$stage"
if [[ -n ${GITHUB_ENV:-} ]]; then
  printf 'KORA_PI_SDK_STAGE=%s\n' "$stage" >> "$GITHUB_ENV"
fi
trap - EXIT INT TERM
printf '%s\n' "$stage"
