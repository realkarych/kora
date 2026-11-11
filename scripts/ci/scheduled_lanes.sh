set -eo pipefail

KORA_SCHEDULED_LIFETIME_MODULES=(
  tests/conformance/test_lifetime_cleanup_controls.py
  tests/conformance/test_lifetime_direct_inputs.py
  tests/conformance/test_lifetime_helper_phases.py
  tests/conformance/test_lifetime_io_helper.py
  tests/conformance/test_lifetime_journal_recovery.py
  tests/conformance/test_lifetime_measurement_cohorts.py
  tests/conformance/test_lifetime_measurement_observer.py
  tests/conformance/test_lifetime_parent_exit.py
  tests/conformance/test_lifetime_pending_helpers.py
  tests/conformance/test_lifetime_recovery_claim.py
  tests/conformance/test_lifetime_recovery_ownership.py
  tests/conformance/test_lifetime_resume.py
  tests/conformance/test_lifetime_rpc_concurrency.py
  tests/conformance/test_lifetime_services.py
  tests/conformance/test_lifetime_transport.py
)
KORA_SCHEDULED_MACOS_STORAGE_MODULES=(
  tests/plugins/storage/test_pressure_controller.py
  tests/plugins/storage/test_pressure_image_creation.py
  tests/plugins/storage/test_pressure_native_release.py
)

select_scheduled_lane() {
  case "$1:$2" in
    ubuntu-24.04:lifetime|macos-15:lifetime) KORA_SCHEDULED_SELECTION=("${KORA_SCHEDULED_LIFETIME_MODULES[@]}") ;;
    macos-15:storage) KORA_SCHEDULED_SELECTION=("${KORA_SCHEDULED_MACOS_STORAGE_MODULES[@]}") ;;
    *) exit 1 ;;
  esac
}

scheduled_ignores() {
  KORA_SCHEDULED_IGNORES=()
  case "$1" in
    ubuntu-24.04) ;;
    macos-15)
      for path in "${KORA_SCHEDULED_MACOS_STORAGE_MODULES[@]}"; do
        KORA_SCHEDULED_IGNORES+=("--ignore=$path")
      done
      ;;
    *) exit 1 ;;
  esac
  for path in "${KORA_SCHEDULED_LIFETIME_MODULES[@]}"; do
    KORA_SCHEDULED_IGNORES+=("--ignore=$path")
  done
}

run_scheduled_lane() {
  select_scheduled_lane "$KORA_SCHEDULED_OS" "$KORA_SCHEDULED_LANE"
  KORA_CHECKOUT_REVISION="$(git rev-parse HEAD)"
  test "$KORA_CHECKOUT_REVISION" = "$KORA_WORKFLOW_REVISION"
  mkdir -p "$KORA_SCHEDULED_DIRECTORY"
  if uv run --no-sync pytest -q -m "not measurement" "${KORA_SCHEDULED_SELECTION[@]}" --junitxml="$KORA_SCHEDULED_DIRECTORY/junit.xml" > "$KORA_SCHEDULED_DIRECTORY/tests.stdout" 2> "$KORA_SCHEDULED_DIRECTORY/tests.stderr"; then
    KORA_EXIT=0
  else
    KORA_EXIT="$?"
  fi
  printf '%s\n' "$KORA_EXIT" > "$KORA_SCHEDULED_DIRECTORY/tests.exit"
  if test "$KORA_EXIT" != 0; then
    tail -c 65536 "$KORA_SCHEDULED_DIRECTORY/tests.stdout"
    tail -c 65536 "$KORA_SCHEDULED_DIRECTORY/tests.stderr"
    exit "$KORA_EXIT"
  fi
  uv run --no-sync python -m tests.scaffold_evidence scheduled \
    "$KORA_SCHEDULED_DIRECTORY/junit.xml" "$KORA_SCHEDULED_OS" "$KORA_SCHEDULED_LANE"
  tail -c 2048 "$KORA_SCHEDULED_DIRECTORY/tests.stdout"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  test "$#" -eq 0
  run_scheduled_lane
fi
