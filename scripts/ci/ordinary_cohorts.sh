set -eo pipefail

launch_children() {
  pids=()
  names=()
  evidence=()
  for arguments in "$KORA_ORDINARY_OUTPUT"/child-*.args; do
    test -f "$arguments"
    test ! -L "$arguments"
    test -s "$arguments"
    names+=("$(basename "$arguments" .args)")
  done
  for name in "${names[@]}"; do
    uv run --no-sync python -m tests.support.ordinary_pytest -v --exitfirst -o cache_dir="$KORA_ORDINARY_OUTPUT/cache-$name" --basetemp="$KORA_ORDINARY_OUTPUT/tmp-$name" --junitxml="$KORA_ORDINARY_OUTPUT/$name.xml" "@$KORA_ORDINARY_OUTPUT/$name.args" > "$KORA_ORDINARY_OUTPUT/$name.log" 2>&1 &
    pids+=("$!")
    evidence+=("$name.log" "$name.xml" "$name.exit")
  done
}

join_groups() {
  result=0
  for index in "${!pids[@]}"; do
    if wait "${pids[$index]}"; then
      status=0
    else
      status="$?"
      result=1
    fi
    printf '%s\n' "$status" > "$KORA_ORDINARY_OUTPUT/${names[$index]}.exit"
  done
}

require_group_evidence() {
  for name in "${evidence[@]}"; do
    test -f "$KORA_ORDINARY_OUTPUT/$name"
    test ! -L "$KORA_ORDINARY_OUTPUT/$name"
    test -s "$KORA_ORDINARY_OUTPUT/$name"
  done
}

print_group_logs() {
  for name in "${evidence[@]}"; do
    case "$name" in
      *.log) cat "$KORA_ORDINARY_OUTPUT/$name" ;;
    esac
  done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  test "$#" -eq 0
  launch_children
  join_groups
  require_group_evidence
  print_group_logs
  exit "$result"
fi
