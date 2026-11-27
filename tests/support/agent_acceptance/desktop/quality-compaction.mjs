import assert from 'node:assert/strict'
import { runQualityCompactionCorrection } from './quality-compaction-correction.mjs'
import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityCompaction({ authority, request, command, events, execute, state }) {
  if (request.quality_compaction?.correction_flow) return runQualityCompactionCorrection({ authority, request, command, events, execute, state })
  const root = authority.scenario_root
  const task = request.quality_compaction
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const observation = { task_id: task.task_id, adoption_sent_after_summary: false,
    correction_sent_after_summary: false, continuation_accepted_after_three_summaries: false }
  state.quality_compaction = observation
  const until = async (check, label) => {
    const deadline = Date.now() + 360000
    while (!await check()) {
      const fatal = (await events()).findLast(item => item.event === 'error' && item.fatal)
      assert.ok(!fatal, fatal?.message)
      assert.ok(Date.now() < deadline, label)
      await pause(20)
    }
  }
  const finishedCount = async () => (await events()).filter(item => item.event === 'finished').length
  const summaries = async () => (await events()).filter(item => item.event === 'compactions').at(-1)?.compactions.filter(item => item.status === 'completed') ?? []
  const savedRecord = path => readFile(path, 'utf8').then(JSON.parse)
  if (authority.phase === 'work') {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
    let proposalPath
    if (task.prelude) {
      const before = await finishedCount()
      await command({ action: 'send', prompt: task.prelude, scope: request.scope,
        ...(request.approval ? {} : { approval_mode: request.approval_mode }) })
      await until(async () => await finishedCount() > before, 'actual unadopted assistant proposal')
      if (task.slot === '4') {
        const all = await events()
        proposalPath = all.findLast(item => item.event === 'started').path
        const saved = (await savedRecord(proposalPath)).turns.at(-1)
        assert.equal(saved.status, 'completed')
        assert.ok(saved.answer.trim() && saved.actions.length === 0, 'The proposal-only turn must deliver an answer without tool actions')
      }
    }
    const before = await finishedCount()
    await command({ action: 'send', prompt: request.prompt, scope: request.scope,
      ...(proposalPath ? { path: proposalPath, new_task: true } : {}),
      ...(request.approval ? {} : { approval_mode: request.approval_mode }) })
    let adopted = false
    let corrected = false
    let continued = false
    await until(async () => {
      const completed = await summaries()
      const started = (await events()).findLast(item => item.event === 'started')
      if (started && await finishedCount() === before) {
        if (task.adoption && !adopted && completed.length >= 1) {
          adopted = true
          await command({ action: 'send', path: started.path, prompt: task.adoption })
          observation.adoption_sent_after_summary = true
        }
        if (task.correction && !corrected && completed.length >= 1) {
          corrected = true
          await command({ action: 'send', path: started.path, prompt: task.correction })
          observation.correction_sent_after_summary = true
        }
        if (task.continuation && task.slot === '6' && !continued && completed.length >= 3) {
          continued = true
          observation.before_continuation_summary_ids = completed.map(item => item.id)
          await command({ action: 'send', path: started.path, prompt: task.continuation })
        }
      }
      return await finishedCount() > before
    }, 'task-specific long-context Main work')
    const all = await events()
    const finished = all.findLast(item => item.event === 'finished')
    const started = all.findLast(item => item.event === 'started')
    const record = await savedRecord(started.path)
    const saved = record.turns.at(-1)
    state.task_id = saved.task.id
    if (task.slot === '6') {
      const supplement = saved.task.supplements.find(item => item.text === task.continuation)
      observation.continuation_accepted_after_three_summaries = continued && supplement?.applied_request != null
      assert.equal(observation.continuation_accepted_after_three_summaries, true)
    }
    if (task.slot === '3') {
      const adoption = saved.task.supplements.find(item => item.text === task.adoption && item.applied_request != null)
      assert.ok(adoption)
      const first = saved.compactions.find(item => item.status === 'completed')
      assert.ok(first?.request_numbers.length)
      assert.ok(Math.max(...first.request_numbers) < adoption.applied_request)
      const reads = saved.actions.filter(item => item.kind === 'read' && item.status === 'completed' && task.comparison_paths.some(path => item.prepared_parameters.path === join(request.scope.root, path)))
      assert.equal(new Set(reads.map(item => item.prepared_parameters.path)).size, task.comparison_paths.length)
      assert.ok(reads.every(item => item.task_id === saved.task.id && item.prepared_request != null && item.prepared_request >= adoption.applied_request))
      observation.comparison_authorization_at_action = reads.map(item => ({ action_id: item.id, task_id: item.task_id, prepared_request: item.prepared_request, applied_request: adoption.applied_request }))
    }
    if (task.slot === '9') assert.ok(saved.task.supplements.some(item => item.text === task.correction && item.applied_request != null))
    if (task.slot === '10') {
      assert.equal(finished.turn.status, 'failed')
      assert.equal(saved.task.work_step.capacity_recovery_used, true)
      assert.ok(saved.compactions.length >= 3)
      await writeFile(join(root, 'quality-compaction-original.json'), JSON.stringify({ started, finished, saved }, null, 2))
      await writeFile(join(root, 'quality-compaction-before-reopen.json'), await readFile(started.path))
    } else assert.equal(finished.turn.status, 'completed', finished.turn.error)
  } else {
    const original = JSON.parse(await readFile(join(root, 'quality-compaction-original.json'), 'utf8'))
    await command({ action: 'open', path: original.started.path })
    await until(async () => (await events()).some(item => item.event === 'conversation' && item.path === original.started.path), 'open original long-context task')
    assert.equal((await events()).some(item => item.event === 'started'), false)
    const opening = JSON.parse(await readFile(join(authority.runtime, 'receiver/exchange.json'), 'utf8'))
    assert.equal(opening.exchanges.length, 0)
    const before = await savedRecord(original.started.path)
    assert.equal(before.turns.at(-1).task.work_step.id, original.saved.task.work_step.id)
    assert.equal(before.turns.at(-1).task.work_step.capacity_recovery_used, true)
    observation.open_http_attempts = opening.exchanges.length
    observation.opened_pending_step = before.turns.at(-1).task.work_step
    await command({ action: 'verify_recovery', path: original.started.path, task_id: original.started.task_id })
    await until(async () => (await events()).some(item => item.event === 'recovery'), 'verify original source effects')
    assert.equal((await events()).findLast(item => item.event === 'recovery').recovery.can_resume, true)
    const count = await finishedCount()
    await command({ action: 'resume_task', path: original.started.path, task_id: original.started.task_id })
    await until(async () => await finishedCount() > count, 'resume original long-context task')
    const finished = (await events()).findLast(item => item.event === 'finished')
    const record = await savedRecord(original.started.path)
    const saved = record.turns.at(-1)
    await writeFile(join(root, 'quality-compaction-' + authority.phase + '.json'), JSON.stringify({ finished, saved, observation }, null, 2))
    state.task_id = saved.task.id
    assert.equal(saved.task.id, original.saved.task.id)
    assert.deepEqual(saved.task.scope, original.saved.task.scope)
    assert.deepEqual(saved.task.approval, original.saved.task.approval)
    const work = saved.request_attempts.find(item => item.purpose === 'work')
    if (!work) {
      state.error = 'The saved reopened task did not issue a work request.'
      state.passed = false
      return
    }
    assert.ok(saved.task.requests > before.turns.at(-1).task.requests)
    assert.equal(work.step_id, original.saved.task.work_step.id)
    if (authority.phase === 'capacity-reopen') {
      assert.equal(finished.turn.status, 'failed')
      assert.deepEqual(saved.compactions, [])
      assert.deepEqual(saved.actions, [])
      assert.equal(saved.task.work_step.capacity_recovery_used, true)
    } else {
      assert.equal(finished.turn.status, 'completed', finished.turn.error)
      assert.deepEqual(saved.actions.map(item => [item.kind, item.status, item.prepared_parameters.path]), [['read', 'completed', join(request.scope.root, 'continuation.txt')]])
    }
  }
  state.passed = true
}
