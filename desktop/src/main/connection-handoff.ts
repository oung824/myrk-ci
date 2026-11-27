import { spawn, type ChildProcess } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import type { EventEmitter } from 'node:events'
import { decodeCommand } from '../shared/decode'
import type { ConnectionManager, ConnectionTarget } from './connection-manager'

export interface ConnectionHandoffState {
  stage: string
  owner_pid: number
  child_pid?: number
  case_nonce: string
  authorization_sha256: string
  error_code?: string
}

export interface ConnectionHandoffOptions {
  entry: string
  env: NodeJS.ProcessEnv
  provider: ConnectionTarget
  caseNonce: string
  authorizationSha256: string
  verify(): void
  onState(state: ConnectionHandoffState): void
  stdout?: number | 'ignore'
  stderr?: number | 'ignore'
}

const claimedCases = new Set<string>()

export function startConnectionHandoff(manager: ConnectionManager, options: ConnectionHandoffOptions): { child: ChildProcess; shutdown(): Promise<boolean> } {
  options.verify()
  if (claimedCases.has(options.caseNonce)) throw Object.assign(new Error('This case already owns a connection handoff'), { code: 'case_already_claimed' })
  claimedCases.add(options.caseNonce)
  const nonce = randomUUID()
  const child = spawn(process.execPath, [options.entry], {
    cwd: dirname(options.entry), windowsHide: true,
    stdio: ['ignore', options.stdout ?? 'ignore', options.stderr ?? 'ignore', 'ipc'],
    env: { ...options.env, MYRK_CONNECTION_OWNER_PID: String(process.pid), MYRK_CONNECTION_HANDOFF_NONCE: nonce,
      MYRK_CONNECTION_CASE_NONCE: options.caseNonce, MYRK_CONNECTION_AUTHORIZATION_SHA256: options.authorizationSha256 },
  })
  const binding = { owner_pid: process.pid, child_pid: child.pid, nonce,
    case_nonce: options.caseNonce, authorization_sha256: options.authorizationSha256 }
  const publish = (stage: string, error_code?: string) => options.onState({ stage, owner_pid: process.pid,
    child_pid: child.pid, case_nonce: options.caseNonce, authorization_sha256: options.authorizationSha256,
    ...(error_code ? { error_code } : {}) })
  const send = (value: object) => new Promise<void>((resolve, reject) => {
    if (!child.connected) { reject(new Error('Child IPC is unavailable')); return }
    child.send(value, error => error ? reject(new Error('Child IPC send failed')) : resolve())
  })
  let claimed = false
  let sending = false
  let sent = false
  let received = false
  let exited = false
  const closed = new Promise<boolean>(resolve => {
    child.once('close', (code, signal) => {
      exited = true
      publish('child_closed', code === 0 && signal === null ? undefined : 'child_failed')
      resolve(true)
    })
  })
  child.once('error', () => publish('failed', 'child_start_failed'))
  child.on('message', async (value: unknown) => {
    if (typeof value !== 'object' || value === null || !('type' in value)
      || !['request_owner_connection', 'owner_connection_received'].includes(String(value.type))) return
    const message = value as Record<string, unknown>
    const matches = message.nonce === nonce && message.case_nonce === options.caseNonce
      && message.authorization_sha256 === options.authorizationSha256 && message.instance_pid === child.pid
      && (message.type !== 'owner_connection_received' || message.owner_pid === process.pid)
      && Object.keys(message).every(key => ['type', 'nonce', 'instance_pid', 'case_nonce', 'authorization_sha256', 'owner_pid'].includes(key))
    if (message.type === 'owner_connection_received') {
      if (matches && sending && !received) {
        received = true
        if (sent) publish('received')
      }
      return
    }
    let error_code: string | undefined
    if (!matches) error_code = 'connection_binding_mismatch'
    else if (claimed) error_code = 'connection_already_claimed'
    else claimed = true
    try {
      if (error_code) {
        publish('rejected', error_code)
        await send({ type: 'owner_connection_error', ...binding, error_code })
        return
      }
      try { options.verify() } catch { throw Object.assign(new Error('Connection authority was rejected'), { code: 'owner_authorization_rejected' }) }
      await manager.consumeConnection(options.provider, async connection => {
        const reply = { type: 'owner_connection_result', ...binding, provider_config: connection.provider_config, key: connection.key }
        sending = true
        publish('sending')
        try { await send(reply) } finally { reply.key = '' }
      })
      sent = true
      publish('sent')
      if (received) publish('received')
    } catch (error) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? error.code : undefined
      error_code = typeof code === 'string' && ['connection_unavailable', 'connection_ambiguous', 'owner_authorization_rejected'].includes(code)
        ? code : 'connection_handoff_failed'
      publish('failed', error_code)
      if (child.connected) await send({ type: 'owner_connection_error', ...binding, error_code }).catch(() => {})
    }
  })
  publish('waiting_request')
  return {
    child,
    async shutdown() {
      if (exited) return await closed
      const flag = options.env.MYRK_TEST_NORMAL_EXIT_FLAG
      if (!flag) return false
      try { await writeFile(flag, 'Normal shutdown requested by the connection owner.\n') } catch { publish('failed', 'normal_shutdown_failed'); return false }
      let timer: ReturnType<typeof setTimeout>
      const timeout = new Promise<boolean>(resolve => { timer = setTimeout(() => resolve(false), 30000) })
      const result = await Promise.race([closed, timeout])
      clearTimeout(timer!)
      return result
    },
  }
}

