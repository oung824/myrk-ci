import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, safeStorage, shell, Tray } from 'electron'
import { dirname, extname, isAbsolute, resolve } from 'node:path'
import { realpath, stat } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'
import { decodeCommand } from '../shared/decode'
import type { ServiceEvent } from '../shared/protocol'
import { PythonService } from './python-service'
import { ConnectionStore } from './connection-store'
import { ConnectionManager } from './connection-manager'
import { attachConnectionOwner } from './connection-owner-bootstrap'
import { installTemporaryConnectionReceiver } from './connection-handoff'
import { ArtifactPreviews } from './artifact-preview'
import { prepareDocumentPreview } from './document-preview'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = resolve(here, '../../..')
let window: BrowserWindow | undefined
let service: PythonService | undefined
let connected = false
let allowClose = false
let quitting = false
let tray: Tray | undefined
let connectionHandoff: Awaited<ReturnType<typeof attachConnectionOwner>> | undefined
const runningPaths = new Set<string>()
let activePath = ''
const selectedDocuments = new Map<string, string>()
const selectedWorkspaces = new Map<string, Set<string>>()
const pending: ServiceEvent[] = []
const recordedFiles = new Set<string>()
const commonProjectRoot = dirname(execFileSync('git', ['rev-parse', '--path-format=absolute', '--git-common-dir'], { cwd: projectRoot, windowsHide: true, encoding: 'utf8' }).trim())
const previews = new ArtifactPreviews({
  runtimeRoot: process.env.MYRK_RUNTIME_ROOT ?? resolve(commonProjectRoot, '.scratch/runtime'),
  projectRoot: commonProjectRoot,
  imageSize: async (bytes, mimeType) => {
    if (mimeType === 'image/png' || mimeType === 'image/jpeg') return nativeImage.createFromBuffer(bytes).getSize()
    const owner = window
    if (!owner || owner.isDestroyed()) throw new Error('图片预览窗口已关闭。')
    const size: unknown = await owner.webContents.executeJavaScriptInIsolatedWorld(1001, [{ code: `(() => {
      const image = new Image();
      image.src = ${JSON.stringify(`data:${mimeType};base64,${bytes.toString('base64')}`)};
      return image.decode().then(() => ({width:image.naturalWidth,height:image.naturalHeight})).finally(() => image.removeAttribute('src'));
    })()` }]).catch(() => { throw new Error('图片无法完整解码。') })
    if (!size || typeof size !== 'object' || !('width' in size) || !('height' in size) || typeof size.width !== 'number' || typeof size.height !== 'number' || !Number.isInteger(size.width) || !Number.isInteger(size.height)) throw new Error('图片无法完整解码。')
    return { width: size.width, height: size.height }
  },
  prepareDocument: prepareDocumentPreview,
})
let previewCleanup: Promise<void> = Promise.resolve()

if (process.env.MYRK_USER_DATA_DIRECTORY) app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
const primary = app.requestSingleInstanceLock()
if (!primary) app.quit()

