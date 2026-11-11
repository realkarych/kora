set -eo pipefail

admit_lane() {
  case "$KORA_SCAFFOLD_OS:$KORA_SCAFFOLD_LANE" in
    ubuntu-24.04:conformance|ubuntu-24.04:remainder|ubuntu-24.04:integration|ubuntu-24.04:storage|ubuntu-24.04:local-runtime) ;;
    macos-15:conformance|macos-15:remainder|macos-15:storage|macos-15:local-runtime) ;;
    *) exit 1 ;;
  esac
}

open_ordinary_output() {
  KORA_ORDINARY_OUTPUT="$(mktemp -d "$RUNNER_TEMP/kora-ordinary.XXXXXX")"
  export KORA_ORDINARY_OUTPUT
  printf 'KORA_ORDINARY_OUTPUT=%s\n' "$KORA_ORDINARY_OUTPUT" >> "$GITHUB_ENV"
}

preserve_ordinary_evidence() {
  for name in first.log second.log third.log measurements.log integration.log surface.log other.log engine.log first.xml second.xml third.xml measurements.xml integration.xml surface.xml other.xml engine.xml first.exit second.exit third.exit measurements.exit integration.exit surface.exit other.exit engine.exit; do
    if test -f "$KORA_ORDINARY_OUTPUT/$name" && test ! -L "$KORA_ORDINARY_OUTPUT/$name"; then
      cp "$KORA_ORDINARY_OUTPUT/$name" "$KORA_SCAFFOLD_DIRECTORY/$name"
    fi
  done
}

run_lane() {
  admit_lane
  KORA_CHECKOUT_REVISION="$(git rev-parse HEAD)"
  test "$KORA_CHECKOUT_REVISION" = "$KORA_WORKFLOW_REVISION"
  mkdir -p "$KORA_SCAFFOLD_DIRECTORY"
  read -r -a KORA_SCAFFOLD_ARGUMENTS <<< "$KORA_PYTEST_ARGUMENTS"
  if test "$KORA_SCAFFOLD_LANE" = conformance; then
    if uv run --no-sync pytest --collect-only -q -m "not measurement" > "$KORA_SCAFFOLD_DIRECTORY/complete.txt" 2> "$KORA_SCAFFOLD_DIRECTORY/complete.stderr"; then
      printf '0\n' > "$KORA_SCAFFOLD_DIRECTORY/complete.exit"
    else
      KORA_EXIT="$?"
      printf '%s\n' "$KORA_EXIT" > "$KORA_SCAFFOLD_DIRECTORY/complete.exit"
      exit "$KORA_EXIT"
    fi
  fi
  KORA_SCAFFOLD_SELECTION=()
  KORA_TEST_LAUNCH=()
  if test "$KORA_SCAFFOLD_LANE" = local-runtime; then
    KORA_SCAFFOLD_SELECTION=(-m "not measurement")
    KORA_TEST_LAUNCH=(bash scripts/ci/run-local-runtime-tests.sh)
  fi
  KORA_EXIT=0
  if test "$KORA_SCAFFOLD_LANE" != storage && test "$KORA_SCAFFOLD_LANE" != local-runtime; then
    open_ordinary_output
    if bash scripts/ci/ordinary_cohorts.sh > "$KORA_SCAFFOLD_DIRECTORY/tests.stdout" 2> "$KORA_SCAFFOLD_DIRECTORY/tests.stderr"; then
      KORA_EXIT=0
    else
      KORA_EXIT="$?"
    fi
    preserve_ordinary_evidence
  elif "${KORA_TEST_LAUNCH[@]}" uv run --no-sync pytest -q "${KORA_SCAFFOLD_SELECTION[@]}" "${KORA_SCAFFOLD_ARGUMENTS[@]}" --junitxml="$KORA_SCAFFOLD_DIRECTORY/junit.xml" > "$KORA_SCAFFOLD_DIRECTORY/tests.stdout" 2> "$KORA_SCAFFOLD_DIRECTORY/tests.stderr"; then
    KORA_EXIT=0
  else
    KORA_EXIT="$?"
  fi
  printf '%s\n' "$KORA_EXIT" > "$KORA_SCAFFOLD_DIRECTORY/tests.exit"
  if test "$KORA_EXIT" != 0; then
    tail -c 65536 "$KORA_SCAFFOLD_DIRECTORY/tests.stdout"
    tail -c 65536 "$KORA_SCAFFOLD_DIRECTORY/tests.stderr"
    exit "$KORA_EXIT"
  fi
  uv run --no-sync python -m tests.scaffold_evidence record \
    "$KORA_SCAFFOLD_DIRECTORY" "$KORA_SCAFFOLD_RUN" "$KORA_SCAFFOLD_ATTEMPT" \
    "$KORA_CHECKOUT_REVISION" "$KORA_REQUESTED_REVISION" \
    "$KORA_SCAFFOLD_OS" "$KORA_SCAFFOLD_PYTHON" "$KORA_TEST_DATABASES" "$KORA_SCAFFOLD_LANE" \
    "${KORA_SCAFFOLD_SELECTION[@]}" "${KORA_SCAFFOLD_ARGUMENTS[@]}"
  tail -c 2048 "$KORA_SCAFFOLD_DIRECTORY/tests.stdout"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  case "$*" in
    tests) run_lane ;;
    preserve)
      test -n "${KORA_ORDINARY_OUTPUT:-}"
      preserve_ordinary_evidence
      ;;
    *) exit 1 ;;
  esac
fi