export function installTemporaryConnectionReceiver(manager: ConnectionManager, options: {
  parentPid: number
  caseNonce: string
  authorizationSha256: string
  onState?: (state: ConnectionHandoffState) => void
}): () => void {
  const localEvents: EventEmitter = process
  let requested = false
  let finished = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let nonce: string
  const publish = (stage: string, error_code?: string) => options.onState?.({ stage, owner_pid: options.parentPid,
    child_pid: process.pid, case_nonce: options.caseNonce, authorization_sha256: options.authorizationSha256,
    ...(error_code ? { error_code } : {}) })
  const cleanup = () => {
    clearTimeout(timer)
    process.off('myrk:request-private-connection', request)
    process.off('message', receive)
    process.off('disconnect', disconnected)
  }
  const complete = (configured: boolean, error_code?: string) => {
    if (finished) return
    finished = true
    cleanup()
    publish(configured ? 'configured' : 'failed', error_code)
    localEvents.emit('myrk:temporary-connection', { configured, ...(error_code ? { error_code } : {}) })
  }
  const disconnected = () => complete(false, 'owner_disconnected')
  const receive = async (value: unknown) => {
    if (finished || typeof value !== 'object' || value === null || !('type' in value)
      || !['connection_result', 'connection_error'].includes(String(value.type))) return
    const message = value as Record<string, unknown>
    if (message.nonce !== nonce || message.owner_pid !== options.parentPid || message.child_pid !== process.pid
      || message.case_nonce !== options.caseNonce || message.authorization_sha256 !== options.authorizationSha256
      || Object.keys(message).some(key => !['type', 'nonce', 'owner_pid', 'child_pid', 'case_nonce', 'authorization_sha256', 'provider_config', 'key', 'error_code'].includes(key))) {
      if ('key' in message) message.key = ''
      complete(false, 'connection_binding_mismatch')
      return
    }
    process.off('message', receive)
    if (message.type === 'connection_error') {
      const allowed = ['connection_unavailable', 'connection_ambiguous', 'owner_authorization_rejected',
        'owner_unavailable', 'owner_timeout', 'owner_binding_mismatch', 'owner_ack_failed']
      complete(false, typeof message.error_code === 'string' && allowed.includes(message.error_code) ? message.error_code : 'owner_connection_failed')
      return
    }
    let connection
    try {
      const command = decodeCommand({ action: 'configure', provider_config: message.provider_config, key: message.key })
      if (command.action !== 'configure' || !command.provider_config || !command.key) throw new Error('Invalid temporary connection')
      connection = { provider_config: command.provider_config, key: command.key }
      await manager.configureTemporary(connection)
      complete(true)
    } catch {
      complete(false, 'connection_configuration_failed')
    } finally {
      if (connection) connection.key = ''
      message.key = ''
    }
  }
  const request = () => {
    if (requested || finished) return
    requested = true
    if (typeof process.send !== 'function' || !process.connected || process.ppid !== options.parentPid) {
      complete(false, 'owner_unavailable')
      return
    }
    nonce = randomUUID()
    process.on('message', receive)
    process.once('disconnect', disconnected)
    timer = setTimeout(() => complete(false, 'owner_timeout'), 45000)
    publish('requesting')
    process.send({ type: 'request_connection', nonce, instance_pid: process.pid, host_pid: options.parentPid,
      case_nonce: options.caseNonce, authorization_sha256: options.authorizationSha256 }, error => {
      if (error) complete(false, 'owner_ipc_failed')
    })
  }
  process.on('myrk:request-private-connection', request)
  return cleanup
}
