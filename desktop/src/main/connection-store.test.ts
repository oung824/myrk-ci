// @vitest-environment node
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { chmod, readFile, readdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import { ConnectionStore, type ConnectionStoreData } from './connection-store'

function encryption() {
  const key = randomBytes(32)
  return {
    isAsyncEncryptionAvailable: async () => true,
    encryptStringAsync: async (plaintext: string) => {
      const iv = randomBytes(12)
      const cipher = createCipheriv('aes-256-gcm', key, iv)
      return Buffer.concat([iv, cipher.update(plaintext, 'utf8'), cipher.final(), cipher.getAuthTag()])
    },
    decryptStringAsync: async (ciphertext: Buffer) => {
      const cipher = createDecipheriv('aes-256-gcm', key, ciphertext.subarray(0, 12))
      cipher.setAuthTag(ciphertext.subarray(-16))
      return { result: Buffer.concat([cipher.update(ciphertext.subarray(12, -16)), cipher.final()]).toString('utf8'), shouldReEncrypt: false }
    },
  }
}

const provider = { name: 'Example', base_url: 'https://example.test/v1', model: 'chat-example', protocol: 'openai-chat' as const }
const original: ConnectionStoreData = { version: 1, connections: [{ provider_config: provider, key: 'synthetic-store-secret' }], selected_provider: provider }

test('saved connections and the last selection survive a new store without plaintext on disk', async () => {
  const directory = await testDirectory('connection-store-')
  const file = join(directory, 'nested', 'connections.bin')
  const crypto = encryption()
  expect(await new ConnectionStore(file, crypto).load()).toEqual({ version: 1, connections: [], selected_provider: null })
  await new ConnectionStore(file, crypto).save(original)
  expect((await readFile(file)).includes(Buffer.from('synthetic-store-secret'))).toBe(false)
  expect(await new ConnectionStore(file, crypto).load()).toEqual(original)
})

test('unavailable encryption rejects a replacement and retains the previous encrypted connections', async () => {
  const directory = await testDirectory('connection-store-unavailable-')
  const file = join(directory, 'connections.bin')
  const crypto = encryption()
  await new ConnectionStore(file, crypto).save(original)
  const before = await readFile(file)
  const unavailable = { ...crypto, isAsyncEncryptionAvailable: async () => false }
  await expect(new ConnectionStore(file, unavailable).save({ ...original, connections: [] })).rejects.toThrow('unavailable')
  expect(await readFile(file)).toEqual(before)
  expect(await new ConnectionStore(file, crypto).load()).toEqual(original)
})

test('an encryption failure leaves the original file and no replacement temporary file', async () => {
  const directory = await testDirectory('connection-store-encryption-failure-')
  const file = join(directory, 'connections.bin')
  const crypto = encryption()
  await new ConnectionStore(file, crypto).save(original)
  const before = await readFile(file)
  const failed = { ...crypto, encryptStringAsync: async (): Promise<Buffer> => { throw new Error('Encryption failed') } }
  await expect(new ConnectionStore(file, failed).save({ version: 1, connections: [], selected_provider: provider })).rejects.toThrow('Encryption failed')
  expect(await readFile(file)).toEqual(before)
  expect(await readdir(directory)).toEqual(['connections.bin'])
})

test.runIf(process.platform === 'win32')('a failed disk replacement preserves the old password and removes the temporary ciphertext', async () => {
  const directory = await testDirectory('connection-store-write-failure-')
  const file = join(directory, 'connections.bin')
  const crypto = encryption()
  const store = new ConnectionStore(file, crypto)
  await store.save(original)
  const before = await readFile(file)
  await chmod(file, 0o444)
  try {
    await expect(store.save({ version: 1, connections: [], selected_provider: provider })).rejects.toThrow()
    expect(await readFile(file)).toEqual(before)
    expect(await store.load()).toEqual(original)
    expect(await readdir(directory)).toEqual(['connections.bin'])
  } finally {
    await chmod(file, 0o666)
  }
})

test('unreadable ciphertext and unsupported saved formats are retained for recovery', async () => {
  const directory = await testDirectory('connection-store-invalid-')
  const file = join(directory, 'connections.bin')
  const crypto = encryption()
  const store = new ConnectionStore(file, crypto)
  const invalid = Buffer.from('not a valid encrypted connection store')
  await writeFile(file, invalid)
  await expect(store.load()).rejects.toThrow()
  expect(await readFile(file)).toEqual(invalid)
  const unsupported = await crypto.encryptStringAsync(JSON.stringify({ ...original, version: 2 }))
  await writeFile(file, unsupported)
  await expect(store.load()).rejects.toThrow('Invalid saved connections')
  expect(await readFile(file)).toEqual(unsupported)
})