async function openWindow(): Promise<void> {
  if (window && !window.isDestroyed()) {
    if (process.env.MYRK_BACKGROUND_TEST !== '1') { window.show(); window.focus() }
    return
  }
  connected = false
  window = new BrowserWindow({
    title: 'Myrk 开发版', width: 1180, height: 830, minWidth: 680, minHeight: 540,
    show: process.env.MYRK_BACKGROUND_TEST !== '1',
    backgroundColor: '#ffffff', autoHideMenuBar: true,
    webPreferences: {
      preload: resolve(here, '../preload/index.cjs'),
      sandbox: true, contextIsolation: true, nodeIntegration: false,
      offscreen: process.env.MYRK_BACKGROUND_TEST === '1',
    },
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', event => event.preventDefault())
  window.webContents.on('did-start-loading', () => { connected = false })
  window.on('closed', () => {
    connected = false; window = undefined; pending.length = 0
    previewCleanup = previews.clear()
    void previewCleanup.catch(() => receive({ event: 'error', message: '预览资源未能释放，请重新打开窗口后重试退出。' }))
  })
  if (process.env.ELECTRON_RENDERER_URL) await window.loadURL(process.env.ELECTRON_RENDERER_URL)
  else await window.loadFile(resolve(here, '../renderer/index.html'))
}

async function quit(): Promise<void> {
  if (quitting) return
  quitting = true
  await previewCleanup.catch(() => {})
  previewCleanup = previews.clear()
  const previewError = await previewCleanup.then(() => '', error => String(error))
  if (previewError) {
    quitting = false
    receive({ event: 'shutdown_blocked', message: '预览资源尚未释放，请稍后重试退出。' })
    return
  }
  if (connectionHandoff && !await connectionHandoff.shutdown()) {
    quitting = false
    receive({ event: 'shutdown_blocked', message: '验收子进程尚未退出，请等待其收尾后重试。' })
    return
  }
  if (!await service!.shutdown()) {
    quitting = false
    await openWindow()
    receive({ event: 'shutdown_blocked', message: '退出未完成：部分进展未能保存或办事程序异常结束；已有内容仍保留。请检查记录位置后，从右下角通知区域的 Myrk（桌面助理）图标菜单重试退出。' })
    return
  }
  allowClose = true
  tray?.destroy()
  app.quit()
}

function receive(event: ServiceEvent): void {
  const nextPath = event.event === 'ready' || event.event === 'conversation' ? event.path : event.event === 'snapshot' ? event.active_path : activePath
  if (nextPath !== activePath) {
    previewCleanup = previews.clear()
    void previewCleanup.catch(() => receive({ event: 'error', message: '预览资源未能释放，请重试关闭预览或退出。' }))
  }
  previews.observe(event)
  const actions = event.event === 'action' ? [event.action] : event.event === 'conversation' ? event.turns.flatMap(turn => turn.actions ?? []) : event.event === 'snapshot' ? event.conversations.flatMap(conversation => conversation.turns.flatMap(turn => turn.actions ?? [])) : event.event === 'finished' ? event.turn.actions ?? [] : []
  for (const action of actions) {
    if (action.status === 'completed' && ['create', 'write', 'edit', 'read', 'calculate'].includes(action.kind) && /^[A-Za-z]:[\\/]/.test(action.target)) recordedFiles.add(resolve(action.target))
    for (const path of [action.backup_path, action.full_output_path]) if (path && /^[A-Za-z]:[\\/]/.test(path)) recordedFiles.add(resolve(path))
  }
  if (event.event === 'ready' || event.event === 'conversation') activePath = event.path
  if (event.event === 'snapshot') activePath = event.active_path
  if (event.event === 'started') runningPaths.add(event.path ?? activePath)
  if (event.event === 'finished') runningPaths.delete(event.path)
  if (event.event === 'closed') runningPaths.clear()
  if (connected && window && !window.isDestroyed()) window.webContents.send('myrk:event', event)
  else pending.push(event)
}

if (primary) void app.whenReady().then(async () => {
let connections: ConnectionManager
let connectionOperation: Promise<void> = Promise.resolve()
let completeInitialization: () => void
let initializationFailed = false
const initializeConnections = new Promise<void>(resolve => { completeInitialization = resolve })
service = new PythonService({
  root: projectRoot,
  python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe',
  module: process.env.MYRK_SERVICE_MODULE,
  dataDirectory: process.env.MYRK_DATA_DIRECTORY,
}, event => {
  if (event.event === 'ready') {
    void connections.initialize().then(async reply => {
      if (process.env.MYRK_CONNECTION_RECEIVER === '1') {
        installTemporaryConnectionReceiver(connections, {
          parentPid: Number(process.env.MYRK_QUEUE_HOST_PID),
          caseNonce: process.env.MYRK_CONNECTION_CASE_NONCE!,
          authorizationSha256: process.env.MYRK_CONNECTION_AUTHORIZATION_SHA256!,
        })
      }
      if (process.env.MYRK_CONNECTION_OWNER_CASE) {
        connectionHandoff = await attachConnectionOwner(connections,
          process.env.MYRK_CONNECTION_PROJECT_ROOT ?? projectRoot, process.env.MYRK_CONNECTION_OWNER_CASE)
      }
      receive(event)
      receive(reply)
      completeInitialization()
    }).catch(() => {
      initializationFailed = true
      receive({ event: 'error', message: '连接恢复未完成，请重新打开软件。', fatal: true })
      completeInitialization()
    })
  } else {
    if (event.event === 'error' && event.fatal) { initializationFailed = true; completeInitialization() }
    receive(event)
  }
})
connections = new ConnectionManager(new ConnectionStore(resolve(app.getPath('userData'), 'connections.enc'), safeStorage), service, receive)

const menu = Menu.buildFromTemplate([
  { id: 'myrk-open', label: '打开 Myrk', click: () => { void openWindow() } },
  { id: 'myrk-quit', label: '退出 Myrk（停止工作并保存）', click: () => { void quit() } },
])
Menu.setApplicationMenu(menu)
const pixels = Buffer.alloc(16 * 16 * 4)
for (let y = 2; y < 14; y++) for (let x = 2; x < 14; x++) {
  if (x < 5 || x > 10 || y < 9 && Math.abs(x - 7.5) < (9 - y) / 2) {
    const offset = (y * 16 + x) * 4
    pixels.set([50, 50, 50, 255], offset)
  }
}
tray = new Tray(nativeImage.createFromBitmap(pixels, { width: 16, height: 16 }))
tray.setToolTip('Myrk · 关闭窗口后继续工作')
tray.setContextMenu(menu)
tray.on('double-click', () => { void openWindow() })
app.on('activate', () => { void openWindow() })
app.on('second-instance', () => { void openWindow() })

ipcMain.on('myrk:connect', event => {
  if (event.sender !== window?.webContents) return
  connected = true
  for (const message of pending.splice(0)) window.webContents.send('myrk:event', message)
})
ipcMain.handle('myrk:command', async (event, value: unknown) => {
  if (event.sender !== window?.webContents) throw new Error('无法识别操作来源。')
  if (quitting) throw new Error('软件正在退出，等待工作停止并保存。')
  try {
    const command = decodeCommand(value)
    await initializeConnections
    if (initializationFailed) throw new Error('连接恢复未完成。')
    if (command.action === 'get_connection_storage') {
      receive({ event: 'connection_storage', ...connections.state() })
      return
    }
    if (command.action === 'configure' || command.action === 'forget_connection') {
      const operation = connectionOperation.then(() => command.action === 'configure' ? connections.configure(command) : connections.forget(command.provider_config))
      connectionOperation = operation.catch(() => {})
      await operation
      return
    }
    if (command.action === 'send' && command.document !== undefined && command.document !== selectedDocuments.get(command.path ?? activePath)) {
      throw new Error('请选择本次允许读取的资料。')
    }
    if (command.action === 'send' && command.scope !== undefined && !selectedWorkspaces.get(command.path ?? activePath)?.has(command.scope.root)) {
      throw new Error('请选择本次允许使用的任务文件夹。')
    }
    if (command.action === 'set_project_trust' && !selectedWorkspaces.get(activePath)?.has(command.root)) {
      throw new Error('请选择本次需要确认的项目文件夹。')
    }
    await service!.send(command)
  } catch {
    throw new Error('未能完成操作，请检查输入或重新打开软件。')
  }
})
ipcMain.handle('myrk:quit', event => {
  if (event.sender !== window?.webContents) throw new Error('无法识别操作来源。')
  void quit()
})
ipcMain.handle('myrk:copy', (event, value: unknown) => {
  if (event.sender !== window?.webContents || typeof value !== 'string') throw new Error('无法复制这些内容。')
  clipboard.writeText(value)
})
ipcMain.handle('myrk:open-artifact-preview', (event, value: unknown) => {
  if (event.sender !== window?.webContents || quitting) throw new Error('无法识别操作来源。')
  return previews.open(value)
})
ipcMain.handle('myrk:close-artifact-preview', (event, value: unknown) => {
  if (event.sender !== window?.webContents) throw new Error('无法识别操作来源。')
  return previews.close(value).catch(error => { receive({ event: 'error', message: '预览资源未能释放，请重试关闭预览或退出。' }); throw error })
})
ipcMain.handle('myrk:reveal-artifact', async (event, value: unknown) => {
  if (event.sender !== window?.webContents) throw new Error('无法识别操作来源。')
  shell.showItemInFolder(await previews.reveal(value))
})
ipcMain.handle('myrk:reveal-file', async (event, value: unknown) => {
  if (event.sender !== window?.webContents || typeof value !== 'string' || !isAbsolute(value) || !/^[A-Za-z]:[\\/]/.test(value) || !recordedFiles.has(resolve(value))) throw new Error('请从真实动作记录选择本机文件位置。')
  if (!(await stat(value)).isFile()) throw new Error('该文件目前不存在，或不是普通文件。')
  shell.showItemInFolder(resolve(value))
})
ipcMain.handle('myrk:import-conversation', async event => {
  if (event.sender !== window?.webContents || !window || runningPaths.size > 0 || allowClose) throw new Error('请等待当前工作收尾后再导入。')
  const selection = await dialog.showOpenDialog(window, {
    title: '复制导入旧会话记录（保留原件）',
    properties: ['openFile'],
    filters: [{ name: '会话记录', extensions: ['json'] }],
  })
  if (selection.canceled || !selection.filePaths[0]) return
  await service!.send({ action: 'import', path: selection.filePaths[0] })
})
ipcMain.handle('myrk:select-document', async event => {
  if (event.sender !== window?.webContents) throw new Error('无法识别操作来源。')
  const owner = activePath
  const result = await dialog.showOpenDialog(window!, {
    title: '选择一份授权文本资料', properties: ['openFile'],
    filters: [{ name: '普通文本资料', extensions: ['txt', 'md'] }],
  })
  if (result.canceled || !result.filePaths[0]) return null
  const path = result.filePaths[0]
  if (!['.txt', '.md'].includes(extname(path).toLowerCase())) throw new Error('请选择 .txt 或 .md 普通文本资料。')
  selectedDocuments.set(owner, path)
  return path
})
ipcMain.handle('myrk:select-workspace', async event => {
  if (event.sender !== window?.webContents) throw new Error('无法识别操作来源。')
  const owner = activePath
  const result = await dialog.showOpenDialog(window!, {
    title: '选择本次允许工作的任务文件夹', properties: ['openDirectory'],
  })
  if (result.canceled || !result.filePaths[0]) return null
  const selections = selectedWorkspaces.get(owner) ?? new Set<string>()
  const selected = await realpath(result.filePaths[0])
  selections.add(selected)
  selectedWorkspaces.set(owner, selections)
  return selected
})

app.on('before-quit', event => {
  if (allowClose) return
  event.preventDefault()
  void quit()
})
app.on('window-all-closed', () => {})

await openWindow()
})
