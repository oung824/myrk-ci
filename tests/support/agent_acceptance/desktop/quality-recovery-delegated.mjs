import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityRecoveryDelegated({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  assert.equal(request.quality_recovery.resume_kind, 'qg5-8')
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const saved = path => readFile(path, 'utf8').then(JSON.parse)
  const digest = bytes => createHash('sha256').update(bytes).digest('hex')
  const count = async name => (await events()).filter(event => event.event === name).length
  const httpCount = async () => (await saved(join(authority.runtime, 'receiver/exchange.json'))).exchanges.filter(row => !row.not_sent).length
  const until = async (check, label) => {
    const deadline = Date.now() + 90000
    while (!await check()) {
      const fatal = (await events()).findLast(event => event.event === 'error' && event.fatal)
      assert.ok(!fatal, fatal?.message)
      assert.ok(Date.now() < deadline, label)
      await pause(20)
    }
  }
  const next = async (payload, name) => {
    const before = await count(name)
    await command(payload)
    await until(async () => await count(name) > before, name)
    return (await events()).findLast(event => event.event === name)
  }

  if (authority.phase === 'work') {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
    await command({ action: 'send', prompt: request.prompt, scope: request.scope })
    await until(async () => await count('started') > 0, 'original cargo Task starts')
    const started = (await events()).find(event => event.event === 'started')
    await until(() => existsSync(join(root, 'quality-gate-0-arrived.json')), 'first actual native request reaches its external response barrier')
    const gate = await saved(join(root, 'quality-gate-0-arrived.json'))
    assert.equal(gate.origin, 'actual_terminal_http_response')
    assert.ok(gate.sdk_input_id)
    await command({ action: 'send', path: started.path, prompt: request.quality_recovery.supplement })
    await until(async () => (await events()).some(event => event.event === 'supplement' && event.state === 'received'), 'actual user supplement is received')
    const received = (await events()).find(event => event.event === 'supplement' && event.state === 'received')
    await writeFile(join(root, 'quality-gate-0-release.txt'), 'Release after the actual current user supplement was received.\n', { flag: 'wx' })
    await until(async () => (await events()).some(event => event.event === 'finished' && event.task_id === started.task_id), 'original parent keeps its local interruption after the completed child')
    const finished = (await events()).findLast(event => event.event === 'finished' && event.task_id === started.task_id)
    assert.equal(finished.turn.status, 'failed')
    assert.equal(finished.saved, true)
    assert.equal(finished.turn.task.id, started.task_id)
    assert.deepEqual(finished.turn.task.scope, request.scope)
    assert.deepEqual(finished.turn.task.approval, request.approval)
    const applied = (await events()).filter(event => event.event === 'supplement' && event.state === 'applied')
    assert.equal(applied.length, 1)
    assert.equal(applied[0].supplement.id, received.supplement.id)
    assert.equal(finished.turn.task.supplements.length, 1)
    assert.equal(finished.turn.task.supplements[0].id, received.supplement.id)
    assert.ok(finished.turn.task.supplements[0].applied_request !== null)
    assert.equal(finished.turn.delegations.length, 1)
    const work = finished.turn.delegations[0]
    assert.equal(work.status, 'completed')
    assert.equal(work.scope.root, join(request.scope.root, 'side'))
    assert.deepEqual(work.scope.allowed_actions, ['read'])
    assert.equal(work.requests, 2)
    const child = await readFile(work.record_path)
    const record = JSON.parse(child.toString('utf8'))
    assert.equal(record.turns.at(-1).request_owner_id, work.id)
    assert.deepEqual(record.turns.at(-1).execution_scope, work.scope)
    const exchange = await saved(join(authority.runtime, 'receiver/exchange.json'))
    assert.ok(exchange.exchanges.some(row => row.not_sent && row.fault_origin?.includes('after completed child')))
    await writeFile(join(root, 'original-before.json'), await readFile(started.path), { flag: 'wx' })
    await writeFile(join(root, 'original-child.json'), child, { flag: 'wx' })
    await writeFile(join(root, 'quality-recovery-original.json'), JSON.stringify({ started, finished,
      child_path: work.record_path, child_sha256: digest(child), applied_supplements: applied.length,
      supplement_id: received.supplement.id, applied_request: finished.turn.task.supplements[0].applied_request }, null, 2), { flag: 'wx' })
    state.task_id = started.task_id
    state.quality_recovery_delegated = { phase: 'work', child_id: work.id, child_requests: work.requests, applied_supplements: applied.length }
  } else {
    const original = await saved(join(root, 'quality-recovery-original.json'))
    assert.equal(digest(await readFile(original.child_path)), original.child_sha256)
    const childBlocked = await next({ action: 'open', path: original.child_path }, 'error')
    assert.ok(childBlocked.message)
    const opened = await next({ action: 'open', path: original.started.path }, 'conversation')
    assert.equal(opened.path, original.started.path)
    const openCount = await httpCount()
    assert.equal(openCount, 0)
    const verified = await next({ action: 'verify_recovery', path: original.started.path, task_id: original.started.task_id }, 'recovery')
    assert.equal(verified.recovery.can_resume, true)
    assert.equal(await httpCount(), 0)
    const finished = await next({ action: 'resume_task', path: original.started.path, task_id: original.started.task_id }, 'finished')
    assert.equal(finished.turn.task.id, original.started.task_id)
    assert.equal(finished.turn.status, 'completed', finished.turn.error)
    assert.equal(finished.saved, true)
    assert.deepEqual(finished.turn.task.scope, original.finished.turn.task.scope)
    assert.deepEqual(finished.turn.task.approval, original.finished.turn.task.approval)
    assert.deepEqual(finished.turn.task.supplements, original.finished.turn.task.supplements)
    assert.ok(finished.turn.task.requests > original.finished.turn.task.requests)
    assert.deepEqual(finished.turn.actions.map(action => action.kind), ['read'])
    assert.equal(finished.turn.actions[0].target, 'handoff.txt')
    assert.equal(digest(await readFile(original.child_path)), original.child_sha256)
    await writeFile(join(root, 'recovery-observation.json'), JSON.stringify({ open_http_attempts: openCount,
      can_resume: verified.recovery.can_resume, applied_supplements: original.applied_supplements,
      child_path: original.child_path, child_sha256: original.child_sha256, child_resume_blocked_reason: childBlocked.message }, null, 2), { flag: 'wx' })
    state.task_id = original.started.task_id
    state.quality_recovery_delegated = { phase: 'reopen', original_task_id: original.started.task_id, open_http_attempts: openCount,
      child_record_unchanged: true, no_edit_or_child_replay: true }
  }
  state.passed = true
}
