import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { childEnvironment } from './ownership.ts'
import { verifyRecovery } from './retention-fixture.ts'
import { validateWorkflowTransition, validationWorkflow } from './invocation-workflow.ts'
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, truncateSync, unlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fitHeap, fitHeapLimits, heapCeiling, heapGates, heapGrowth, parseHeapLimits, slopeCeiling } from './invocation-heap.ts'
import { invocationGrowth, parseInvocationPoint, persistInvocation, readInvocation } from './invocation.ts'
import { classifyControl, controls, parseRunnerMode, Retention } from './retention.ts'
import { measurementMetrics, measurementProtocol } from './protocol.ts'
import { compare } from './protocol.ts'
import { readEvidence } from './trial-evidence.ts'

export function heapChecks(): void {
  const slopes = [-8, -4, 0, 4, 8].map(value => value * 1024)
  const all = (value: number): number[] => Array<number>(5).fill(value)
  const limits = { terminal: 61440, full: 98304, late: 61440, slope: 28672 }

  const signed = fitHeap(slopes)
  const plateau = heapGrowth([200, 100, 80, 100, 90, 95])
  const middle = heapGrowth([0, 0, 0, 100, 110, 90])
  const leak = heapGrowth([1, 2, 3, 4, 5, 6].map(value => value * 245760))

  assert.equal(signed, 20480)
  assert.equal(fitHeap(slopes.map(value => Math.max(0, value))), 8192)
  assert.equal(fitHeap(all(heapCeiling), heapCeiling), heapCeiling)
  assert.throws(() => fitHeap(all(heapCeiling + 1), heapCeiling), /heap-budget-exceeds-ceiling/u)
  assert.equal(fitHeap(all(28672), slopeCeiling), 28672)
  assert.throws(() => fitHeap(all(30720), slopeCeiling), /heap-budget-exceeds-ceiling/u)
  assert.deepEqual(plateau, { full: 20, late: 5, slope: -2.5 })
  assert.deepEqual(middle, { full: 110, late: 10, slope: -5 })
  assert.deepEqual(leak, { full: 1228800, late: 491520, slope: 245760 })
  assert(Object.values(heapGates(leak, limits)).every(value => !value))
  assert.deepEqual(fitHeapLimits(Array.from({ length: 5 }, () => Array<number>(6).fill(-100)), Array.from({ length: 5 }, () => ({ full: 0, late: 0, slope: -100 }))), { terminal: 0, full: 0, late: 0, slope: 0 })
  assert.deepEqual(parseHeapLimits(limits), limits)
  for (const invalid of [{ ...limits, terminal: 61441 }, { ...limits, slope: 32768 }, { ...limits, full: Infinity }, { ...limits, extra: 0 }]) assert.throws(() => parseHeapLimits(invalid))

  const workflow = 'permissions: read\nactions/checkout@pinned\nname: Console browser calibration (${{ matrix.slot }})\n        slot: [1, 2, 3, 4, 5]\nrun: pnpm --dir web run test:browser --calibrate\nname: console-browser-calibration-${{ matrix.slot }}'

  const validation = validationWorkflow(workflow)

  validateWorkflowTransition(workflow, validation)
  for (const changed of [validation.replace('permissions: read', 'permissions: write'), validation.replace('checkout@pinned', 'checkout@other'), validation + '\nrun: bypass-checks']) assert.throws(() => { validateWorkflowTransition(workflow, changed) })

  const recovery = Array.from({ length: 8 }, (_, index) => ({ memory: { rss: 1000000, heapTotal: 1000000, heapUsed: index > 0 && index < 7 ? 345760 : 100000, external: 0, arrayBuffers: 0 }, retention: { allocations: index === 0 ? 0 : 1, arrays: index > 0 && index < 7 ? 1 : 0, nominalBytes: index > 0 && index < 7 ? 245760 : 0, released: index === 7 } }))
  const failedRecovery = recovery.map((point, index) => index === 7 ? { ...point, memory: { ...point.memory, heapUsed: 345760 } } : point)

  const recoveryResult = verifyRecovery(recovery, limits)
  const failedRecoveryResult = verifyRecovery(failedRecovery, limits)

  assert(recoveryResult.passed)
  assert(!failedRecoveryResult.passed && !failedRecoveryResult.gates.release && !failedRecoveryResult.gates.recovery)
  assert.throws(() => verifyRecovery(recovery.slice(1), limits))

  const summary = Object.fromEntries(measurementMetrics.map(key => [key, 0]))
  const ordinary = compare([summary, summary, summary], [summary, summary, summary], { protocol: measurementProtocol, reference: { platform: 'linux', arch: 'x64', node: 'fixture', browser: 'fixture' }, heap: limits, absolute: summary, pairedMargin: summary }).gates
  const pass = { 'invocation-full': true, 'invocation-late': true, 'invocation-slope': true }
  const detected = { ...pass, 'invocation-slope': false }

  const lower = classifyControl('cycle-1', ordinary, pass, true, true)
  const required = classifyControl('cycle-8', ordinary, detected, true, true)

  assert(lower.valid && !lower.detected)
  assert(required.valid && required.detected)
  assert.equal(classifyControl('intertrial', ordinary, detected, true, false).outcome, 'recovery-failed')
  assert.equal(classifyControl('intertrial', ordinary, detected, false, false).outcome, 'invalid-evidence')
  assert.equal(classifyControl('intertrial', { ...ordinary, 'loadMedian-paired': false }, detected, true, true).outcome, 'unrelated-failure')
  assert(!classifyControl('cycle-8', { ...ordinary, 'runnerheapUsedPeak-absolute-head-0': false }, pass, true, true).valid)
  assert(!classifyControl('cycle-8', { ...ordinary, 'runnerexternalReleasedGrowth-paired': false }, detected, true, true).valid)
  assert(!classifyControl('cycle-8', ordinary, detected, false, true).valid)
  assert(!classifyControl('cycle-8', ordinary, detected, true, false).valid)
  assert(!classifyControl('cycle-8', {}, detected, true, true).valid)
  for (const mode of controls) assert.equal(parseRunnerMode([`--control=${mode}`]), mode)
  for (const arguments_ of [['--control=marker-secret'], ['--calibrate', '--control=cycle-8'], ['--control=cycle-8', '--control=cycle-8'], ['--smoke', '--smoke']]) assert.throws(() => parseRunnerMode(arguments_), { message: 'invalid-browser-mode' })
}

