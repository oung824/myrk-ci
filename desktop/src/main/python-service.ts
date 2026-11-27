import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { createInterface } from 'node:readline'
import { randomUUID } from 'node:crypto'
import { decodeEvent } from '../shared/decode'
import type { Command, ProviderConfig, ServiceEvent } from '../shared/protocol'

export type ConnectionRequest = Extract<Command, { action: 'configure' }> | {
  action: 'restore_connections'
  connections: { provider_config: ProviderConfig; key: string }[]
  selected_provider: ProviderConfig | null
} | Extract<Command, { action: 'forget_connection' }>

type ConfigurationReply = Extract<ServiceEvent, { event: 'configured' }>

interface ServiceOptions {
  python: string
  root: string
  module?: string
  dataDirectory?: string
}

export class PythonService {
  private child: ChildProcessWithoutNullStreams
  private closed: Promise<boolean>
  private shuttingDown: Promise<boolean> | undefined
  private blocked: ((saved: boolean) => void) | undefined
  private requests = new Map<string, { resolve: (event: ConfigurationReply) => void; reject: (error: Error) => void }>()
  exited = false

  constructor(options: ServiceOptions, receive: (event: ServiceEvent) => void) {
    this.child = spawn(options.python, ['-u', '-X', 'utf8', '-m', options.module ?? 'myrk.desktop_service'], {
      cwd: options.root,
      shell: false,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        ...(options.dataDirectory ? { MYRK_DATA_DIRECTORY: options.dataDirectory } : {}),
      },
    })
    this.child.stderr.resume()
    const lines = createInterface({ input: this.child.stdout })
    let spawned = false
    let acknowledged = false
    this.child.once('spawn', () => { spawned = true })
    lines.on('line', line => {
      try {
        const event = decodeEvent(line)
        const request = event.request_id ? this.requests.get(event.request_id) : undefined
        if (request && (event.event === 'configured' || event.event === 'error')) {
          if (event.event === 'error') request.reject(new Error(event.message))
          else request.resolve(event)
          return
        }
        if (event.event === 'closed') acknowledged = true
        if (event.event === 'shutdown_blocked') this.blocked?.(false)
        receive(event)
      } catch {
        receive({ event: 'error', message: '办事程序返回了无法识别的消息，请关闭后重新打开。', fatal: true })
      }
    })
    this.child.on('error', () => receive({ event: 'error', message: '无法启动办事程序，请检查开发环境和 Python（执行办事逻辑的程序）路径。', fatal: true }))
    this.child.stdin.on('error', () => receive({ event: 'error', message: '与办事程序的连接已中断，请关闭后重新打开。', fatal: true }))
    this.closed = new Promise(resolve => {
      this.child.on('close', code => {
        this.exited = true
        for (const request of this.requests.values()) request.reject(new Error('办事程序已退出，连接设置未完成。'))
        lines.close()
        if (spawned && (!this.shuttingDown || code !== 0)) receive({ event: 'error', message: '办事程序已退出，尚未保存的内容可能丢失，请关闭后重新打开。', fatal: true })
        resolve(!spawned || (code === 0 && acknowledged))
      })
    })
  }

  send(command: Command | (ConnectionRequest & { request_id: string })): Promise<void> {
    if (this.exited) return Promise.reject(new Error('办事程序已退出，请关闭后重新打开。'))
    return new Promise((resolve, reject) => {
      this.child.stdin.write(JSON.stringify(command) + '\n', error => {
        if (error) reject(new Error('未能发送操作，请检查办事程序是否仍在运行。'))
        else resolve()
      })
    })
  }

  request(command: ConnectionRequest): Promise<ConfigurationReply> {
    const request_id = randomUUID()
    return new Promise<ConfigurationReply>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('连接设置未收到确认，请重新打开软件后重试。')), 30000)
      this.requests.set(request_id, {
        resolve: event => { clearTimeout(timer); resolve(event) },
        reject: error => { clearTimeout(timer); reject(error) },
      })
      void this.send({ ...command, request_id }).catch(error => this.requests.get(request_id)?.reject(error))
    }).finally(() => { this.requests.delete(request_id) })
  }

  shutdown(): Promise<boolean> {
    if (!this.shuttingDown) {
      const blocked = new Promise<boolean>(resolve => { this.blocked = resolve })
      this.shuttingDown = (this.exited ? this.closed : this.send({ action: 'shutdown' }).then(() => Promise.race([this.closed, blocked]), () => this.closed)).then(saved => {
        if (!saved) this.shuttingDown = undefined
        return saved
      })
    }
    return this.shuttingDown
  }
}
