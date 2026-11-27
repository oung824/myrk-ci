import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function readSavedSummaryObservation(root, primary, summaryId) {
  assert.ok(existsSync(join(root, 'review-only', 'quality-correction-summary.ready')), 'Saved summary observation is not published')
  const observation = JSON.parse(await readFile(join(root, 'review-only', 'quality-correction-summary-observation.json'), 'utf8'))
  const source = await readFile(join(root, 'review-only', 'quality-correction-summary-source.original.json'))
  const raw = await readFile(join(root, 'quality-correction-old-summary.original.json'))
  const digest = body => createHash('sha256').update(body).digest('hex')
  assert.equal(observation.origin, 'saved_record_at_terminal_http_before_response_consumption')
  assert.equal(observation.phase, 'work_after_first_completed_normal_summary')
  assert.equal(observation.source_record, primary.path)
  assert.equal(observation.source_record_bytes, source.length)
  assert.equal(observation.source_record_sha256, digest(source))
  assert.equal(observation.summary_sha256, digest(raw))
  assert.equal(observation.summary_id, summaryId)
  assert.equal(observation.task_id, primary.task_id)
  assert.ok(typeof observation.sdk_input_id === 'string' && observation.sdk_input_id.length > 0)
  const record = JSON.parse(source)
  const running = record.turns.filter(item => item.status === 'running')
  assert.equal(running.length, 1)
  const turn = record.turns.at(-1)
  assert.equal(running[0], turn)
  assert.equal(turn.task.id, primary.task_id)
  const first = turn.compactions.find(item => item.status === 'completed' && item.trigger !== 'window_recovery')
  assert.equal(first?.id, summaryId)
  assert.equal(first.task_id, primary.task_id)
  const summary = JSON.parse(raw)
  assert.deepEqual(summary, first)
  const identity = observation.request_identity
  const attempts = turn.request_attempts.filter(item => item.number === identity.number && item.actor === identity.actor
    && item.purpose === identity.purpose && item.delegation_id === identity.delegation_id && item.step_id === observation.request_step_id)
  assert.equal(attempts.length, 1)
  assert.equal(attempts[0].status, 'sent')
  assert.equal(identity.purpose, 'work')
  assert.ok(first.request_numbers.length > 0 && Math.max(...first.request_numbers) < identity.number)
  return { summary, raw, observation }
}

export async function runQualityCompactionCorrection({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  const value = request.quality_compaction
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
  assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
  const until = async (predicate, label) => {
    const deadline = Date.now() + 900000
    while (true) {
      const observed = await events()
      const failed = observed.findLast(item => item.event === 'finished' && item.turn.status === 'failed')
      assert.ok(!failed, failed?.turn.error)
      const fatal = observed.findLast(item => item.event === 'error' && item.fatal)
      assert.ok(!fatal, fatal?.message)
      if (await predicate()) return
      assert.ok(Date.now() < deadline, label)
      await pause(20)
    }
  }
  const count = async name => (await events()).filter(item => item.event === name).length
  const readRecord = async path => JSON.parse(await readFile(path, 'utf8'))
  await command({ action: 'send', prompt: request.prompt, scope: request.scope })
  await until(async () => await count('finished') === 1, 'actual attendance task completes')
  const attendance = (await events()).find(item => item.event === 'started')
  assert.equal((await events()).findLast(item => item.event === 'finished').turn.status, 'completed')
  await command({ action: 'send', path: attendance.path, prompt: value.roster, scope: request.scope, new_task: true })
  await until(() => existsSync(join(root, 'quality-correction-roster-inflight.json')), 'actual roster terminal request in flight')
  const roster = (await events()).findLast(item => item.event === 'started')
  assert.notEqual(roster.task_id, attendance.task_id)
  await command({ action: 'stop', path: roster.path })
  await until(async () => await count('finished') === 2, 'actual roster task stop is saved')
  const stopped = (await events()).findLast(item => item.event === 'finished')
  assert.equal(stopped.turn.status, 'cancelled')
  const prior = (await readRecord(attendance.path)).turns
  assert.equal(prior.length, 2)
  await writeFile(join(root, 'quality-correction-prior-tasks.original.json'), JSON.stringify(prior, null, 2), { flag: 'wx' })
  await command({ action: 'send', path: attendance.path, prompt: value.primary, scope: request.scope, new_task: true })
  let captured = false
  let originalHash
  const originalFile = join(root, 'quality-correction-old-summary.original.json')
  await until(async () => {
    const summaries = (await events()).findLast(item => item.event === 'compactions')?.compactions.filter(item => item.status === 'completed') ?? []
    if (!captured && summaries.length && existsSync(join(root, 'review-only', 'quality-correction-summary.ready'))) {
      const primary = (await events()).findLast(item => item.event === 'started')
      const { raw } = await readSavedSummaryObservation(root, primary, summaries[0].id)
      originalHash = createHash('sha256').update(raw).digest('hex')
      captured = true
      await command({ action: 'send', path: attendance.path, prompt: value.correction })
    }
    const failed = (await events()).findLast(item => item.event === 'finished' && item.turn.status === 'failed')
    assert.ok(!failed, failed?.turn.error)
    return await count('finished') === 3
  }, 'actual primary sources and later correction complete')
  assert.ok(captured, 'At least one actual normal summary must precede the correction')
  const final = await readRecord(attendance.path)
  assert.deepEqual(final.turns.slice(0, 2), prior)
  assert.equal(final.turns.at(-1).status, 'completed')
  assert.equal(createHash('sha256').update(await readFile(originalFile)).digest('hex'), originalHash)
  assert.ok(final.turns.at(-1).task.supplements.some(item => item.text === value.correction && item.applied_request != null))
  state.quality_compaction = { task_id: value.task_id, correction_sent_after_summary: true,
    attendance_task_id: attendance.task_id, cancelled_task_id: roster.task_id, old_summary_sha256: originalHash,
    prior_task_ids: prior.map(item => item.task.id), original_path: attendance.path }
  state.task_id = final.turns.at(-1).task.id
  state.passed = true
}
