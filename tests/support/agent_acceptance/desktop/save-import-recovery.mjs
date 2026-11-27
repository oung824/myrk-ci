import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'

export async function runRecoveryScenario({ authority, request, command, events, execute, state }) {
  const root = authority.scenario_root
  const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
  const { dialog } = require('electron')
  const until = async (check, label) => {
    const deadline = Date.now() + 45000
    while (!await check()) { assert.ok(Date.now() < deadline, label); await pause(25) }
  }
  const next = async (value, name) => {
    const before = (await events()).filter(item => item.event === name).length
    await command(value)
    await until(async () => (await events()).filter(item => item.event === name).length > before, name)
    return (await events()).findLast(item => item.event === name)
  }
  const open = path => next({ action: 'open', path }, 'conversation')
  const attempts = async () => JSON.parse(await readFile(join(authority.runtime, 'receiver/exchange.json'), 'utf8')).exchanges.length
  if (authority.case_id === 'formal.recovery-family') return runFamily({ authority, request, command, events, execute, state, root, dialog, until, next, open, attempts })
  if (authority.phase === 'work') {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
    await command({ action: 'send', prompt: request.prompt, scope: request.scope })
    await until(() => existsSync(join(root, 'save-boundary.json')), 'actual local save-boundary exit')
    await until(async () => (await events()).some(item => item.event === 'error' && item.fatal), 'actual service exit')
    const started = (await events()).find(item => item.event === 'started')
    assert.ok(started)
    await writeFile(join(root, 'recovery-started.json'), JSON.stringify(started, null, 2), { flag: 'wx' })
    state.planned_service_exit = JSON.parse(await readFile(join(root, 'save-boundary.json'), 'utf8'))
    state.task_id = started.task_id
  } else {
    const started = JSON.parse(await readFile(join(root, 'recovery-started.json'), 'utf8'))
    const opened = await open(started.path)
    const open_http_attempts = await attempts()
    const blocked = await next({ action: 'resume_task', path: started.path, task_id: started.task_id }, 'error')
    const blocked_resume_http_attempts = await attempts()
    const importedSource = join(root, 'reliable-original.json')
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [importedSource] })
    const before = (await events()).filter(item => item.event === 'conversation').length
    await execute('window.myrk.importConversation()')
    await until(async () => (await events()).filter(item => item.event === 'conversation').length > before, 'public native import')
    const imported = (await events()).findLast(item => item.event === 'conversation')
    const importedRecovery = await next({ action: 'verify_recovery', path: imported.path, task_id: started.task_id }, 'recovery')
    const importedBlocked = await next({ action: 'resume_task', path: imported.path, task_id: started.task_id }, 'error')
    assert.equal(await attempts(), 0)
    await open(started.path)
    const verified = await next({ action: 'verify_recovery', path: started.path, task_id: started.task_id }, 'recovery')
    let finished = null
    if (verified.recovery.can_resume) finished = await next({ action: 'resume_task', path: started.path, task_id: started.task_id }, 'finished')
    await writeFile(join(root, 'recovery-reopened.json'), JSON.stringify({ opened, recovery: verified.recovery,
      finished, open_http_attempts, blocked_resume_http_attempts, blocked, imported, imported_recovery: importedRecovery.recovery,
      imported_blocked: importedBlocked }, null, 2), { flag: 'wx' })
    state.task_id = started.task_id
  }
  state.passed = true
}


async function runFamily({ authority, request, command, events, execute, state, root, dialog, until, next, open, attempts }) {
  if (authority.phase === 'work') {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.scope.root] })
    assert.equal(await execute('window.myrk.selectWorkspace()'), request.scope.root)
    await command({ action: 'send', prompt: request.prompt, scope: request.scope })
    await until(() => existsSync(join(root, 'family-child-arrived.flag')), 'actual child terminal request')
    const started = (await events()).find(item => item.event === 'started')
    const supplement = await next({ action: 'send', path: started.path, prompt: request.supplement }, 'supplement')
    await writeFile(join(root, 'family-child-release.flag'), 'Explicit supplement is saved; deliver held HTTP reply.\n', { flag: 'wx' })
    await until(async () => (await events()).some(item => item.event === 'finished'), 'original parent fails after completed child')
    const finished = (await events()).findLast(item => item.event === 'finished')
    const raw = await readFile(started.path)
    const record = JSON.parse(raw.toString('utf8'))
    const work = record.turns.at(-1).delegations[0]
    await writeFile(join(root, 'family-original.json'), raw, { flag: 'wx' })
    await writeFile(join(root, 'child-original.json'), await readFile(work.record_path), { flag: 'wx' })
    await writeFile(join(root, 'family-started.json'), JSON.stringify({ started, supplement, finished, child_path: work.record_path }, null, 2), { flag: 'wx' })
    state.task_id = started.task_id
  } else {
    const original = JSON.parse(await readFile(join(root, 'family-started.json'), 'utf8'))
    const opened = await open(original.started.path)
    const open_http_attempts = await attempts()
    const child_open_blocked = await next({ action: 'open', path: original.child_path }, 'error')
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [original.child_path] })
    const before = (await events()).filter(item => item.event === 'conversation').length
    await execute('window.myrk.importConversation()')
    await until(async () => (await events()).filter(item => item.event === 'conversation').length > before, 'import original child for browsing')
    const imported = (await events()).findLast(item => item.event === 'conversation')
    const child_resume_blocked = await next({ action: 'resume_task', path: imported.path, task_id: original.started.task_id }, 'error')
    const child_browse_http_attempts = await attempts()
    await open(original.started.path)
    const recovery = await next({ action: 'verify_recovery', path: original.started.path, task_id: original.started.task_id }, 'recovery')
    let finished = null
    if (recovery.recovery.can_resume) finished = await next({ action: 'resume_task', path: original.started.path, task_id: original.started.task_id }, 'finished')
    await writeFile(join(root, 'family-reopened.json'), JSON.stringify({ opened, recovery: recovery.recovery, finished,
      open_http_attempts, child_browse_http_attempts, child_open_blocked, child_resume_blocked, imported }, null, 2), { flag: 'wx' })
    state.task_id = original.started.task_id
  }
  state.passed = true
}
