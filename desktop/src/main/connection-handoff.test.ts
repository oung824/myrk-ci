// @vitest-environment node
import { delimiter, join, resolve } from 'node:path'
import { readFile, writeFile } from 'node:fs/promises'
import { spawn } from 'node:child_process'
import { rolldown } from 'rolldown'
import { expect, test } from 'vitest'
import type { ServiceEvent } from '../shared/protocol'
import { testDirectory } from '../../tests/run-context.mjs'
import { ConnectionStore } from './connection-store'
import { ConnectionManager } from './connection-manager'
import { PythonService } from './python-service'
import { startConnectionHandoff, type ConnectionHandoffState } from './connection-handoff'

test.each([true, false])('an owner hands one exact connection to its own child without quitting itself; receipt=%s', async acknowledge => {
  const directory = await testDirectory('owner-handoff-')
  const entry = join(directory, 'child.mjs')
  const exitFlag = join(directory, 'normal-exit.flag')
  await writeFile(entry, `
import { existsSync } from 'node:fs'
const request = { type: 'request_owner_connection', nonce: process.env.MYRK_CONNECTION_HANDOFF_NONCE,
  instance_pid: process.pid, case_nonce: process.env.MYRK_CONNECTION_CASE_NONCE,
  authorization_sha256: process.env.MYRK_CONNECTION_AUTHORIZATION_SHA256 }
let results = 0
process.on('message', value => {
  if (value.type === 'owner_connection_result') {
    results++
    if (value.key !== 'synthetic-owner-key' || value.provider_config.model !== 'one') process.exitCode = 2
    if (${acknowledge}) process.send({ type: 'owner_connection_received', nonce: value.nonce, instance_pid: process.pid,
      owner_pid: process.ppid, case_nonce: request.case_nonce, authorization_sha256: request.authorization_sha256 })
    process.send(request)
  } else if (value.type === 'owner_connection_error') {
    process.send({ type: 'test_report', results, code: value.error_code,
      keyInEnvironment: process.env.MYRK_CONNECTION_KEY !== undefined || process.env.MYRK_QUEUE_CONNECTION_KEY !== undefined })
  }
})
const timer = setInterval(() => { if (existsSync(process.env.MYRK_TEST_NORMAL_EXIT_FLAG)) { clearInterval(timer); process.disconnect() } }, 20)
process.send(request)
`)
  const events: ServiceEvent[] = []
  const service = new PythonService({ python: process.env.MYRK_PYTHON!, root: resolve('..'), dataDirectory: join(directory, 'records'), module: 'tests.desktop_fixture' }, event => events.push(event))
  const provider = { name: 'Example', protocol: 'openai-chat' as const, base_url: 'https://example.test/v1', model: 'one' }
  const manager = new ConnectionManager(new ConnectionStore(join(directory, 'unused-store'), {
    isAsyncEncryptionAvailable: async () => { throw new Error('No native credential operation is permitted') },
    encryptStringAsync: async () => { throw new Error('No save is permitted') },
    decryptStringAsync: async () => { throw new Error('No decrypt is permitted') },
  }), service, event => events.push(event))
  const states: ConnectionHandoffState[] = []
  let verificationCount = 0
  let handoff: ReturnType<typeof startConnectionHandoff> | undefined
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 15000 }).toBe(true)
    await manager.configureTemporary({ provider_config: provider, key: 'synthetic-owner-key' })
    const options = { entry, env: { ...process.env, MYRK_TEST_NORMAL_EXIT_FLAG: exitFlag }, provider,
      caseNonce: (acknowledge ? '1' : '3').repeat(32), authorizationSha256: '2'.repeat(64), verify: () => { verificationCount++ },
      onState: (state: ConnectionHandoffState) => states.push(state) }
    handoff = startConnectionHandoff(manager, options)
    const reports: unknown[] = []
    handoff.child.on('message', value => { if (typeof value === 'object' && value !== null && 'type' in value && value.type === 'test_report') reports.push(value) })
    await expect.poll(() => reports, { timeout: 10000 }).toEqual([{ type: 'test_report', results: 1, code: 'connection_already_claimed', keyInEnvironment: false }])
    expect(states.map(state => state.stage)).toContain('sent')
    expect(states.some(state => state.stage === 'received')).toBe(acknowledge)
    if (acknowledge) expect(states.findIndex(state => state.stage === 'sent')).toBeLessThan(states.findIndex(state => state.stage === 'received'))
    expect(verificationCount).toBe(2)
    expect(JSON.stringify(states)).not.toContain('synthetic-owner-key')
    expect(await handoff.shutdown()).toBe(true)
    expect(await readFile(exitFlag, 'utf8')).toContain('Normal shutdown')
    await manager.consumeConnection(provider, async connection => { expect(connection.key).toBe('synthetic-owner-key') })
    expect(() => startConnectionHandoff(manager, options)).toThrow(expect.objectContaining({ code: 'case_already_claimed' }))
  } finally {
    if (handoff) await handoff.shutdown()
    await service.shutdown()
  }
}, 30000)

