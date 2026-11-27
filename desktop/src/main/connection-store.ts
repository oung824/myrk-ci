import { randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { decodeCommand } from '../shared/decode'
import type { ProviderConfig } from '../shared/protocol'

export interface ConnectionStoreData {
  version: 1
  connections: { provider_config: ProviderConfig; key: string }[]
  selected_provider: ProviderConfig | null
}

interface Encryption {
  isAsyncEncryptionAvailable(): Promise<boolean>
  encryptStringAsync(plaintext: string): Promise<Buffer>
  decryptStringAsync(ciphertext: Buffer): Promise<{ result: string; shouldReEncrypt: boolean }>
}

export class ConnectionStore {
  constructor(private readonly file: string, private readonly encryption: Encryption) {}

  async load(): Promise<ConnectionStoreData> {
    const ciphertext = await readFile(this.file).catch(error => {
      if (error.code === 'ENOENT') return null
      throw error
    })
    if (ciphertext === null) return { version: 1, connections: [], selected_provider: null }
    if (!await this.encryption.isAsyncEncryptionAvailable()) throw new Error('Local encryption is unavailable')
    const { result } = await this.encryption.decryptStringAsync(ciphertext)
    const data: unknown = JSON.parse(result)
    if (typeof data !== 'object' || data === null || !('version' in data) || data.version !== 1 || !('connections' in data) || !Array.isArray(data.connections) || !('selected_provider' in data)) throw new Error('Invalid saved connections')
    const connections = data.connections.map(value => {
      if (typeof value !== 'object' || value === null || !('provider_config' in value) || !('key' in value)) throw new Error('Invalid saved connection')
      const decoded = decodeCommand({ action: 'configure', provider_config: value.provider_config, key: value.key })
      if (decoded.action !== 'configure' || !decoded.provider_config || !decoded.key) throw new Error('Invalid saved connection')
      return { provider_config: decoded.provider_config, key: decoded.key }
    })
    const selected = data.selected_provider === null ? null : decodeCommand({ action: 'configure', provider_config: data.selected_provider })
    if (selected !== null && (selected.action !== 'configure' || !selected.provider_config)) throw new Error('Invalid saved selection')
    return { version: 1, connections, selected_provider: selected === null ? null : selected.provider_config! }
  }

  async save(data: ConnectionStoreData): Promise<void> {
    if (!await this.encryption.isAsyncEncryptionAvailable()) throw new Error('Local encryption is unavailable')
    const ciphertext = await this.encryption.encryptStringAsync(JSON.stringify(data))
    await mkdir(dirname(this.file), { recursive: true })
    const pending = `${this.file}.${randomUUID()}.tmp`
    try {
      await writeFile(pending, ciphertext, { flag: 'wx', mode: 0o600 })
      await rename(pending, this.file)
    } finally {
      await rm(pending, { force: true })
    }
  }
}
