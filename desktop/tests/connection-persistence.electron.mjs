import { app, BrowserWindow, Menu } from 'electron'
import assert from 'node:assert/strict'
import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'

installNormalExit(app)
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
const directory = process.env.MYRK_CREDENTIAL_TEST_DIRECTORY
const phase = process.env.MYRK_CREDENTIAL_TEST_PHASE
const waitFor = async check => {
  const deadline = Date.now() + 20000
  while (!await check()) { assert.ok(Date.now() < deadline, 'Credential behavior timed out'); await pause(25) }
}
async function run() {
try {
  await import(builtMain())
  await app.whenReady()
  await waitFor(() => BrowserWindow.getAllWindows().length === 1)
  const window = BrowserWindow.getAllWindows()[0]
  await waitFor(() => !window.webContents.isLoading())
  const execute = code => window.webContents.executeJavaScript(code)
  await execute(`window.credentialEvents=[];window.myrk.subscribe(event=>window.credentialEvents.push(event));window.myrk.command({action:'snapshot'});window.myrk.command({action:'get_connection_storage'})`)
  await waitFor(() => execute(`window.credentialEvents.some(event=>event.event==='snapshot') && window.credentialEvents.some(event=>event.event==='connection_storage')`))
  const command = value => execute(`window.myrk.command(${JSON.stringify(value)})`)
  if (phase === 'save') await command({ action: 'configure', key: 'synthetic-native-credential-7f83' })
  const events = await execute('window.credentialEvents')
  const state = events.filter(event => event.event === 'connection_storage').at(-1)
  assert.equal(state.error, null)
  assert.equal(state.available.length, phase === 'forgotten' ? 0 : 1)
  assert.equal(state.saved.length, phase === 'forgotten' ? 0 : 1)
  assert.equal(events.some(event => event.event === 'started'), false)
  assert.equal(JSON.stringify(events).includes('synthetic-native-credential-7f83'), false)
  if (phase === 'restore') {
    assert.equal(events.find(event => event.event === 'snapshot').configured, true)
    await command({ action: 'configure' })
    await command({ action: 'forget_connection', provider_config: state.saved[0] })
    await waitFor(() => execute(`window.credentialEvents.filter(event=>event.event==='snapshot').at(-1).configured === false`))
  }
  await writeFile(join(directory, `${phase}.json`), JSON.stringify({ passed: true, saved: state.saved.length, noAutomaticWork: true, noSecretEvents: true }))
} catch (error) {
  await writeFile(join(directory, `${phase}.json`), JSON.stringify({ passed: false, error: String(error) }))
} finally {
  const quit = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit')
  if (quit) quit.click()
  else app.quit()
}
}
void run()