test('an acceptance child that already failed cannot block the owner from shutting down', async () => {
  const directory = await testDirectory('failed-child-shutdown-')
  const entry = join(directory, 'failed-child.mjs')
  await writeFile(entry, 'process.exit(7)\n')
  const events: ServiceEvent[] = []
  const service = new PythonService({ python: process.env.MYRK_PYTHON!, root: resolve('..'), dataDirectory: join(directory, 'records'), module: 'tests.desktop_fixture' }, event => events.push(event))
  const manager = new ConnectionManager(new ConnectionStore(join(directory, 'unused-store'), {
    isAsyncEncryptionAvailable: async () => { throw new Error('No credential loading is permitted') },
    encryptStringAsync: async () => { throw new Error('No save is permitted') },
    decryptStringAsync: async () => { throw new Error('No decrypt is permitted') },
  }), service, () => {})
  const states: ConnectionHandoffState[] = []
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 15000 }).toBe(true)
    const handoff = startConnectionHandoff(manager, { entry, env: process.env,
      provider: { protocol: 'openai-chat', base_url: 'https://example.test/v1', model: 'one' },
      caseNonce: '4'.repeat(32), authorizationSha256: '5'.repeat(64), verify: () => {},
      onState: state => states.push(state) })
    expect(await new Promise(accept => handoff.child.once('close', code => accept(code)))).toBe(7)
    expect(states).toContainEqual(expect.objectContaining({ stage: 'child_closed', error_code: 'child_failed' }))
    expect(await handoff.shutdown()).toBe(true)
  } finally { await service.shutdown() }
}, 20000)

