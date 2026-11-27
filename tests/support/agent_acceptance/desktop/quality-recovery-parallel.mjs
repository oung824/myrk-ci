import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { createServer } from 'node:http'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityRecoveryParallel({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  const config = request.quality_recovery
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const servers = {}
  const receivers = Object.fromEntries(['a', 'b'].map(key => [key, { received: [], closed: false, owner_pid: process.pid }]))
  const observation = { paths: {}, task_ids: {}, approval_decisions: [], stops: {}, main_pid: process.pid }
  const seen = new Set()
  class KnownDefaultPermissionLoss extends Error {}
  let initialOriginal
  let initialHttpCount
  let actualNewEvent
  const capturePermissionLoss = async failed => {
    if (!actualNewEvent || !initialOriginal || failed.path !== actualNewEvent.path) return false
    const fresh = JSON.parse(await readFile(failed.path, 'utf8'))
    const initial = JSON.parse(initialOriginal.toString('utf8'))
    const oldTask = initial.turns.at(-1).task
    const newTask = fresh.turns.at(-1).task
    if (JSON.stringify(oldTask.approval) !== JSON.stringify(request.approval)) return false
    const expected = new Set(request.approval.access.denied_paths)
    if (!expected.size || newTask.approval.revision === request.approval.revision
      || [...expected].every(path => newTask.approval.access.denied_paths.includes(path))) return false
    const native = await exchange()
    if (native.exchanges.length !== initialHttpCount) return false
    await writeFile(join(root, 'quality-permission-initial-before-new.original.json'), initialOriginal, { flag: 'wx' })
    await command({ action: 'stop', path: observation.paths.a })
    const deadline = Date.now() + 30000
    while (JSON.parse(await readFile(observation.paths.a, 'utf8')).turns.at(-1).status !== 'cancelled') {
      assert.ok(Date.now() < deadline, 'The still-running initial Task is durably cancelled after the independent permission failure')
      await pause(20)
    }
    const finalInitial = await readFile(observation.paths.a)
    const finalTask = JSON.parse(finalInitial.toString('utf8')).turns.at(-1).task
    assert.equal(finalTask.id, oldTask.id)
    assert.deepEqual(finalTask.approval, oldTask.approval)
    await writeFile(join(root, 'quality-permission-initial.original.json'), finalInitial, { flag: 'wx' })
    await writeFile(join(root, 'quality-permission-new.original.json'), await readFile(failed.path), { flag: 'wx' })
    const proof = { schema_version: 1, origin: 'public_new_after_temporary_configuration', actual_new_event: actualNewEvent,
      initial_path: observation.paths.a, initial_task_id: oldTask.id, new_path: failed.path, new_task_id: newTask.id,
      initial_original: 'quality-permission-initial.original.json', new_original: 'quality-permission-new.original.json',
      before_http_count: initialHttpCount, after_http_count: native.exchanges.length, failed_event: failed,
      explicit_mode_negative: false,
      initial_before_new_original: 'quality-permission-initial-before-new.original.json',
      initial_before_new_sha256: createHash('sha256').update(initialOriginal).digest('hex'),
      initial_final_sha256: createHash('sha256').update(finalInitial).digest('hex'),
      initial_stop_origin: 'actual_public_stop_after_independent_new_task_permission_failure' }
    await writeFile(join(root, 'quality-permission-change.json'), JSON.stringify(proof, null, 2), { flag: 'wx' })
    observation.paths.b = failed.path
    observation.task_ids.b = newTask.id
    observation.permission_change = proof
    state.known_default_permission_loss = proof
    state.task_id = oldTask.id
    state.passed = true
    return true
  }
  const exchange = () => readFile(join(authority.runtime, 'receiver/exchange.json'), 'utf8').then(JSON.parse)
  const until = async (check, label) => {
    const deadline = Date.now() + 90000
    while (!await check()) {
      for (const event of await events()) {
        const action = event.action
        if (event.event !== 'action' || action.status !== 'awaiting_approval' || seen.has(action.approval_id)) continue
        const key = ['a', 'b'].find(key => observation.task_ids[key] === event.task_id)
        assert.ok(key, 'Only either original independent task may propose this send')
        const original = config.parallel[key]
        const source = await readFile(original.source)
        const exact = action.kind === 'send' && action.target === receivers[key].url
          && action.prepared_parameters.path === original.source
          && action.prepared_parameters.payload_sha256 === original.source_sha256
          && createHash('sha256').update(source).digest('hex') === original.source_sha256
          && action.impact.includes(source.toString('utf8')) && action.impact.includes(receivers[key].url)
          && (config.adapter === 'qg5-3' || config.adapter === 'qg5-4' && key === 'b')
        assert.ok(exact, 'Actual current candidate binds only its own exact source and local receiver')
        seen.add(action.approval_id)
        observation.approval_decisions.push({ task_id: event.task_id, approval_id: action.approval_id,
          action_id: action.id, prepared_parameters: action.prepared_parameters, approved: true })
        await command({ action: 'approve_action', path: event.path, task_id: event.task_id,
          action_id: action.id, approval_id: action.approval_id, approved: true })
      }
      const fatal = (await events()).findLast(item => item.event === 'error' && item.fatal)
      assert.ok(!fatal, fatal?.message)
      const failed = (await events()).findLast(item => item.event === 'finished' && item.turn.status === 'failed')
      if (failed && await capturePermissionLoss(failed)) throw new KnownDefaultPermissionLoss('Actual new-chat permissions changed; dependent work remains unexecuted.')
      assert.ok(!failed, failed?.turn.error)
      assert.ok(Date.now() < deadline, label)
      await pause(20)
    }
  }
  const start = async key => {
    const scope = config.parallel[key].scope
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), scope.root)
    const before = (await events()).filter(item => item.event === 'started').length
    const prompt = (key === 'a' ? request.prompt : config.parallel[key].prompt)
      + (config.adapter === 'qg5-3' || key === 'b' && config.adapter === 'qg5-4' ? '\nLocal receiver: ' + receivers[key].url : '')
    await command({ action: 'send', prompt, scope })
    await until(async () => (await events()).filter(item => item.event === 'started').length > before, 'independent original chat starts')
    const started = (await events()).findLast(item => item.event === 'started')
    observation.paths[key] = started.path
    observation.task_ids[key] = started.task_id
  }
  const gate = index => until(() => existsSync(join(root, `quality-gate-${index}-arrived.json`)), `actual terminal response ${index} arrives`)
  const release = index => writeFile(join(root, `quality-gate-${index}-release.txt`), 'Release only after the actual planned user event\n', { flag: 'wx' })
  const finish = key => until(async () => (await events()).some(item => item.event === 'finished' && item.task_id === observation.task_ids[key]), 'original chat finishes')
  const stop = async key => {
    const captured = await exchange()
    const record = JSON.parse(await readFile(observation.paths[key], 'utf8'))
    const observed = { sdk_input_ids: captured.sdk_inputs.map(item => item.sdk_input_id),
      action_ids: record.turns.flatMap(turn => turn.actions.map(action => action.id)) }
    await command({ action: 'stop', path: observation.paths[key] })
    await until(async () => (await events()).some(item => item.event === 'stopping' && item.path === observation.paths[key] && item.task_id === observation.task_ids[key]), 'this exact task stop is accepted')
    observed.accepted = true
    observation.stops[key] = observed
    await until(async () => JSON.parse(await readFile(observation.paths[key], 'utf8')).turns.at(-1).status === 'cancelled', 'accepted stop is durably saved for the original task')
    observed.finished_event_observed = (await events()).some(item => item.event === 'finished' && item.task_id === observation.task_ids[key])
  }
  try {
    for (const key of ['a', 'b']) {
      servers[key] = createServer((incoming, response) => {
        const chunks = []
        incoming.on('data', chunk => chunks.push(chunk))
        incoming.once('end', () => { receivers[key].received.push(Buffer.concat(chunks).toString('hex')); response.writeHead(204); response.end() })
      })
      await new Promise((accept, reject) => { servers[key].once('error', reject); servers[key].listen(0, '127.0.0.1', accept) })
      receivers[key].url = `http://127.0.0.1:${servers[key].address().port}/${key}`
    }
    await writeFile(join(root, 'quality-parallel-receivers.json'), JSON.stringify(receivers, null, 2))
    await start('a')
    await gate(config.adapter === 'qg5-3' ? 0 : config.adapter === 'qg5-4' ? 2 : 1)
    initialOriginal = await readFile(observation.paths.a)
    initialHttpCount = (await exchange()).exchanges.length
    const count = (await events()).filter(item => item.event === 'conversation').length
    await command({ action: 'new' })
    await until(async () => (await events()).filter(item => item.event === 'conversation').length > count, 'another chat opens in the same Main')
    actualNewEvent = (await events()).findLast(item => item.event === 'conversation')
    await start('b')
    await gate(config.adapter === 'qg5-3' ? 1 : 3)
    if (config.adapter === 'qg5-3') {
      await release(0); await finish('a')
      await release(1); await finish('b')
    } else {
      await stop('a')
      const before = (await events()).filter(item => item.event === 'snapshot').length
      await command({ action: 'snapshot' })
      await until(async () => (await events()).filter(item => item.event === 'snapshot').length > before, 'independent running task remains visible')
      const snapshot = (await events()).findLast(item => item.event === 'snapshot')
      observation.b_running_after_a_stop = snapshot.conversations.find(item => item.path === observation.paths.b).running
      assert.equal(observation.b_running_after_a_stop, true)
      if (config.adapter === 'qg5-4') { await release(3); await gate(5) }
      await stop('b')
    }
    state.task_id = observation.task_ids.a
    state.passed = true
  } catch (error) {
    if (!(error instanceof KnownDefaultPermissionLoss)) throw error
  } finally {
    for (const key of ['a', 'b']) {
      if (!servers[key]?.listening) continue
      servers[key].closeAllConnections()
      await new Promise(accept => servers[key].close(accept))
      receivers[key].closed = true
    }
    await writeFile(join(root, 'quality-parallel-receivers.json'), JSON.stringify(receivers, null, 2))
    await writeFile(join(root, 'quality-parallel-observation.json'), JSON.stringify(observation, null, 2))
    state.quality_recovery_parallel = observation
  }
}
