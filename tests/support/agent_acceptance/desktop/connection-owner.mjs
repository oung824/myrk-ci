import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, isAbsolute, join, relative } from 'node:path'
import { pathToFileURL } from 'node:url'
import { setTimeout as pause } from 'node:timers/promises'
import { save, verifyEntryAuthority } from './queue-entry-authority.mjs'
import { configureOwnerProfile } from './owner-profile.mjs'

const directory = dirname(process.env.MYRK_ACCEPTANCE_AUTHORITY)
const authority = verifyEntryAuthority(directory)
const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
const { app, Menu, safeStorage } = require('electron')
const root = authority.runtime
const state = { pid: process.pid, runtime_versions: process.versions, synthetic: authority.grant.transport === 'mock', normal_profile_accesses: 0 }
const profile = authority.grant.transport === 'mock' ? join(root, 'owner/profile') : authority.grant.connection_profile
assert.ok(profile, 'An explicit real connection profile is required')
configureOwnerProfile(app, profile, root, authority.grant.transport)
process.env.MYRK_USER_DATA_DIRECTORY = profile
app.disableHardwareAcceleration()
app.commandLine.appendSwitch('disable-background-networking')
const quit = () => { const item = Menu.getApplicationMenu()?.getMenuItemById('myrk-quit'); if (item) item.click(); else app.quit() }
const timer = setInterval(() => { if (existsSync(join(root, 'normal-exit.flag'))) quit() }, 50)
app.once('will-quit', () => { clearInterval(timer); state.normal_exit = true; save(join(root, 'owner/state.json'), state) })
async function run() { try {
  while (!existsSync(process.env.MYRK_PROCESS_OWNER_FILE)) await pause(20)
  await app.whenReady()
  if (authority.grant.transport === 'mock') {
    mkdirSync(profile, { recursive: true })
    assert.equal(existsSync(join(profile, 'connections.enc')), false)
    const savedProvider = { ...authority.provider_config, name: 'Synthetic saved connection', context_window_override: 128000 }
    const encrypted = await safeStorage.encryptStringAsync(JSON.stringify({ version: 1,
      connections: [{ provider_config: savedProvider, key: 'acceptance-synthetic-only' }], selected_provider: savedProvider }))
    writeFileSync(join(profile, 'connections.enc'), encrypted)
    state.synthetic_store_created = true
  }
  verifyEntryAuthority(directory)
  await import(pathToFileURL(authority.main).href)
  state.profile = app.getPath('userData')
  state.connection_store = join(state.profile, 'connections.enc')
  const part = relative(root, state.profile)
  state.synthetic_profile = part !== '' && !part.startsWith('..') && !isAbsolute(part)
  state.normal_profile_accesses = state.synthetic_profile ? 0 : 1
  state.main_loaded = true; save(join(root, 'owner/state.json'), state)
} catch (error) {
  state.error = error.stack; save(join(root, 'owner/state.json'), state); app.exit(1)
} }
void run()