test.each([
  ...['success', 'wrong_nonce', 'wrong_pid', 'wrong_case', 'wrong_authorization', 'missing_parent', 'owner_error', 'unsafe_owner_error'].map(scenario => ({ scenario, slowStart: false })),
  { scenario: 'success', slowStart: true },
])('the isolated Main installs only its bound parent connection: $scenario; slow start=$slowStart', async ({ scenario, slowStart }) => {
  const directory = await testDirectory('temporary-main-receiver-')
  await writeFile(join(directory, 'handoff_fixture.py'), `
import faulthandler
import runpy
import time
# Bound the real interpreter even when event-loop creation or shutdown stalls.
faulthandler.dump_traceback_later(24, exit=True)
try:
    time.sleep(${slowStart ? '4.2' : '0'})
    runpy.run_module('tests.desktop_fixture', run_name='__main__')
finally:
    faulthandler.cancel_dump_traceback_later()
`)
  const entry = join(directory, 'receiver-source.mjs')
  const bundled = join(directory, 'receiver.mjs')
  const source = (path: string) => JSON.stringify(resolve(path).replaceAll('\\', '/'))
  await writeFile(entry, `
import { installTemporaryConnectionReceiver } from ${source('src/main/connection-handoff.ts')}
import { ConnectionManager } from ${source('src/main/connection-manager.ts')}
import { ConnectionStore } from ${source('src/main/connection-store.ts')}
import { PythonService } from ${source('src/main/python-service.ts')}
const directory = ${JSON.stringify(directory)}
const started = performance.now()
const phase = stage => { if (process.connected) process.send({ type: 'receiver_stage', stage, elapsed_ms: Math.round(performance.now() - started) }) }
let finishing = false
let timer
phase('created')
const service = new PythonService({ python: process.env.MYRK_PYTHON, root: ${JSON.stringify(resolve('..'))},
  module: 'handoff_fixture', dataDirectory: directory + '/records' }, event => {
    if (event.event !== 'ready' || finishing) return
    phase('python_ready')
    clearTimeout(timer)
    timer = setTimeout(() => { phase('ipc_timeout'); void finish(undefined, true) }, 4000)
    start()
  })
const manager = new ConnectionManager(new ConnectionStore(directory + '/must-not-be-written', {
  isAsyncEncryptionAvailable: async () => { throw new Error('Native storage must not be used') },
  encryptStringAsync: async () => { throw new Error('Saving is forbidden') },
  decryptStringAsync: async () => { throw new Error('Decrypting is forbidden') },
}), service, () => {})
timer = setTimeout(() => { phase('startup_timeout'); void finish(undefined, true) }, 12000)
async function finish(result, timedOut = false) {
  if (finishing) return
  finishing = true
  clearTimeout(timer)
  if (result && process.connected) {
    const state = manager.state()
    process.send({ type: 'receiver_report', ...result, available: state.available.length, saved: state.saved.length })
  }
  const saved = await service.shutdown()
  if (timedOut || !saved) process.exitCode = 3
  if (process.connected) process.disconnect()
}
function start() {
  const uninstall = installTemporaryConnectionReceiver(manager, { parentPid: process.ppid + ${scenario === 'missing_parent' ? '1' : '0'},
    caseNonce: 'receiver-case', authorizationSha256: 'receiver-authorization' })
  process.once('myrk:temporary-connection', result => {
    uninstall()
    void finish(result)
  })
  process.emit('myrk:request-private-connection')
  process.emit('myrk:request-private-connection')
}
`)
  const build = await rolldown({ input: entry, platform: 'node', external: /^node:/ })
  try {
    const output = await build.generate({ format: 'esm' })
    await writeFile(bundled, output.output[0].code)
  } finally { await build.close() }
  const child = spawn(process.execPath, [bundled], {
    env: { ...process.env, PYTHONPATH: [directory, process.env.PYTHONPATH].filter(Boolean).join(delimiter) },
    windowsHide: true, stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
  })
  let stderr = ''
  child.stderr!.on('data', data => { stderr = (stderr + String(data)).slice(-8192) })
  const reports: unknown[] = []
  const stages: { stage: string; elapsed_ms: number }[] = []
  let requests = 0
  child.on('message', (value: unknown) => {
    if (typeof value !== 'object' || value === null || !('type' in value)) return
    if (value.type === 'receiver_report') reports.push(value)
    if (value.type === 'receiver_stage' && 'stage' in value && typeof value.stage === 'string'
      && 'elapsed_ms' in value && typeof value.elapsed_ms === 'number') {
      stages.push({ stage: value.stage, elapsed_ms: value.elapsed_ms })
    }
    if (value.type !== 'request_connection') return
    requests++
    const request = value as Record<string, unknown>
    child.send({ type: scenario.endsWith('owner_error') ? 'connection_error' : 'connection_result', nonce: scenario === 'wrong_nonce' ? 'wrong' : request.nonce,
      owner_pid: process.pid, child_pid: scenario === 'wrong_pid' ? child.pid! + 1 : child.pid,
      case_nonce: scenario === 'wrong_case' ? 'wrong' : request.case_nonce,
      authorization_sha256: scenario === 'wrong_authorization' ? 'wrong' : request.authorization_sha256,
      ...(scenario.endsWith('owner_error')
        ? { error_code: scenario === 'owner_error' ? 'connection_unavailable' : 'untrusted-synthetic-secret-message' }
        : { provider_config: { name: 'Example', protocol: 'openai-chat', base_url: 'https://example.test/v1', model: 'one' }, key: 'synthetic-receiver-key' }) })
  })
  const code = await new Promise<number | null>(accept => { child.once('close', code => accept(code)) })
  expect(code, JSON.stringify({ stages, stderr })).toBe(0)
  if (slowStart) expect(stages.find(stage => stage.stage === 'python_ready')!.elapsed_ms).toBeGreaterThan(4000)
  expect(reports).toEqual([{ type: 'receiver_report', configured: scenario === 'success',
    ...(scenario === 'success' ? {} : { error_code: scenario === 'missing_parent' ? 'owner_unavailable'
      : scenario === 'owner_error' ? 'connection_unavailable'
      : scenario === 'unsafe_owner_error' ? 'owner_connection_failed' : 'connection_binding_mismatch' }),
    available: scenario === 'success' ? 1 : 0, saved: 0 }])
  expect(requests).toBe(scenario === 'missing_parent' ? 0 : 1)
  expect(JSON.stringify(reports)).not.toContain('synthetic-receiver-key')
  expect(JSON.stringify(reports)).not.toContain('untrusted-synthetic-secret-message')
}, 30000)
