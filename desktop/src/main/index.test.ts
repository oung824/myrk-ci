// @vitest-environment node
import { join } from 'node:path'
import { mkdir, readFile, symlink, unlink, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { expect, test, vi } from 'vitest'
import type { ServiceEvent } from '../shared/protocol'
import type { ArtifactPreviewResult } from '../shared/artifact-preview'
import { testDirectory } from '../../tests/run-context.mjs'

const native = vi.hoisted(() => ({
  handlers: new Map<string, (event: { sender: object }, value?: unknown) => unknown>(),
  appEvents: new Map<string, (event: { preventDefault(): void }) => void>(),
  events: [] as ServiceEvent[],
  webContents: {
    send: (_channel: string, event: ServiceEvent) => { native.events.push(event) },
    setWindowOpenHandler: () => {}, on: () => {},
  },
  folder: '',
  imageSize: { width: 2, height: 2 },
  revealed: [] as string[],
}))

vi.mock('electron', () => ({
  app: { requestSingleInstanceLock: () => true, setPath: () => {}, getPath: () => native.folder, whenReady: async () => {}, on: (name: string, listener: (event: { preventDefault(): void }) => void) => { native.appEvents.set(name, listener) }, quit: () => {} },
  safeStorage: { isAsyncEncryptionAvailable: async () => false },
  Menu: { buildFromTemplate: () => ({}), setApplicationMenu: () => {} },
  nativeImage: { createFromBitmap: () => ({}), createFromBuffer: () => ({ getSize: () => native.imageSize }) },
  shell: { showItemInFolder: (path: string) => { native.revealed.push(path) } },
  Tray: class { setToolTip() {} setContextMenu() {} on() {} destroy() {} },
  BrowserWindow: class {
    webContents = native.webContents
    isDestroyed() { return false }
    on() {}
    close() {}
    async loadFile() {}
    async loadURL() {}
  },
  clipboard: { writeText: () => {} },
  dialog: { showOpenDialog: async () => ({ canceled: false, filePaths: [native.folder] }) },
  ipcMain: {
    on: (name: string, listener: (event: { sender: object }, value?: unknown) => unknown) => { native.handlers.set(name, listener) },
    handle: (name: string, listener: (event: { sender: object }, value?: unknown) => unknown) => { native.handlers.set(name, listener) },
  },
}))

test('a native folder selection belongs to its conversation and cannot authorize another conversation', async () => {
  native.folder = await testDirectory('native-scope-')
  vi.stubEnv('MYRK_SERVICE_MODULE', 'tests.desktop_fixture')
  vi.stubEnv('MYRK_DATA_DIRECTORY', join(native.folder, 'records'))
  await import('./index')
  await expect.poll(() => native.handlers.has('myrk:connect')).toBe(true)
  const source = { sender: native.webContents }
  native.handlers.get('myrk:connect')!(source)
  const operate = (command: unknown) => native.handlers.get('myrk:command')!(source, command)
  try {
    await expect.poll(() => native.events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    const original = native.events.find(event => event.event === 'ready')!.path!
    await expect(operate({ action: 'set_project_trust', root: native.folder, trusted: true })).rejects.toThrow()
    await native.handlers.get('myrk:select-workspace')!(source)
    const originalFolder = native.folder
    await operate({ action: 'set_project_trust', root: originalFolder, trusted: true })
    await expect.poll(() => native.events.some(event => event.event === 'project_trust' && event.root === originalFolder && event.saved)).toBe(true)
    native.folder = await testDirectory('native-pending-one-')
    await native.handlers.get('myrk:select-workspace')!(source)
    native.folder = await testDirectory('native-pending-two-')
    await native.handlers.get('myrk:select-workspace')!(source)
    await operate({ action: 'new' })
    await expect.poll(() => native.events.some(event => event.event === 'conversation')).toBe(true)
    const other = native.events.find(event => event.event === 'conversation')!.path!
    const scope = { root: originalFolder, allowed_actions: ['read'], provider: 'deepseek', recipients: ['deepseek'] }
    await expect(operate({ action: 'send', path: other, prompt: '不能挪用范围', scope })).rejects.toThrow()
    expect(native.events.filter(event => event.event === 'started')).toHaveLength(0)
    await operate({ action: 'configure', key: 'test-only' })
    await operate({ action: 'send', path: original, prompt: '原聊天获准使用范围', scope })
    await expect.poll(() => native.events.some(event => event.event === 'finished'), { timeout: 10000 }).toBe(true)
    expect(native.events.find(event => event.event === 'finished')!.path).toBe(original)
  } finally {
    native.appEvents.get('before-quit')!({ preventDefault() {} })
    await expect.poll(() => native.events.some(event => event.event === 'closed')).toBe(true)
    vi.unstubAllEnvs()
  }
}, 20000)

test('a delivered artifact opens a fixed current snapshot only in its owning active conversation', async () => {
  vi.resetModules()
  native.handlers.clear()
  native.events.length = 0
  native.folder = await testDirectory('native-artifact-')
  vi.stubEnv('MYRK_SERVICE_MODULE', 'tests.desktop_fixture')
  vi.stubEnv('MYRK_DATA_DIRECTORY', join(native.folder, 'records'))
  await import('./index')
  await expect.poll(() => native.handlers.has('myrk:connect')).toBe(true)
  const source = { sender: native.webContents }
  native.handlers.get('myrk:connect')!(source)
  const operate = (command: unknown) => native.handlers.get('myrk:command')!(source, command)
  try {
    await expect.poll(() => native.events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    const owner = native.events.find(event => event.event === 'ready')!.path!
    await native.handlers.get('myrk:select-workspace')!(source)
    await operate({ action: 'set_project_trust', root: native.folder, trusted: true })
    await operate({ action: 'configure', key: 'artifacts-controlled' })
    await operate({ action: 'send', path: owner, prompt: '制作并明确交付报告', scope: { root: native.folder, allowed_actions: ['read', 'edit'], provider: 'deepseek', recipients: ['deepseek'] } })
    await expect.poll(() => native.events.some(event => event.event === 'finished'), { timeout: 20000 }).toBe(true)
    const finished = native.events.find(event => event.event === 'finished')!
    if (finished.event !== 'finished') throw new Error('Expected completed turn')
    const artifact = finished.turn.artifacts![0]
    const request = { conversationId: owner, artifactId: artifact.id, requestId: 'first-open' }
    expect(native.handlers.has('myrk:open-artifact-preview')).toBe(true)
    let openNumber = 0
    const open = async (value: unknown, sender: { sender: object } = source) => await native.handlers.get('myrk:open-artifact-preview')!(sender, value && typeof value === 'object' ? { ...value, requestId: `open-${++openNumber}` } : value) as ArtifactPreviewResult
    const snapshot = await open(request)
    expect(snapshot).toMatchObject({ status: 'ready', deliveryState: 'unchanged', content: { kind: 'text', text: '实际交付正文\n', history: { status: 'created' } } })
    if (snapshot.status !== 'ready' || snapshot.content.kind !== 'text') throw new Error('Expected full text snapshot')
    await writeFile(artifact.path, '交付后的外部修改\n', 'utf8')
    expect(snapshot.content.text).toBe('实际交付正文\n')
    expect(await open(request)).toMatchObject({ status: 'ready', deliveryState: 'changed', content: { text: '交付后的外部修改\n' } })
    await expect(open(request, { sender: {} })).rejects.toThrow()
    await expect(open({ ...request, artifactId: 'not-an-artifact' })).rejects.toThrow()
    await expect(open({ ...request, path: artifact.path })).rejects.toThrow()
    const record = JSON.parse(await readFile(owner, 'utf8'))
    const savedTurn = record.turns[record.turns.length - 1]
    const savedAction = savedTurn.actions.find((action: { id: string }) => action.id === artifact.source_action_id)
    const change = savedAction.file_changes[0]
    const currentBytes = Buffer.from('可读的当前全文\n')
    const backup = join(native.folder, 'original-report.bin')
    const invalidBefore = Buffer.from([0xff, 0x00, 0x80])
    await writeFile(artifact.path, currentBytes)
    await writeFile(backup, invalidBefore)
    change.change_kind = savedTurn.artifacts[0].change_kind = 'modified'
    change.backup_path = backup
    change.before_sha256 = createHash('sha256').update(invalidBefore).digest('hex')
    change.after_sha256 = savedTurn.artifacts[0].delivered_sha256 = createHash('sha256').update(currentBytes).digest('hex')
    const restored = join(native.folder, 'records', 'historical-preview.json')
    await writeFile(restored, JSON.stringify(record), 'utf8')
    const count = native.events.filter(event => event.event === 'conversation').length
    await operate({ action: 'open', path: restored })
    await expect.poll(() => native.events.filter(event => event.event === 'conversation').length).toBe(count + 1)
    request.conversationId = restored
    expect(await open(request)).toMatchObject({ status: 'ready', content: { text: '可读的当前全文\n', history: { status: 'unavailable' } } })
    const original = Buffer.from('真实修改前全文\n')
    await writeFile(backup, original)
    change.before_sha256 = createHash('sha256').update(original).digest('hex')
    const reliableRecord = join(native.folder, 'records', 'reliable-preview.json')
    await writeFile(reliableRecord, JSON.stringify(record), 'utf8')
    await operate({ action: 'open', path: reliableRecord })
    await expect.poll(() => native.events.some(event => event.event === 'conversation' && event.path === reliableRecord)).toBe(true)
    request.conversationId = reliableRecord
    expect(await open(request)).toMatchObject({ status: 'ready', content: { history: { status: 'available', actionId: artifact.source_action_id, beforeText: '真实修改前全文\n', afterText: '可读的当前全文\n' } } })
    await writeFile(backup, '外部损坏历史')
    expect(await open(request)).toMatchObject({ status: 'ready', content: { text: '可读的当前全文\n', history: { status: 'unavailable' } } })
    await writeFile(artifact.path, Buffer.alloc(5 * 1024 * 1024 + 1, 65))
    expect(await open(request)).toMatchObject({ status: 'too_large' })
    await unlink(artifact.path)
    expect(await open(request)).toMatchObject({ status: 'missing' })
    let recordNumber = 0
    async function loadSavedTarget(path: string, name: string): Promise<void> {
      savedTurn.artifacts[0].path = change.path = path
      savedTurn.artifacts[0].name = name
      const savedPath = join(native.folder, 'records', `target-${++recordNumber}.json`)
      await writeFile(savedPath, JSON.stringify(record), 'utf8')
      await operate({ action: 'open', path: savedPath })
      await expect.poll(() => native.events.some(event => event.event === 'conversation' && event.path === savedPath)).toBe(true)
      request.conversationId = savedPath
    }
    const imagePath = join(native.folder, 'image.png')
    await writeFile(imagePath, Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVQImWP8z8Dwn4GBgYGJAQoAHgQCAfGvK/0AAAAASUVORK5CYII=', 'base64'))
    await loadSavedTarget(imagePath, 'image.png')
    expect(await open(request)).toMatchObject({ status: 'ready', content: { kind: 'image', width: 2, height: 2, mimeType: 'image/png' } })
    native.imageSize = { width: 0, height: 0 }
    expect(await open(request)).toMatchObject({ status: 'unreadable' })
    native.imageSize = { width: 2, height: 2 }
    const oversizedPng = Buffer.alloc(24)
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).copy(oversizedPng)
    oversizedPng.writeUInt32BE(10000, 16); oversizedPng.writeUInt32BE(10000, 20)
    await writeFile(imagePath, oversizedPng)
    expect(await open(request)).toMatchObject({ status: 'too_large' })
    const unknown = join(native.folder, 'legacy.doc')
    await writeFile(unknown, 'legacy')
    await loadSavedTarget(unknown, 'legacy.doc')
    expect(await open(request)).toMatchObject({ status: 'unsupported' })
    await native.handlers.get('myrk:reveal-artifact')!(source, { conversationId: request.conversationId, artifactId: artifact.id })
    expect(native.revealed).toContain(unknown)
    await unlink(unknown)
    expect(await open(request)).toMatchObject({ status: 'missing' })
    const outsideDirectory = await testDirectory('outside-artifact-scope-')
    const outside = join(outsideDirectory, 'outside.txt')
    await writeFile(outside, 'outside')
    await loadSavedTarget(outside, 'outside.txt')
    await expect(open(request)).rejects.toThrow('范围')
    const link = join(native.folder, 'linked')
    await symlink(outsideDirectory, link, 'junction')
    await loadSavedTarget(join(link, 'outside.txt'), 'outside.txt')
    expect(await open(request)).toMatchObject({ status: 'unreadable' })
    const directory = join(native.folder, 'directory.txt')
    await mkdir(directory)
    await loadSavedTarget(directory, 'directory.txt')
    expect(await open(request)).toMatchObject({ status: 'unreadable' })
    await native.handlers.get('myrk:close-artifact-preview')!(source, { snapshotId: snapshot.snapshotId })
    const beforeNew = native.events.filter(event => event.event === 'conversation').length
    await operate({ action: 'new' })
    await expect.poll(() => native.events.filter(event => event.event === 'conversation').length).toBe(beforeNew + 1)
    await expect(open(request)).rejects.toThrow()
  } finally {
    native.appEvents.get('before-quit')!({ preventDefault() {} })
    await expect.poll(() => native.events.some(event => event.event === 'closed')).toBe(true)
    vi.unstubAllEnvs()
  }
}, 40000)
