import assert from 'node:assert/strict'
import { app, BrowserWindow, Menu } from 'electron'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'
import { configureOwnerProfile } from '../../tests/support/agent_acceptance/desktop/owner-profile.mjs'

const directory = process.env.MYRK_PROFILE_CONTEXT_DIRECTORY
const phase = process.env.MYRK_PROFILE_CONTEXT_PHASE
const profile = join(directory, 'saved-profile')
await mkdir(profile, { recursive: true })
installNormalExit(app)
if (phase === 'save') app.setPath('userData', profile)
else configureOwnerProfile(app, profile, join(directory, 'formal-owner'), 'real')
process.env.MYRK_USER_DATA_DIRECTORY = profile
app.disableHardwareAcceleration()
const waitFor = async check => {
  const deadline = Date.now() + 20000
  while (!await check()) { assert.ok(Date.now() < deadline, 'Connection profile context timed out'); await pause(25) }
}
async function run() {
  try {
    await import(builtMain())
    await app.whenReady()
    await waitFor(() => BrowserWindow.getAllWindows().length === 1)
    const window = BrowserWindow.getAllWindows()[0]
    await waitFor(() => !window.webContents.isLoading())
    const execute = code => window.webContents.executeJavaScript(code)
    await execute(`window.profileEvents=[];window.myrk.subscribe(event=>window.profileEvents.push(event));window.myrk.command({action:'get_connection_storage'})`)
    await waitFor(() => execute(`window.profileEvents.some(event=>event.event==='connection_storage')`))
    if (phase === 'save') {
      await execute(`window.myrk.command({action:'configure',key:'synthetic-profile-context-only'})`)
      await waitFor(() => execute(`window.profileEvents.filter(event=>event.event==='connection_storage').at(-1).saved.length===1`))
    }
    const events = await execute('window.profileEvents')
    const state = events.filter(event => event.event === 'connection_storage').at(-1)
    assert.equal(app.getPath('userData'), profile)
    assert.equal(state.error, null)
    assert.equal(state.available.length, 1)
    assert.equal(state.saved.length, 1)
    assert.equal(events.some(event => event.event === 'started'), false)
    assert.equal(JSON.stringify(events).includes('synthetic-profile-context-only'), false)
    await writeFile(join(directory, `${phase}.json`), JSON.stringify({ passed: true, state, profile: app.getPath('userData'), session_profile: app.getPath('sessionData'), automatic_work: false }))
  } catch (error) {
    await writeFile(join(directory, `${phase}.json`), JSON.stringify({ passed: false, error: error.message, profile: app.getPath('userData'), session_profile: app.getPath('sessionData') }))
  } finally {
    const quit = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit')
    if (quit) quit.click(); else app.quit()
  }
}
void run()
