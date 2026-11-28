import { spawn } from 'node:child_process'
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { extname, isAbsolute, join, relative } from 'node:path'
import workerScript from '../../scripts/office-preview-worker.ps1?raw'

export const OFFICE_PREVIEW_CONVERSION_TIMEOUT_MS = 90_000
export const OFFICE_PREVIEW_CLEANUP_TIMEOUT_MS = 15_000

export type OfficePreview = {
  engine: 'microsoft-office'
  sourceSha256: string
  sections: { id: string; label: string; pdfPath: string }[]
  close(): Promise<void>
}

export class OfficePreviewCleanupError extends Error {
  constructor(readonly retainedDirectory: string, reason: string) {
    super(`Office preview cleanup failed; retained ${retainedDirectory}: ${reason}`)
  }
}

export async function openOfficePreview(options: { sourcePath: string; directory: string; signal?: AbortSignal }): Promise<OfficePreview> {
  if (!['.docx', '.xlsx', '.pptx'].includes(extname(options.sourcePath).toLowerCase())) {
    throw new Error('Office preview supports only .docx, .xlsx and .pptx')
  }
  if (process.platform !== 'win32') throw new Error('Local Microsoft Office preview requires Windows')
  if (!isAbsolute(options.sourcePath) || !isAbsolute(options.directory)) throw new Error('Office preview requires absolute snapshot paths')
  const sourceWithin = relative(options.directory, options.sourcePath)
  if (!sourceWithin.startsWith('..') && !isAbsolute(sourceWithin)) throw new Error('Office source snapshot must be outside the conversion directory')
  if (options.signal?.aborted) throw new Error('Office preview cancelled')
  await mkdir(options.directory)
  await mkdir(join(options.directory, 'temp'))
  await mkdir(join(options.directory, 'profile', 'roaming'), { recursive: true })
  await mkdir(join(options.directory, 'profile', 'local'))
  const script = join(options.directory, 'worker.ps1')
  await writeFile(script, workerScript)
  const environment = {
    ...process.env, TEMP: join(options.directory, 'temp'), TMP: join(options.directory, 'temp'), TMPDIR: join(options.directory, 'temp'),
    APPDATA: join(options.directory, 'profile', 'roaming'), LOCALAPPDATA: join(options.directory, 'profile', 'local'),
  }
  const executable = 'C:\\Program Files\\PowerShell\\7\\pwsh.exe'
  let cancelled = false
  let timedOut = false
  let cancelWrite: Promise<void> = Promise.resolve()
  const run = (mode: 'convert' | 'cleanup') => new Promise<void>((resolveRun, rejectRun) => {
    const child = spawn(executable, ['-NoLogo', '-NoProfile', '-NonInteractive', '-STA', '-File', script, '-Directory', options.directory, '-SourcePath', options.sourcePath, '-Mode', mode], { cwd: options.directory, env: environment, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    let error = ''
    child.stdout.on('data', data => { error += String(data) })
    child.stderr.on('data', data => { error += String(data) })
    const abort = () => {
      cancelled = true
      cancelWrite = writeFile(join(options.directory, 'cancel.flag'), '').catch(() => {})
    }
    if (mode === 'convert') {
      options.signal?.addEventListener('abort', abort, { once: true })
      if (options.signal?.aborted) abort()
    }
    const timeoutMs = mode === 'convert' ? OFFICE_PREVIEW_CONVERSION_TIMEOUT_MS : OFFICE_PREVIEW_CLEANUP_TIMEOUT_MS
    const timeout = setTimeout(() => { timedOut = true; child.kill() }, timeoutMs)
    child.once('error', error => rejectRun(mode === 'cleanup' ? new OfficePreviewCleanupError(options.directory, String(error)) : error))
    child.once('close', code => {
      clearTimeout(timeout)
      options.signal?.removeEventListener('abort', abort)
      if (mode === 'cleanup' && code !== 0) rejectRun(new OfficePreviewCleanupError(options.directory, error.trim()))
      else resolveRun()
    })
  })
  let launchError = ''
  await run('convert').catch(error => { launchError = String(error) })
  await cancelWrite
  await run('cleanup')
  const result = await readFile(join(options.directory, 'result.json'), 'utf8').then(text => JSON.parse(text) as { status: string; error?: string; engine: 'microsoft-office'; sourceSha256: string; sections: OfficePreview['sections'] }).catch(error => ({ status: 'failed', error: String(error), engine: 'microsoft-office' as const, sourceSha256: '', sections: [] }))
  if (cancelled || timedOut || launchError || result.status !== 'converted') {
    await rm(options.directory, { recursive: true, maxRetries: 5, retryDelay: 100 }).catch(error => {
      throw new OfficePreviewCleanupError(options.directory, `${result.error ?? launchError}; ${String(error)}`)
    })
    throw new Error(cancelled ? 'Office preview cancelled' : timedOut ? 'Office preview timed out' : result.error || launchError || 'Office preview conversion failed')
  }
  return { engine: result.engine, sourceSha256: result.sourceSha256, sections: result.sections, close: () => rm(options.directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }) }
}
