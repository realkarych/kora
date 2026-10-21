import assert from 'node:assert/strict'

export const controls = ['cycle-1', 'cycle-8', 'cycle-32', 'intertrial'] as const
export type Control = typeof controls[number]
export type RunnerMode = 'verification' | 'calibration' | 'smoke' | 'diagnostic' | Control

export function parseRunnerMode(arguments_: string[]): RunnerMode {
  assert(arguments_.length <= 1, 'invalid-browser-mode')
  const argument = arguments_[0]
  if (argument === undefined) return 'verification'
  if (argument === '--calibrate') return 'calibration'
  if (argument === '--smoke') return 'smoke'
  if (argument === '--diagnostic') return 'diagnostic'
  for (const control of controls) if (argument === `--control=${control}`) return control
  throw new Error('invalid-browser-mode')
}

export function isControl(mode: RunnerMode): mode is Control {
  return controls.some(control => control === mode)
}

export class Retention {
  readonly mode: Control | 'none'
  readonly payload: number
  readonly maximum: number
  #arrays: (number[] | null)[]
  #allocations = 0
  #released = false

  constructor(mode: Control | 'none') {
    this.mode = mode
    this.payload = mode === 'cycle-1' ? 1024 : mode === 'cycle-8' ? 8192 : mode === 'cycle-32' ? 32768 : mode === 'intertrial' ? 245760 : 0
    this.maximum = mode === 'none' ? 0 : mode === 'intertrial' ? 6 : 180
    this.#arrays = Array<number[] | null>(this.maximum).fill(null)
  }

  allocate(boundary: 'cycle' | 'finalization'): void {
    assert(!this.#released, 'retention-owner-released')
    if (this.mode === 'none' || (boundary === 'finalization') !== (this.mode === 'intertrial')) return
    assert(this.#allocations < this.maximum, 'retention-owner-limit')
    this.#arrays[this.#allocations] = Array<number>(this.payload / 8).fill(0.5)
    this.#allocations++
  }

  evidence(): { allocations: number; arrays: number; nominalBytes: number; released: boolean } {
    return { allocations: this.#allocations, arrays: this.#arrays.filter(value => value !== null).length, nominalBytes: this.#arrays.reduce((total, value) => total + (value?.length ?? 0) * 8, 0), released: this.#released }
  }

  release(): void {
    this.#arrays.fill(null)
    this.#released = true
  }
}

const ancillary = ['runnerheapUsedSpan', 'runnerheapUsedLateSpan', 'runnerheapUsedMedianGrowth', 'runnerheapUsedPeak', 'runnerheapUsedReleasedGrowth']

export function classifyControl(mode: Control, ordinary: Record<string, boolean>, invocation: Record<string, boolean>, complete: boolean, recovered: boolean): { detected: boolean; unrelatedPassed: boolean; valid: boolean; outcome: string; recovery: string } {
  const detected = ['invocation-full', 'invocation-late', 'invocation-slope'].some(key => invocation[key] === false)
  const unrelatedPassed = Object.entries(ordinary).every(([key, passed]) => passed || ancillary.some(metric => key.startsWith(`${metric}-`)))
  const admitted = complete && Object.keys(ordinary).length === 336 && Object.keys(invocation).sort().join(',') === 'invocation-full,invocation-late,invocation-slope'
  const valid = admitted && recovered && unrelatedPassed && (mode === 'cycle-1' || detected)
  const outcome = !admitted ? 'invalid-evidence' : !recovered ? 'recovery-failed' : !unrelatedPassed ? 'unrelated-failure' : detected ? 'detected' : mode === 'cycle-1' ? 'lower-probe-missed' : 'required-control-missed'
  return { detected, unrelatedPassed, valid, outcome, recovery: !admitted ? 'unverified' : !recovered ? 'failed' : mode === 'intertrial' ? 'passed' : 'not-applicable' }
}
