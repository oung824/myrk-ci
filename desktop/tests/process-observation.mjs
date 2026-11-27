import { randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { appendFileSync, createWriteStream } from 'node:fs'
import { mkdir, readdir, readFile, rename, writeFile } from 'node:fs/promises'
import { delimiter, dirname, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { finished } from 'node:stream/promises'
import { fileURLToPath } from 'node:url'

const source = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

export class ProcessObservation {
  #pending = []
  #failure
  #closing = false
  #closed
  #child
  #commands
  #receipts = new Set()
  #errorsFinished
  report = { complete: false, processes: [] }

  static async start(directory) {
    await mkdir(directory, { recursive: true })
    const prefix = join(directory, `observer-${randomUUID()}`)
    const python = process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe'
    const argv = [python, '-B', '-m', 'tests.support.process_observation']
    const child = spawn(python, argv.slice(1), {
      cwd: source, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: [source, process.env.PYTHONPATH].filter(Boolean).join(delimiter) },
    })
    appendFileSync(`${prefix}-launch.jsonl`, JSON.stringify({ pid: child.pid, launcher_pid: process.pid, argv, cwd: source, at: new Date().toISOString() }) + '\n')
    const observer = new ProcessObservation(child, prefix)
    try {
      await observer.status()
    } catch (error) {
      observer.#closing = true
      child.stdin.end()
      await observer.#closed
      await observer.#errorsFinished
      throw error
    }
    return observer
  }

  constructor(child, prefix) {
    this.#child = child
    this.#commands = `${prefix}-commands.jsonl`
    const errors = createWriteStream(`${prefix}-stderr.txt`)
    this.#errorsFinished = finished(errors).then(() => {}, error => { this.#fail(error) })
    child.stderr.pipe(errors)
    const lines = createInterface({ input: child.stdout })
    lines.on('line', line => {
      appendFileSync(`${prefix}-reports.jsonl`, line + '\n')
      const waiting = this.#pending.shift()
      try {
        this.report = JSON.parse(line)
        waiting?.resolve(this.report)
      } catch (error) {
        waiting?.reject(error)
        this.#fail(error)
        child.stdin.end()
      }
    })
    child.once('error', error => this.#fail(error))
    child.stdin.on('error', error => this.#fail(error))
    this.#closed = new Promise(resolve => child.once('close', (code, signal) => {
      appendFileSync(`${prefix}-launch.jsonl`, JSON.stringify({ pid: child.pid, code, signal, at: new Date().toISOString() }) + '\n')
      lines.close()
      if (!this.#closing || code !== 0) this.#fail(new Error(`Process observer closed: code=${code}, signal=${signal}; diagnostics=${prefix}-stderr.txt`))
      resolve({ code, signal })
    }))
  }

  #fail(error) {
    appendFileSync(this.#commands, JSON.stringify({ event: 'client-error', message: error.message, code: error.code ?? null }) + '\n')
    this.#failure ??= error
    for (const waiting of this.#pending.splice(0)) waiting.reject(error)
  }

  #request(command) {
    if (this.#failure) return Promise.reject(this.#failure)
    appendFileSync(this.#commands, JSON.stringify(command) + '\n')
    return new Promise((resolve, reject) => {
      this.#pending.push({ resolve, reject })
      this.#child.stdin.write(JSON.stringify(command) + '\n')
    })
  }

  async capture(child, argv, cwd) {
    if (!child.pid || child.exitCode !== null || child.signalCode !== null) throw new Error('Directly launched program is not alive before identity capture')
    const first = await this.#request({ action: 'capture', pid: child.pid })
    const observation = first.processes.find(item => item.pid === child.pid)
    if (child.exitCode !== null || child.signalCode !== null) throw new Error('Directly launched program exited during identity capture')
    if (observation.identity?.created_filetime === null || !observation.identity?.image) return observation
    const identity = observation.identity
    const result = await this.#request({ action: 'capture', pid: child.pid, expected: identity,
      direct_spawn: { identity, launcher_pid: process.pid, argv, cwd, source: 'caller_spawn' } })
    if (child.exitCode !== null || child.signalCode !== null) throw new Error('Directly launched program exited while binding its spawn receipt')
    return result.processes.find(item => item.pid === child.pid)
  }

  async captureReceipts(directory) {
    for (const name of await readdir(directory)) {
      if (!name.startsWith('process-') || !name.endsWith('.json') || this.#receipts.has(join(directory, name))) continue
      const path = join(directory, name)
      const receipt = JSON.parse(await readFile(path, 'utf8'))
      if (receipt.version !== 1 || !Array.isArray(receipt.records)) throw new Error(`Unsupported fixture receipt: ${path}`)
      appendFileSync(this.#commands, JSON.stringify({ receipt: path, source: receipt.source }) + '\n')
      for (const command of receipt.records) await this.#request(command)
      const self = this.report.processes.find(item => item.pid === receipt.interpreter_pid)
      if (!self) throw new Error(`Receipt has no observed interpreter: ${path}`)
      await writeFile(`${path}.ack.pending`, JSON.stringify(self))
      await rename(`${path}.ack.pending`, `${path}.ack`)
      this.#receipts.add(path)
    }
    return this.report
  }

  status() { return this.#request({ action: 'status' }) }
  wait(timeout_ms) { return this.#request({ action: 'wait', timeout_ms }) }

  async close() {
    this.#closing = true
    let exit
    try {
      if (!this.#failure) await this.#request({ action: 'close' })
    } finally {
      this.#child.stdin.end()
      exit = await this.#closed
      await this.#errorsFinished
    }
    if (exit.code !== 0 || this.#failure) throw this.#failure ?? new Error('Process observer did not close normally')
    return this.report
  }
}
