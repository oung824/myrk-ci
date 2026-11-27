import { app, BrowserWindow, dialog } from 'electron'
import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from '../../../../desktop/tests/run-context.mjs'
import { installNormalExit } from '../../../../desktop/tests/desktop-control.mjs'

installNormalExit(app)
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
const root = process.env.MYRK_ACCEPTANCE_RESTART_ROOT
const phase = process.env.MYRK_ACCEPTANCE_RESTART_PHASE
async function run() {
const request = JSON.parse(await readFile(join(root, 'scenario.json'), 'utf8'))
const wait = async check => {
  const deadline = Date.now() + 30000
  while (!await check()) { assert.ok(Date.now() < deadline, 'Waiting for actual desktop recovery event'); await pause(25) }
}
let window
let error
let events = []
try {
  dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [request.folder] })
  await import(builtMain())
  await app.whenReady()
  await wait(() => BrowserWindow.getAllWindows().length === 1)
  window = BrowserWindow.getAllWindows()[0]
  await wait(() => !window.webContents.isLoading())
  await window.webContents.executeJavaScript(`window.resumeEvents=[]; window.myrk.subscribe(event=>window.resumeEvents.push(event)); window.myrk.command({action:'snapshot'})`)
  const all = () => window.webContents.executeJavaScript('window.resumeEvents')
  const command = value => window.webContents.executeJavaScript(`window.myrk.command(${JSON.stringify(value)})`)
  await wait(async () => (await all()).some(item => item.event === 'snapshot'))
  if (phase === 'work') {
    await command({ action: 'configure', key: 'synthetic-restart-only', provider_config: request.config })
    await window.webContents.executeJavaScript('window.myrk.selectWorkspace()')
    await command({ action: 'send', prompt: 'Edit the synthetic file once, run the specified check, and preserve any actual service error.', scope: request.scope })
    await wait(async () => (await all()).some(item => item.event === 'finished'))
    events = await all()
    const started = events.find(item => item.event === 'started')
    const finished = events.find(item => item.event === 'finished')
    assert.equal(finished.turn.service_failure, true)
    assert.equal(finished.turn.task.requests, 4)
    assert.deepEqual(finished.turn.actions.map(item => [item.kind, item.status]), [['read', 'completed'], ['edit', 'completed'], ['check', 'completed']])
    assert.equal(finished.turn.actions.at(-1).exit_code, 0)
    assert.equal(finished.turn.actions.at(-1).cleanup_complete, true)
    assert.equal(await readFile(join(request.folder, 'runs.txt'), 'utf8'), 'x')
    await writeFile(join(root, 'original-state.json'), JSON.stringify({ started, finished }, null, 2))
  } else {
    const original = JSON.parse(await readFile(join(root, 'original-state.json'), 'utf8'))
    await command({ action: 'open', path: original.started.path })
    await wait(async () => (await all()).some(item => item.event === 'conversation' && item.path === original.started.path))
    const opened = (await all()).find(item => item.event === 'conversation' && item.path === original.started.path)
    assert.equal(opened.running, false)
    assert.equal((await all()).some(item => item.event === 'started'), false)
    const exchange = JSON.parse(await readFile(process.env.MYRK_ACCEPTANCE_RESUME_SCRIPT.replace(/script\.json$/, 'exchange.json'), 'utf8'))
    assert.equal(exchange.exchanges.length, 0)
    await command({ action: 'verify_recovery', path: original.started.path, task_id: original.started.task_id })
    await wait(async () => (await all()).some(item => item.event === 'recovery'))
    assert.equal((await all()).find(item => item.event === 'recovery').recovery.can_resume, true)
    await command({ action: 'configure', key: 'synthetic-restart-only', provider_config: request.config })
    await command({ action: 'resume_task', path: original.started.path, task_id: original.started.task_id })
    await wait(async () => (await all()).some(item => item.event === 'finished'))
    events = await all()
    const finished = events.find(item => item.event === 'finished')
    assert.equal(finished.turn.status, 'completed')
    assert.equal(finished.turn.task.id, original.started.task_id)
    assert.equal(finished.turn.task.requests, 5)
    assert.equal(finished.turn.task.total_requests, 5)
    assert.deepEqual(finished.turn.task.scope, original.finished.turn.task.scope)
    assert.deepEqual(finished.turn.task.approval, original.finished.turn.task.approval)
    assert.deepEqual(finished.turn.actions, [])
    assert.equal(await readFile(join(request.folder, 'runs.txt'), 'utf8'), 'x')
    assert.equal(await readFile(join(request.folder, 'notes.txt'), 'utf8'), 'edited-once-8149\n')
    await writeFile(join(root, 'reopened-state.json'), JSON.stringify({ opened, finished, open_http_attempts: exchange.exchanges.length }, null, 2))
  }
} catch (failure) { error = failure.stack; if (window && !window.isDestroyed()) events = await window.webContents.executeJavaScript('window.resumeEvents').catch(() => events) }
await writeFile(join(root, `${phase}-events.json`), JSON.stringify({ phase, error, events }, null, 2))
if (window && !window.isDestroyed()) await window.webContents.executeJavaScript('window.myrk.quit()')
else app.quit()
}
void run()