export function invocationChecks(): void {
  const root = mkdtempSync(join(tmpdir(), 'kora-invocation-checks-'))
  const order: ['base' | 'head', number][] = [['base', 0], ['head', 0], ['head', 1], ['base', 1], ['base', 2], ['head', 2]]
  const memory = { rss: 100, heapTotal: 100, heapUsed: 100, external: 0, arrayBuffers: 0 }
  const owner = new Retention('none')
  try {
    persistInvocation(root, 0, 'start', 0, memory, owner.evidence())
    for (const [index, [arm, repeat]] of order.entries()) persistInvocation(root, index + 1, arm, repeat, memory, owner.evidence())

    const points = readInvocation(root)
    const growth = invocationGrowth(points, order)

    assert.deepEqual(growth, { full: 0, late: 0, slope: 0 })
    assert.throws(() => invocationGrowth(points.slice(1), order))
    assert.throws(() => invocationGrowth([...points].reverse(), order))
    assert.throws(() => invocationGrowth([...points.slice(0, 6), points[0]].filter(value => value !== undefined), order))
    assert.throws(() => { persistInvocation(root, 0, 'start', 0, memory, owner.evidence()) })
    const first = points[0]
    assert(first)
    for (const value of [{ ...first, extra: 1 }, { ...first, memory: { ...memory, heapUsed: NaN } }, { ...first, memory: { ...memory, heapUsed: -1 } }, { ...first, sequence: 7 }, { ...first, protocol: 'collector-released-v2' }]) assert.throws(() => parseInvocationPoint(value))
    const last = join(root, 'invocation-6.json')
    unlinkSync(last)
    assert.throws(() => readInvocation(root))
    symlinkSync(join(root, 'invocation-0.json'), last)
    assert.throws(() => readInvocation(root))
    unlinkSync(last)
    mkdirSync(last)
    assert.throws(() => readInvocation(root))
    rmSync(last, { recursive: true })
    writeFileSync(last, '{"marker-secret":')
    assert.throws(() => readInvocation(root))
    const argumentsFailure = spawnSync(process.execPath, [fileURLToPath(new URL('./run.ts', import.meta.url)), '--control=argument-marker-secret'], { env: childEnvironment(), encoding: 'utf8', timeout: 10000, maxBuffer: 65536 })
    assert.equal(argumentsFailure.error, undefined)
    assert.equal(argumentsFailure.status, 1)
    assert(!`${argumentsFailure.stdout}${argumentsFailure.stderr}`.includes('argument-marker-secret'))
    const manifest = join(root, 'manifest.json')
    const destination = join(root, 'aggregate.json')
    const jobs = Array.from({ length: 5 }, (_, index) => ({ slot: index + 1, kind: 'training', conclusion: 'success', name: 'assertion-marker-secret' }))
    for (const [content, marker, failure] of [['{"json-marker-secret":', 'json-marker-secret', 'invalid-json'], [JSON.stringify({ stage: 'training', attempt: '1', jobs }), 'assertion-marker-secret', 'invalid-evidence']]) {
      assert(content && marker && failure)
      writeFileSync(manifest, content)
      const result = spawnSync(process.execPath, [fileURLToPath(new URL('./calibration.ts', import.meta.url)), manifest, destination], { env: childEnvironment(), encoding: 'utf8', timeout: 10000, maxBuffer: 65536 })
      assert.equal(result.error, undefined)
      assert.equal(result.status, 1)
      assert.equal(result.stdout, '')
      assert(!result.stderr.includes(marker))
      const payload: unknown = JSON.parse(result.stderr)
      assert(payload !== null && typeof payload === 'object' && 'failure' in payload)
      assert.equal(payload.failure, failure)
    }
    truncateSync(last, 4097)
    assert.throws(() => readEvidence(last, 4096))
  } finally {
    owner.release()
    rmSync(root, { recursive: true, force: true })
  }
  for (const mode of controls) {
    const retained = new Retention(mode)
    const boundary = mode === 'intertrial' ? 'finalization' : 'cycle'
    try {
      for (let index = 0; index < retained.maximum; index++) retained.allocate(boundary)

      const actual = retained.evidence()

      assert.equal(actual.allocations, retained.maximum)
      assert.equal(actual.arrays, retained.maximum)
      assert.equal(actual.nominalBytes, retained.maximum * retained.payload)
      assert.throws(() => { retained.allocate(boundary) }, /retention-owner-limit/u)
    } finally { retained.release() }
    retained.release()
    assert.deepEqual(retained.evidence(), { allocations: retained.maximum, arrays: 0, nominalBytes: 0, released: true })
    assert.throws(() => { retained.allocate(boundary) }, /retention-owner-released/u)
  }
}
