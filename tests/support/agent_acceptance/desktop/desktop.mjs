import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, isAbsolute, join, relative } from 'node:path'
import { pathToFileURL } from 'node:url'
import { setTimeout as pause } from 'node:timers/promises'
import { save, verifyEntryAuthority } from './queue-entry-authority.mjs'

const authority = verifyEntryAuthority(dirname(process.env.MYRK_ACCEPTANCE_AUTHORITY))
const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
const { app, BrowserWindow, Menu, safeStorage } = require('electron')
const state = { pid: process.pid, parent_pid: process.ppid, runtime_versions: process.versions, encrypt_attempts: 0, decrypt_attempts: 0, passed: false }
const root = authority.runtime
app.setPath('userData', join(root, 'receiver/profile'))
app.setPath('sessionData', join(root, 'receiver/session'))
app.disableHardwareAcceleration()
app.commandLine.appendSwitch('disable-background-networking')
safeStorage.encryptStringAsync = async () => { state.encrypt_attempts++; throw new Error('Receiver cannot save credentials') }
safeStorage.decryptStringAsync = async () => { state.decrypt_attempts++; throw new Error('Receiver cannot read credentials') }
const saveState = () => save(join(root, 'receiver/state.json'), state)
const wait = async (check, label) => {
  const deadline = Date.now() + (authority.grant.transport === 'real' ? 1200000 : 120000)
  while (!await check()) { assert.ok(Date.now() < deadline, label); await pause(25) }
}
const quit = () => { const item = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit'); if (item) item.click(); else app.quit() }
const timer = setInterval(() => { if (existsSync(join(root, 'relay/normal-exit.flag'))) quit() }, 50)
app.once('will-quit', () => { clearInterval(timer); state.normal_exit = true; saveState() })
async function run() { try {
  await import(pathToFileURL(authority.main).href)
  state.profile = app.getPath('userData')
  state.connection_store = join(state.profile, 'connections.enc')
  const part = relative(root, state.profile)
  state.synthetic_profile = part !== '' && !part.startsWith('..') && !isAbsolute(part)
  await app.whenReady()
  await wait(() => BrowserWindow.getAllWindows().length === 1, 'receiver window')
  const window = BrowserWindow.getAllWindows()[0]
  await wait(() => !window.webContents.isLoading(), 'receiver renderer')
  assert.equal(window.isVisible(), false)
  const execute = script => window.webContents.executeJavaScript(script)
  const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
  const events = () => execute('window.acceptanceEvents')
  await execute('window.acceptanceEvents=[];window.myrk.subscribe(value=>window.acceptanceEvents.push(value));window.myrk.command({action:"snapshot"})')
  await wait(async () => (await events()).some(event => event.event === 'snapshot'), 'initial snapshot')
  await new Promise((accept, reject) => {
    const timeout = setTimeout(() => reject(new Error('Temporary connection timed out')), 45000)
    process.once('myrk:temporary-connection', result => { clearTimeout(timeout); result.configured ? accept() : reject(new Error('Temporary connection rejected')) })
    assert.equal(process.emit('myrk:request-private-connection'), true)
  })
  process.emit('myrk:request-private-connection')
  await command({ action: 'snapshot' })
  await wait(async () => (await events()).filter(event => event.event === 'snapshot').at(-1)?.configured === true, 'temporary configuration snapshot')
  const configured = (await events()).filter(event => event.event === 'snapshot').at(-1)
  assert.deepEqual(configured.provider_config, authority.provider_config, 'Actual receiver settings must match the frozen execution plan before sending')
  state.authorized_provider_config = configured.provider_config
  if (authority.case_id !== 'desktop.handoff') {
    const { runScenario } = await import('./scenarios.mjs')
    await runScenario({ authority, window, command, events, execute, wait, state })
    const settingsSave = ['formal.delegation-child-limit','formal.delegation-missing-final-check'].includes(authority.case_id) && state.explicit_child_limit_configured === true
    assert.equal(state.encrypt_attempts, settingsSave || authority.case_id === "formal.provider-settings" && authority.protocol === "deepseek" ? 1 : 0)
    assert.equal(state.decrypt_attempts, 0)
    assert.equal(existsSync(join(state.profile, 'connections.enc')), false)
    save(join(root, 'receiver/events.json'), await events()); saveState()
    if (['formal.recovery-unknown', 'quality.qg5.formal.6'].includes(authority.case_id) && authority.phase === 'work') {
      const previous = (await events()).filter(item => item.event === 'shutdown_blocked').length
      await execute('window.myrk.quit()')
      await wait(async () => (await events()).filter(item => item.event === 'shutdown_blocked').length > previous, 'honest blocked shutdown after local service exit')
      state.planned_main_exit = 72
      save(join(root, 'receiver/events.json'), await events()); saveState(); app.exit(72)
    } else quit()
    return
  }
  await command({ action: 'send', prompt: readFileSync(join(root, 'frozen-connection-material.txt'), 'utf8') })
  await wait(async () => (await events()).some(event => event.event === 'finished'), 'formal product task')
  const finished = (await events()).findLast(event => event.event === 'finished')
  assert.equal(finished.turn.status, 'completed')
  if (authority.grant.transport === 'mock') assert.equal(finished.turn.answer, 'Formal handoff completed.')
  else assert.ok(finished.turn.answer.length > 0)
  assert.equal(finished.turn.requests, 1)
  assert.equal(state.encrypt_attempts + state.decrypt_attempts, 0)
  assert.equal(existsSync(join(root, 'receiver/profile/connections.enc')), false)
  state.task_id = finished.turn.task.id; state.product_attempts = finished.turn.requests; state.passed = true
  save(join(root, 'receiver/events.json'), await events()); saveState(); quit()
} catch (error) {
  state.error = error.stack
  const window = BrowserWindow.getAllWindows()[0]
  let observed = []
  if (window && !window.isDestroyed()) {
    try { observed = await window.webContents.executeJavaScript('window.acceptanceEvents || []') }
    catch (captureError) { state.events_capture_error = captureError.stack }
  } else state.events_capture_error = 'The receiver window was unavailable after the original error.'
  save(join(root, 'receiver/events.json'), observed)
  saveState(); quit()
} }
void run()
