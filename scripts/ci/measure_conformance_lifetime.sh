set -eo pipefail

select_lifetime_shard() {
  case "$1" in
    fixture)
      evidence=measurements/conformance-lifetime
      selection=tests/measurements/test_conformance_lifetime.py::test_conformance_fixture_resources_are_released
      ;;
    listing)
      evidence=measurements/conformance-lifetime-listing
      selection=tests/measurements/test_conformance_lifetime.py::test_installed_listing_resources_are_released
      ;;
    command)
      evidence=measurements/conformance-lifetime-command
      selection=tests/measurements/test_conformance_lifetime.py::test_installed_command_resources_are_released
      ;;
    *) exit 1 ;;
  esac
}

prepare_lifetime_evidence() {
  mkdir -p measurements
  mkdir -m 700 "$evidence"
  printf 'Actual checkout: %s\nRequested measurement revision: %s\n' "$KORA_LIFETIME_CHECKOUT_REVISION" "$KORA_MEASUREMENT_REVISION" > "$evidence/source-revision.txt"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  test "$#" -eq 1
  select_lifetime_shard "$1"
  prepare_lifetime_evidence
  KORA_MEASUREMENT_OUTPUT="$PWD/$evidence/report.json" \
    exec uv run --no-sync pytest -q -p no:logging -m measurement --junitxml="$evidence/tests.xml" "$selection"
fi
