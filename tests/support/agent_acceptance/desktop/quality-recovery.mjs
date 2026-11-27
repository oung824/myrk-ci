import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityRecovery({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  const value = request.quality_recovery
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const seen = new Set()
  const queued = []
  state.quality_recovery = { task_id: value.task_id, adapter: value.adapter, approval_decisions: [] }
  const until = async (check, label) => {
    const deadline = Date.now() + 120000
    while (!await check()) {
      for (const event of await events()) {
        const action = event.action
        if (event.event !== 'action' || action.status !== 'awaiting_approval' || seen.has(action.approval_id)) continue
        const expected = queued.find(item => item.task.id === event.task_id)
        const exact = action.kind === 'history_reuse' && expected && action.target === expected.task.scope.root
          && action.authorization_revision === expected.task.approval.revision
          && action.impact.includes(expected.task.scope.root)
        assert.ok(exact, 'Only the exact queued task history reuse may be approved')
        seen.add(action.approval_id)
        state.quality_recovery.approval_decisions.push({ action, task_id: event.task_id, approved: true })
        await command({ action: 'approve_action', path: event.path, task_id: event.task_id,
          action_id: action.id, approval_id: action.approval_id, approved: true })
      }
      const fatal = (await events()).findLast(item => item.event === 'error' && item.fatal)
      assert.ok(!fatal, fatal?.message)
      assert.ok(Date.now() < deadline, label)
      await pause(20)
    }
  }
  const select = async scope => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), scope.root)
  }
  const count = async name => (await events()).filter(item => item.event === name).length
  const saved = path => readFile(path, 'utf8').then(JSON.parse)
  if (value.parallel) {
    const { runQualityRecoveryParallel } = await import('./quality-recovery-parallel.mjs')
    await runQualityRecoveryParallel({ authority, request, command, events, execute, state })
    return
  }
  if (value.workflow === 'resume' && value.resume_kind === 'qg5-8') {
    const { runQualityRecoveryDelegated } = await import('./quality-recovery-delegated.mjs')
    return runQualityRecoveryDelegated({ authority, request, command, events, execute, state })
  }
  if (value.workflow === 'resume' && ['qg5-6', 'qg5-9'].includes(value.resume_kind)) {
    const { runQualityRecoveryResume } = await import('./quality-recovery-resume.mjs')
    await runQualityRecoveryResume({ authority, request, command, events, execute, state })
    return
  }
  if (value.workflow === 'resume') {
    if (authority.phase === 'work') {
      await select(request.scope)
      await command({ action: 'send', prompt: request.prompt, scope: request.scope })
      await until(async () => await count('finished') === 1, 'original actual recovery work stops with its retained failure')
      const started = (await events()).find(item => item.event === 'started')
      const finished = (await events()).findLast(item => item.event === 'finished')
      assert.equal(finished.turn.status, 'failed')
      await writeFile(join(root, 'original-before.json'), await readFile(started.path), { flag: 'wx' })
      await writeFile(join(root, 'quality-recovery-original.json'), JSON.stringify({ started, finished }, null, 2), { flag: 'wx' })
      state.task_id = started.task_id
    } else {
      const original = await saved(join(root, 'quality-recovery-original.json'))
      await command({ action: 'open', path: original.started.path })
      await until(async () => await count('conversation') > 0, 'original actual recovery record opens')
      const opening = await saved(join(authority.runtime, 'receiver/exchange.json'))
      assert.equal(opening.exchanges.length, 0)
      await command({ action: 'verify_recovery', path: original.started.path, task_id: original.started.task_id })
      await until(async () => await count('recovery') > 0, 'actual original effects are verified')
      const recovery = (await events()).findLast(item => item.event === 'recovery').recovery
      assert.equal(recovery.can_resume, true)
      await writeFile(join(root, 'recovery-observation.json'), JSON.stringify({ open_http_attempts: 0, can_resume: true, recovery }, null, 2))
      await command({ action: 'resume_task', path: original.started.path, task_id: original.started.task_id })
      await until(async () => await count('finished') > 0, 'same verified task resumes without replaying completed effects')
      const finished = (await events()).findLast(item => item.event === 'finished')
      assert.equal(finished.turn.status, 'completed', finished.turn.error)
      state.task_id = original.started.task_id
    }
    state.passed = true
    return
  }
  await select(request.scope)
  await command({ action: 'send', prompt: request.prompt, scope: request.scope })
  await until(async () => await count('started') > 0, 'current frozen task starts')
  const started = (await events()).find(item => item.event === 'started')
  const arrival = value.supplement_arrival
  const gateIndex = arrival?.gate_index ?? 0
  const gatePath = join(root, `quality-gate-${gateIndex}-arrived.json`)
  await until(() => existsSync(gatePath), `actual terminal response ${gateIndex} arrives`)
  let gate
  if (arrival) {
    await until(async () => (await events()).some(item => item.event === 'action' && item.path === started.path
      && item.task_id === started.task_id && item.action.kind === 'read' && item.action.status === 'completed'
      && item.action.prepared_parameters?.path === arrival.target_path), 'the same task completes its original read before the supplement')
    gate = await saved(gatePath)
    assert.equal(gate.index, gateIndex)
    assert.equal(gate.origin, 'actual_terminal_http_response')
    assert.ok(gate.sdk_input_id)
  }
  if (value.supplement) {
    await command({ action: 'send', path: started.path, prompt: value.supplement })
    await until(async () => (await events()).some(item => item.event === 'supplement' && item.state === 'received'
      && item.path === started.path && item.task_id === started.task_id), 'actual later user supplement is received')
  }
  for (const [index, item] of (value.queue ?? []).entries()) {
    await select(item.scope)
    await command({ action: 'send', path: started.path, prompt: item.prompt, scope: item.scope, new_task: true })
    await until(async () => (await events()).some(event => event.event === 'queue' && event.queue.length === index + 1), 'separate actual task enters the durable queue')
  }
  if (value.queue?.length) queued.push(...(await events()).findLast(item => item.event === 'queue').queue)
  await writeFile(join(root, `quality-gate-${gateIndex}-release.txt`), 'Release after actual user events and durable queue\n', { flag: 'wx' })
  if (value.workflow === 'partial_failure') {
    await until(async () => (await events()).some(item => item.event === 'finished' && item.task_id === started.task_id), 'original service failure is saved')
    const failed = (await events()).findLast(item => item.event === 'finished' && item.task_id === started.task_id)
    assert.equal(failed.turn.status, 'failed')
    const nextId = queued[0].task.id
    await until(async () => (await events()).some(item => item.event === 'finished' && item.task_id === nextId), 'the independently queued task finishes automatically')
    await writeFile(join(root, 'quality-failure-original.json'), await readFile(started.path), { flag: 'wx' })
    const observed = await events()
    const verifiedIndex = observed.findIndex(item => item.event === 'recovery' && item.recovery.task_id === started.task_id)
    const nextStart = observed.findIndex(item => item.event === 'started' && item.task_id === nextId)
    assert.ok(verifiedIndex >= 0 && verifiedIndex < nextStart && observed[verifiedIndex].saved)
    await writeFile(join(root, 'quality-failure-observation.json'), JSON.stringify({ queued: queued[0], original_finished: failed,
      verified_before_next: true, recovery: observed[verifiedIndex].recovery, stop_events: observed.filter(item => item.event === 'stopping') }, null, 2))
    state.task_id = started.task_id
    state.passed = true
    return
  }
  await until(async () => await count('finished') === 1 + (value.queue?.length ?? 0), 'original and independently queued tasks finish')
  const finished = (await events()).filter(item => item.event === 'finished')
  assert.ok(finished.every(item => item.turn.status === 'completed'), JSON.stringify(finished.map(item => item.turn.error)))
  const record = await saved(started.path)
  if (value.supplement) {
    const supplements = (await events()).filter(item => item.event === 'supplement'
      && item.path === started.path && item.task_id === started.task_id)
    const applied = supplements.filter(item => item.state === 'applied')
    const received = supplements.find(item => item.state === 'received')
    assert.equal(applied.length, 1)
    assert.equal(applied[0].supplement.id, received.supplement.id)
    const savedSupplement = record.turns[0].task.supplements[0]
    if (arrival) {
      assert.equal(received.saved, true)
      assert.equal(applied[0].saved, true)
      assert.equal(savedSupplement.id, received.supplement.id)
      assert.equal(savedSupplement.applied_request, applied[0].supplement.applied_request)
      await writeFile(join(root, 'quality-supplement-observation.json'), JSON.stringify({
        gate, received, applied: applied[0],
      }, null, 2), { flag: 'wx' })
    } else {
      assert.equal(savedSupplement.applied_request, 2)
    }
  }
  state.task_id = started.task_id
  state.passed = true
}
