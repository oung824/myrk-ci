import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { closeSync, openSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { save, verifyEntryAuthority } from './queue-entry-authority.mjs'

const authority = verifyEntryAuthority(dirname(process.env.MYRK_ACCEPTANCE_AUTHORITY))
const require = createRequire(join(process.env.MYRK_CONNECTION_PROJECT_ROOT, 'desktop/package.json'))
const { app } = require('electron')
app.setPath('userData', join(authority.runtime, 'relay/profile'))
app.disableHardwareAcceleration()
const state = { pid: process.pid, owner_pid: process.ppid, runtime_versions: process.versions, requests: 0, references_released: false }
const saveState = () => save(join(authority.runtime, 'relay/state.json'), state)
const send = (target, value) => new Promise((accept, reject) => target.send(value, error => error ? reject(error) : accept()))
async function run() {
await app.whenReady()
const stdout = openSync(join(authority.runtime, 'receiver/stdout.log'), 'wx')
const stderr = openSync(join(authority.runtime, 'receiver/stderr.log'), 'wx')
const child = spawn(process.execPath, [join(dirname(fileURLToPath(import.meta.url)), 'desktop.mjs')], {
  windowsHide: true, stdio: ['ignore', stdout, stderr, 'ipc'], env: { ...process.env,
    MYRK_ACCEPTANCE_ROLE: 'receiver', MYRK_CONNECTION_RECEIVER: '1', MYRK_QUEUE_HOST_PID: String(process.pid),
    MYRK_CONNECTION_OWNER_CASE: '', MYRK_DATA_DIRECTORY: authority.scenario_root ? join(authority.scenario_root, 'records') : join(authority.runtime, 'receiver/records'),
    MYRK_USER_DATA_DIRECTORY: join(authority.runtime, 'receiver/profile') } })
closeSync(stdout); closeSync(stderr)
state.child_pid = child.pid; saveState()
child.on('message', async message => {
  if (message?.type !== 'request_connection') return
  let received, reply
  try {
    assert.equal(state.requests++, 0)
    assert.equal(message.instance_pid, child.pid); assert.equal(message.host_pid, process.pid)
    assert.equal(message.case_nonce, authority.case_nonce); assert.equal(message.authorization_sha256, authority.authorization_sha256)
    verifyEntryAuthority(dirname(process.env.MYRK_ACCEPTANCE_AUTHORITY))
    received = await new Promise((accept, reject) => {
      const timeout = setTimeout(() => reject(new Error('Owner timed out')), 30000)
      process.once('message', value => { clearTimeout(timeout); accept(value) })
      process.send({ type: 'request_owner_connection', nonce: process.env.MYRK_CONNECTION_HANDOFF_NONCE,
        instance_pid: process.pid, case_nonce: authority.case_nonce, authorization_sha256: authority.authorization_sha256 })
    })
    assert.equal(received.type, 'owner_connection_result'); assert.equal(received.owner_pid, process.ppid)
    assert.equal(received.child_pid, process.pid); assert.equal(received.nonce, process.env.MYRK_CONNECTION_HANDOFF_NONCE)
    assert.equal(received.case_nonce, authority.case_nonce); assert.equal(received.authorization_sha256, authority.authorization_sha256)
    await send(process, { type: 'owner_connection_received', nonce: received.nonce, instance_pid: process.pid,
      owner_pid: process.ppid, case_nonce: authority.case_nonce, authorization_sha256: authority.authorization_sha256 })
    reply = { type: 'connection_result', nonce: message.nonce, owner_pid: process.pid, child_pid: child.pid,
      case_nonce: authority.case_nonce, authorization_sha256: authority.authorization_sha256,
      provider_config: authority.provider_config, key: received.key }
    await send(child, reply); state.delivered_once = true
  } catch (error) { state.error = error.stack }
  finally { if (received) received.key = ''; if (reply) reply.key = ''; received = reply = null; state.references_released = true; saveState() }
})
child.once('close', (code, signal) => { state.child_exit = code; state.child_signal = signal; saveState(); app.exit(code ?? 1) })
}
void run().catch(error => { state.error = error.stack; saveState(); app.exit(1) })
