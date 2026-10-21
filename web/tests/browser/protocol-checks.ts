import assert from 'node:assert/strict'
import { calibrate, compare } from './protocol.ts'
import type { Budgets } from './protocol.ts'

export function protocolChecks(template: Budgets): void {
  const metric = 'runnerheapUsedReleasedGrowth'
  const summary = (growth: number): Record<string, number> => ({ ...Object.fromEntries(Object.keys(template.absolute).map(key => [key, 0])), [metric]: growth })
  const releases = [-7000000, -200000, -280000].map(summary)
  const quiescent = [0, -100000, -250000].map(summary)

  const releaseBudgets = calibrate(releases, template.reference)
  const released = compare(releases, quiescent, releaseBudgets)
  const retained = compare(releases, [1, 0, 0].map(summary), releaseBudgets)

  assert.equal(releaseBudgets.absolute[metric], 0)
  assert.equal(releaseBudgets.pairedMargin[metric], 0)
  assert(Object.values(released.gates).every(Boolean))
  assert.equal(retained.gates[`${metric}-absolute-head-0`], false)
  assert.equal(retained.gates[`${metric}-paired`], false)

  const base = [10, 12, 14].map(summary)
  const medianRegression = [16, 22, 24].map(summary)
  const individualRegression = [11, 13, 31].map(summary)

  const variableBudgets = calibrate(base, template.reference)
  const medianResult = compare(base, medianRegression, variableBudgets)
  const individualResult = compare(base, individualRegression, variableBudgets)

  assert.equal(variableBudgets.absolute[metric], 22)
  assert.equal(variableBudgets.pairedMargin[metric], 8)
  assert.equal(medianResult.gates[`${metric}-paired`], false)
  assert.equal(individualResult.gates[`${metric}-paired`], false)
}
