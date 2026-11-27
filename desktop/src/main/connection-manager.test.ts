// @vitest-environment node
import { resolve, join } from 'node:path'
import { readFile, writeFile } from 'node:fs/promises'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import type { ServiceEvent } from '../shared/protocol'
import { ConnectionStore } from './connection-store'
import { ConnectionManager, type TemporaryConnection } from './connection-manager'
import { PythonService } from './python-service'

test('saved connections restore into a new backend without starting work or exposing secrets', async () => {
  const directory = await testDirectory('connection-manager-')
  const file = join(directory, 'connections.bin')
  const encryption = {
    isAsyncEncryptionAvailable: async () => true,
    encryptStringAsync: async (text: string) => Buffer.from(text),
    decryptStringAsync: async (data: Buffer) => ({ result: data.toString(), shouldReEncrypt: false }),
  }
  const start = async () => {
    const events: ServiceEvent[] = []
    const service = new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: join(directory, 'records'), module: 'tests.desktop_fixture' }, event => events.push(event))
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 15000 }).toBe(true)
    const manager = new ConnectionManager(new ConnectionStore(file, encryption), service, event => events.push(event))
    await manager.initialize()
    return { events, service, manager }
  }
  const first = await start()
  const capacity = { context_window_override: 128000, input_token_limit_override: 96000, output_token_reserve: 4096, context_safety_margin: 512, auto_compaction: false, compaction_trigger: 0.8, compaction_target: 0.55, compaction_recent: 0.2 }
  try {
    await first.manager.configure({ action: 'configure', key: 'synthetic-manager-secret', provider_config: { name: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'deepseek', ...capacity } })
    expect(first.manager.state()).toMatchObject({ available: [{ name: 'DeepSeek' }], saved: [{ name: 'DeepSeek' }], error: null })
  } finally { await first.service.shutdown() }
  const second = await start()
  try {
    expect(second.manager.state().saved).toHaveLength(1)
    expect(second.manager.state().saved[0]).toMatchObject(capacity)
    expect(second.events.some(event => event.event === 'started')).toBe(false)
    await second.manager.configure({ action: 'configure' })
    expect(second.events.at(-1)).toMatchObject({ event: 'configured', configured: true })
    expect(JSON.stringify(second.events)).not.toContain('synthetic-manager-secret')
    const provider = second.manager.state().saved[0]
    await second.manager.forget(provider)
    expect(second.manager.state()).toEqual({ available: [], saved: [], error: null })
    await expect(second.manager.configure({ action: 'configure' })).rejects.toThrow('连接密码')
  } finally { await second.service.shutdown() }
}, 40000)

test('failed replacement remains session-only and an unreadable store is preserved on manual setup', async () => {
  const directory = await testDirectory('connection-manager-failure-')
  const file = join(directory, 'connections.bin')
  let rejectEncryption = false
  const encryption = {
    isAsyncEncryptionAvailable: async () => true,
    encryptStringAsync: async (text: string) => {
      if (rejectEncryption) throw new Error('Controlled encryption failure')
      return Buffer.from(text)
    },
    decryptStringAsync: async (data: Buffer) => ({ result: data.toString(), shouldReEncrypt: false }),
  }
  const events: ServiceEvent[] = []
  const service = new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: join(directory, 'records'), module: 'tests.desktop_fixture' }, event => events.push(event))
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 15000 }).toBe(true)
    const manager = new ConnectionManager(new ConnectionStore(file, encryption), service, event => events.push(event))
    await manager.initialize()
    await manager.configure({ action: 'configure', key: 'original-synthetic-secret' })
    const original = await readFile(file)
    rejectEncryption = true
    await manager.configure({ action: 'configure', key: 'replacement-synthetic-secret' })
    expect(manager.state()).toMatchObject({ available: [{ name: 'DeepSeek' }], saved: [], error: expect.stringContaining('保存失败') })
    expect(await readFile(file)).toEqual(original)
    await expect(manager.forget(manager.state().available[0])).rejects.toThrow('忘记')
    expect(manager.state().available).toHaveLength(1)
    await writeFile(file, 'unreadable original store')
    rejectEncryption = false
    const unreadable = new ConnectionManager(new ConnectionStore(file, encryption), service, event => events.push(event))
    await unreadable.initialize()
    expect(unreadable.state().error).toContain('未能恢复')
    await unreadable.configure({ action: 'configure', key: 'manual-synthetic-secret' })
    expect(unreadable.state()).toMatchObject({ available: [{ name: 'DeepSeek' }], saved: [], error: expect.stringContaining('保存失败') })
    expect(await readFile(file, 'utf8')).toBe('unreadable original store')
    expect(JSON.stringify(events)).not.toContain('synthetic-secret')
  } finally { await service.shutdown() }
}, 30000)

