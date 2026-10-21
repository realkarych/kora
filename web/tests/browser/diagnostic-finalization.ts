import assert from 'node:assert/strict'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ArtifactLimit } from './artifacts.ts'
import { inspectRetainedArtifacts } from './diagnostic-inspection.ts'
import { artifactBytes, directoryLimit } from './diagnostic-snapshot.ts'

export async function finalizeDiagnostic(output: string, marker: string, workloadSignal: AbortSignal, limits: ArtifactLimit, evidence: Record<string, unknown>, inspectionSignal: AbortSignal = AbortSignal.timeout(30000), firstChunk?: () => Promise<void>): Promise<void> {
  const [cleanup] = await Promise.allSettled([Promise.resolve().then(() => { limits.close() })])
  assert(cleanup)
  const inspection = await inspectRetainedArtifacts(output, marker, inspectionSignal, firstChunk)
  evidence.artifactLimitCleanup = cleanup.status
  evidence.artifactLimitFailed = limits.failed
  evidence.interrupted = workloadSignal.aborted
  evidence.inspection = inspection
  if (cleanup.status === 'rejected') evidence.cleanupFailure = 'artifact-limit-finalization'
  if (inspection.status === 'passed') evidence.excludedMarker = 'absent'
  if (workloadSignal.aborted || limits.failed || cleanup.status === 'rejected' || inspection.status !== 'passed') evidence.status = 'failed'
  const data = JSON.stringify(evidence)
  assert(Buffer.byteLength(data) <= 65536, 'Diagnostic launcher evidence exceeded file bound')
  assert(artifactBytes(output) + Buffer.byteLength(data) <= directoryLimit - 4 * 1024 * 1024, 'Diagnostic launcher evidence exceeded cumulative bound')
  writeFileSync(join(output, 'launcher.json'), data, { flag: 'wx', mode: 0o600 })
  assert.equal(cleanup.status, 'fulfilled', 'Diagnostic artifact cleanup failed')
  assert.equal(inspection.status, 'passed', 'Retained diagnostic artifact inspection failed')
  workloadSignal.throwIfAborted()
  assert.equal(evidence.status, 'diagnostic complete; performance unverified', 'Diagnostic workload failed')
}
