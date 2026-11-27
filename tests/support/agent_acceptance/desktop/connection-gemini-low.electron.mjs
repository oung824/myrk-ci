import assert from 'node:assert/strict'
import { app, BrowserWindow, Menu } from 'electron'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from '../../../../desktop/tests/run-context.mjs'
import { installNormalExit } from '../../../../desktop/tests/desktop-control.mjs'
import { runConnectionSettings } from './connection-settings.mjs'

installNormalExit(app)
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
app.commandLine.appendSwitch('disable-background-networking')

const root = process.env.MYRK_CONNECTION_GEMINI_ROOT
const state = { pid: process.pid, passed: false }
const until = async (check, label) => {
  const deadline = Date.now() + 20000
  while (!await check()) { assert.ok(Date.now() < deadline, label); await pause(25) }
}

async function run() {
  let events
  try {
    const request = JSON.parse(await readFile(join(root, 'scenario.json'), 'utf8'))
    await import(builtMain())
    await app.whenReady()
    state.isolated_profile = app.getPath('userData') === process.env.MYRK_USER_DATA_DIRECTORY
    assert.equal(state.isolated_profile, true)
    await until(() => BrowserWindow.getAllWindows().length === 1, 'real Main window')
    const window = BrowserWindow.getAllWindows()[0]
    await until(() => !window.webContents.isLoading(), 'renderer loaded')
    const execute = script => window.webContents.executeJavaScript(script)
    const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
    events = () => execute('window.geminiSettingsEvents')
    await execute('window.geminiSettingsEvents=[]; window.myrk.subscribe(event=>window.geminiSettingsEvents.push(event)); void 0')
    await command({ action: 'snapshot' })
    await until(async () => (await events()).some(event => event.event === 'snapshot'), 'ready service snapshot')
    await command({ action: 'configure', key: 'synthetic-gemini-low-only', provider_config: request.config })
    await command({ action: 'snapshot' })
    await until(async () => (await events()).some(event => event.event === 'snapshot' && event.configured === true), 'configured snapshot')
    state.configured_provider = (await events()).findLast(event => event.event === 'snapshot' && event.configured === true).provider_config
    assert.equal(state.configured_provider.model, request.config.model)
    await runConnectionSettings({ authority: { protocol: 'google', provider_config: request.config }, request, command, events, execute, state })
  } catch (error) {
    state.error = error.stack
  } finally {
    try {
      if (events) await writeFile(join(root, 'public-events.json'), JSON.stringify(await events(), null, 2))
    } catch (error) {
      state.passed = false
      state.capture_error = error.stack
    }
    try {
      await writeFile(join(root, 'state.json'), JSON.stringify(state, null, 2))
    } finally {
      const quit = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit')
      if (quit) quit.click(); else app.quit()
    }
  }
}

void run()