test('a Main-only connection handoff borrows an isolated copy and a temporary connection never persists', async () => {
  const directory = await testDirectory('connection-handoff-manager-')
  const file = join(directory, 'connections.bin')
  let loads = 0
  let writes = 0
  const encryption = {
    isAsyncEncryptionAvailable: async () => true,
    encryptStringAsync: async (text: string) => { writes++; return Buffer.from(text) },
    decryptStringAsync: async (data: Buffer) => { loads++; return { result: data.toString(), shouldReEncrypt: false } },
  }
  const events: ServiceEvent[] = []
  const service = new PythonService({ python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'), dataDirectory: join(directory, 'records'), module: 'tests.desktop_fixture' }, event => events.push(event))
  const provider = { name: 'Owner', base_url: 'https://example.test/v1', model: 'one', protocol: 'openai-chat' as const, available_models: ['one', 'two'] }
  const original = { version: 1 as const, connections: [{ provider_config: provider, key: 'synthetic-handoff-secret' }], selected_provider: provider }
  const store = new ConnectionStore(file, encryption)
  await store.save(original)
  const initialBytes = await readFile(file)
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 15000 }).toBe(true)
    const owner = new ConnectionManager(store, service, event => events.push(event))
    await owner.initialize()
    let borrowed: TemporaryConnection | undefined
    await owner.consumeConnection(provider, async connection => {
      expect(connection.key).toBe('synthetic-handoff-secret')
      borrowed = connection
      connection.provider_config.model = 'changed-only-in-copy'
      connection.provider_config.available_models!.push('changed-only-in-copy')
    })
    expect(borrowed!.key).toBe('')
    await owner.consumeConnection(provider, async connection => {
      expect(connection.key).toBe('synthetic-handoff-secret')
      expect(connection.provider_config.available_models).toEqual(['one', 'two'])
    })
    const controlledFailure = new Error('Controlled consumer failure')
    await expect(owner.consumeConnection(provider, async connection => {
      borrowed = connection
      throw controlledFailure
    })).rejects.toBe(controlledFailure)
    expect(borrowed!.key).toBe('')
    await owner.consumeConnection(provider, async connection => { expect(connection.key).toBe('synthetic-handoff-secret') })
    await expect(owner.consumeConnection({ ...provider, model: 'other' }, async () => {})).rejects.toMatchObject({ code: 'connection_unavailable' })
    const temporary = new ConnectionManager(store, service, event => events.push(event))
    await temporary.configureTemporary(original.connections[0])
    expect(temporary.state()).toMatchObject({ available: [provider], saved: [], error: null })
    await temporary.consumeConnection(provider, async connection => { expect(connection.key).toBe('synthetic-handoff-secret') })
    await temporary.configureTemporary({ provider_config: { ...provider, name: 'Other matching connection' }, key: 'second-synthetic-key' })
    await expect(temporary.consumeConnection(provider, async () => {})).rejects.toMatchObject({ code: 'connection_ambiguous' })
    expect(loads).toBe(1)
    expect(writes).toBe(1)
    expect(await readFile(file)).toEqual(initialBytes)
    expect(JSON.stringify(events)).not.toContain('synthetic-handoff-secret')
  } finally { await service.shutdown() }
}, 30000)
