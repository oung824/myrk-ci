import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runQualityRecoveryResume({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  const kind = request.quality_recovery.resume_kind
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const count = async name => (await events()).filter(item => item.event === name).length
  const saved = path => readFile(path, 'utf8').then(JSON.parse)
  const attempts = async () => (await saved(join(authority.runtime, 'receiver/exchange.json'))).exchanges.length
  const until = async (check, label) => {
    const deadline = Date.now() + 90000
    while (!await check()) { assert.ok(Date.now() < deadline, label); await pause(20) }
  }
  const next = async (payload, name) => {
    const before = await count(name)
    await command(payload)
    await until(async () => await count(name) > before, name)
    return (await events()).findLast(item => item.event === name)
  }
  const open = path => next({ action: 'open', path }, 'conversation')
  const actualImport = async path => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] })
    const before = await count('conversation')
    await execute('window.myrk.importConversation()')
    await until(async () => await count('conversation') > before, 'actual native source import')
    return (await events()).findLast(item => item.event === 'conversation')
  }
  if (authority.phase === 'work') {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
    await command({ action: 'send', prompt: request.prompt, scope: request.scope })
    await until(async () => await count('started') > 0, 'actual original task starts')
    const started = (await events()).find(item => item.event === 'started')
    if (kind === 'qg5-6') {
      await until(() => existsSync(join(root, 'save-boundary.json')), 'actual edit reaches the planned local save boundary')
      await until(async () => (await events()).some(item => item.event === 'error' && item.fatal), 'actual local service exit is reported')
      const boundary = await saved(join(root, 'save-boundary.json'))
      assert.equal(boundary.exit_code, 71)
      await writeFile(join(root, 'original-before.json'), await readFile(join(root, 'reliable-original.json')), { flag: 'wx' })
      state.planned_service_exit = boundary
    } else {
      const arrival = join(root, 'quality-stop-arrived.json')
      await until(() => existsSync(arrival), 'the actual native response reaches the stop checkpoint')
      const held = await saved(arrival)
      assert.equal(held.origin, 'terminal_response_after_completed_read')
      const stopped = await next({ action: 'stop', path: started.path }, 'stopping')
      assert.equal(stopped.task_id, started.task_id)
      assert.equal(stopped.path, started.path)
      await writeFile(join(root, 'quality-stop-release.txt'), 'Release only after the actual accepted stop\n', { flag: 'wx' })
      await until(async () => (await saved(started.path)).turns.at(-1).status === 'cancelled', 'actual stopped original is durable')
      const raw = await readFile(started.path)
      await writeFile(join(root, 'original-before.json'), raw, { flag: 'wx' })
      const legacy = JSON.parse(raw.toString('utf8'))
      const originalCount = await attempts()
      const originalLimit = legacy.turns.at(-1).task.request_limit
      assert.equal(legacy.turns.at(-1).task.requests, originalCount)
      legacy.version = 5
      const missing = ['request_owner_id', 'request_account', 'execution_scope', 'execution_facts', 'request_policy', 'total_requests', 'delegation_max_turns', 'max_turns', 'turns_used']
      for (const turn of legacy.turns) {
        for (const field of missing) { delete turn[field]; delete turn.task[field] }
        turn.task.request_limit ??= request.max_attempts
      }
      const bytes = Buffer.from(JSON.stringify(legacy, null, 2) + '\n')
      await writeFile(join(root, 'legacy-source-v5.json'), bytes, { flag: 'wx' })
      await writeFile(join(root, 'quality-legacy-projection.json'), JSON.stringify({ legacy_sha256: createHash('sha256').update(bytes).digest('hex'), legacy_missing_fields: missing,
        original_request_limit: originalLimit, legacy_projection_request_limit: legacy.turns.at(-1).task.request_limit,
        original_http_attempts: originalCount, stop_sdk_input_id: held.sdk_input_id, stop_accepted_task_id: stopped.task_id,
        projection_basis: 'Separate v5 format copy; explicit legacy-compatible cap or the frozen case cap.' }, null, 2), { flag: 'wx' })
    }
    await writeFile(join(root, 'quality-recovery-original.json'), JSON.stringify({ started }, null, 2), { flag: 'wx' })
    state.task_id = started.task_id
  } else {
    const { started } = await saved(join(root, 'quality-recovery-original.json'))
    const opened = await open(started.path)
    const openCount = await attempts()
    assert.equal(openCount, 0)
    if (kind === 'qg5-9') {
      const imported = await actualImport(join(root, 'legacy-source-v5.json'))
      await open(imported.path)
      const blocked = await next({ action: 'resume_task', path: imported.path, task_id: started.task_id }, 'error')
      assert.equal(await attempts(), 0)
      const projection = await saved(join(root, 'quality-legacy-projection.json'))
      await writeFile(join(root, 'recovery-observation.json'), JSON.stringify({ ...projection, reopened_http_attempts: await attempts(),
        imported_path: imported.path, resume_blocked_reason: blocked.message }, null, 2))
    } else {
      const blocked = await next({ action: 'resume_task', path: started.path, task_id: started.task_id }, 'error')
      const blockedCount = await attempts()
      const imported = await actualImport(join(root, 'original-before.json'))
      const unowned = await next({ action: 'verify_recovery', path: imported.path, task_id: started.task_id }, 'recovery')
      const importedBlocked = await next({ action: 'resume_task', path: imported.path, task_id: started.task_id }, 'error')
      await open(started.path)
      const verified = await next({ action: 'verify_recovery', path: started.path, task_id: started.task_id }, 'recovery')
      assert.equal(verified.recovery.can_resume, true)
      await next({ action: 'resume_task', path: started.path, task_id: started.task_id }, 'finished')
      const reliable = await readFile(join(root, 'original-before.json'))
      const pending = await readFile(join(root, 'pending-original.json'))
      await writeFile(join(root, 'recovery-observation.json'), JSON.stringify({ open_http_attempts: openCount,
        can_resume: verified.recovery.can_resume, planned_local_exit: 71, reliable_and_pending_differ: !reliable.equals(pending),
        blocked_resume_http_attempts: blockedCount, imported_unknown_blocks_resume: unowned.recovery.can_resume === false && Boolean(importedBlocked.message),
        verified_effect: verified.recovery.actions.at(-1).effect, blocked_reason: blocked.message, open_effect: opened.recovery.actions.at(-1).effect }, null, 2))
    }
    state.task_id = started.task_id
  }
  state.passed = true
}
