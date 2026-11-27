import type { Command, ConnectionStorageState, ProviderConfig, ServiceEvent } from '../shared/protocol'
import { ConnectionStore, type ConnectionStoreData } from './connection-store'
import { PythonService } from './python-service'
import { connectionIdentity as identity } from '../shared/connection'

export type ConnectionTarget = Pick<ProviderConfig, 'protocol' | 'base_url' | 'model'>
export type TemporaryConnection = ConnectionStoreData['connections'][number]

export class ConnectionManager {
  private saved: ConnectionStoreData = { version: 1, connections: [], selected_provider: null }
  private available = new Map<string, { provider_config: ProviderConfig; key: string }>()
  private unreadable = false
  private error: string | null = null

  constructor(private store: ConnectionStore, private service: PythonService, private receive: (event: ServiceEvent) => void) {}

  async initialize() {
    try {
      this.saved = await this.store.load()
    } catch {
      this.unreadable = true
      this.error = '已保存的连接密码未能恢复，原资料已保留。可重新填写供本次使用。'
    }
    const reply = await this.service.request({ action: 'restore_connections', connections: this.saved.connections, selected_provider: this.saved.selected_provider })
    for (const entry of this.saved.connections) this.available.set(identity(entry.provider_config), entry)
    this.publish()
    return reply
  }

  state(): ConnectionStorageState {
    return {
      available: [...this.available.values()].map(entry => entry.provider_config),
      saved: this.saved.connections.filter(entry => this.available.get(identity(entry.provider_config))?.key === entry.key).map(entry => entry.provider_config),
      error: this.error,
    }
  }

  private publish(): void {
    this.receive({ event: 'connection_storage', ...this.state() })
  }

  async consumeConnection<T>(provider: ConnectionTarget, consume: (connection: TemporaryConnection) => Promise<T>): Promise<T> {
    const matches = [...this.available.values()].filter(entry => entry.provider_config.protocol === provider.protocol
      && entry.provider_config.base_url === provider.base_url && entry.provider_config.model === provider.model)
    if (matches.length !== 1) throw Object.assign(new Error('Connection handoff is unavailable'), {
      code: matches.length ? 'connection_ambiguous' : 'connection_unavailable',
    })
    const connection = structuredClone(matches[0])
    try { return await consume(connection) }
    finally { connection.key = '' }
  }

  async configureTemporary(connection: TemporaryConnection): Promise<void> {
    const reply = await this.service.request({ action: 'configure', key: connection.key,
      provider_config: connection.provider_config })
    if (!reply.configured || !reply.provider_config) throw Object.assign(new Error('Temporary connection was not configured'), { code: 'connection_configuration_failed' })
    this.available.set(identity(reply.provider_config), { provider_config: structuredClone(reply.provider_config), key: connection.key })
    this.publish()
    this.receive(reply)
  }

  async configure(command: Extract<Command, { action: 'configure' }>): Promise<void> {
    const reply = await this.service.request(command)
    const provider = reply.provider_config
    if (provider && reply.configured) {
      const previous = this.available.get(identity(provider))
      const key = command.key ?? previous?.key
      if (key) {
        this.available.set(identity(provider), { provider_config: provider, key })
        const candidate: ConnectionStoreData = { version: 1, connections: [...this.available.values()], selected_provider: provider }
        try {
          if (this.unreadable) throw new Error('Unreadable credential store')
          await this.store.save(candidate)
          this.saved = candidate
          this.error = null
        } catch {
          this.error = '本次已设置，但保存失败，重启后需重新填写。原保存资料已保留。'
        }
      }
    }
    this.publish()
    this.receive(reply)
  }

  async forget(provider: ProviderConfig): Promise<void> {
    const connection = identity(provider)
    try {
      if (this.unreadable) throw new Error('Unreadable credential store')
      const candidate = { ...this.saved, connections: this.saved.connections.filter(entry => identity(entry.provider_config) !== connection) }
      await this.store.save(candidate)
      this.saved = candidate
      await this.service.request({ action: 'forget_connection', provider_config: provider })
      this.available.delete(connection)
      this.error = null
    } catch {
      this.error = '未能完整忘记此连接密码，请重试；当前状态如下。'
      this.publish()
      throw new Error(this.error)
    }
    this.publish()
    await this.service.send({ action: 'snapshot' })
  }
}
