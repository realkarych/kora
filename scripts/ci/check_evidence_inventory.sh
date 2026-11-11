set -eo pipefail

check_cel_inventory() {
  test -d measurements/cel-lifetime
  test ! -L measurements/cel-lifetime
  test -d measurements/cel-cost
  test ! -L measurements/cel-cost
  test "$(find measurements/cel-lifetime -mindepth 1 -print | LC_ALL=C sort)" = "$(printf '%s\n' measurements/cel-lifetime/cel-checkout-revision.txt measurements/cel-lifetime/cel-evaluator-lifetime.json)"
  test "$(find measurements/cel-cost -mindepth 1 -print | LC_ALL=C sort)" = "$(printf '%s\n' measurements/cel-cost/cel-checkout-revision.txt measurements/cel-cost/cel-cost-smoke.json)"
  for path in measurements/cel-lifetime/cel-evaluator-lifetime.json measurements/cel-cost/cel-cost-smoke.json; do
    test -f "$path"
    test ! -L "$path"
    test -s "$path"
  done
  for path in measurements/cel-lifetime/cel-checkout-revision.txt measurements/cel-cost/cel-checkout-revision.txt; do
    test -f "$path"
    test ! -L "$path"
    test "$(cat "$path")" = "$KORA_MEASUREMENT_REVISION"
  done
}

check_lifetime_inventory() {
  KORA_LIFETIME_EVIDENCE="measurements/conformance-lifetime-$KORA_MEASUREMENT_REVISION"
  test "$(find measurements -mindepth 1 -maxdepth 1 -print | LC_ALL=C sort)" = "$(printf '%s\n' "$KORA_LIFETIME_EVIDENCE-command" "$KORA_LIFETIME_EVIDENCE-fixture" "$KORA_LIFETIME_EVIDENCE-listing")"
  for shard in fixture listing command; do
    test ! -L "$KORA_LIFETIME_EVIDENCE-$shard"
  done
  for path in fixture/report.json fixture/publication.json fixture/tests.xml fixture/source-revision.txt listing/listing.json listing/tests.xml listing/source-revision.txt command/command.json command/command-artifacts/publication.json command/tests.xml command/source-revision.txt; do
    test -f "$KORA_LIFETIME_EVIDENCE-$path"
    test ! -L "$KORA_LIFETIME_EVIDENCE-$path"
    test -s "$KORA_LIFETIME_EVIDENCE-$path"
  done
  for shard in fixture listing command; do
    test "$(cat "$KORA_LIFETIME_EVIDENCE-$shard/source-revision.txt")" = "$(printf 'Actual checkout: %s\nRequested measurement revision: %s' "$KORA_MEASUREMENT_REVISION" "$KORA_MEASUREMENT_REVISION")"
  done
}

case "$*" in
  cel) check_cel_inventory ;;
  lifetime) check_lifetime_inventory ;;
  *) exit 1 ;;
esac
