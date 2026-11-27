import { createHash, randomUUID } from 'node:crypto'
import { lstat, open, realpath } from 'node:fs/promises'
import { extname, isAbsolute, relative, resolve } from 'node:path'
import type { Action, DeliveredArtifact, FileChange, ServiceEvent, Turn } from '../shared/protocol'
import type { ArtifactPreviewRequest, ArtifactPreviewResult, DocumentPreviewInput, ImagePreviewContent, PreparedDocumentPreview, TextPreviewContent } from '../shared/artifact-preview'

const textExtensions = new Set(['.txt', '.md', '.markdown', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.csv', '.tsv', '.log', '.py', '.js', '.jsx', '.ts', '.tsx', '.css', '.html', '.xml', '.svg', '.sql', '.sh', '.ps1', '.bat', '.cmd', '.rs', '.go', '.java', '.c', '.h', '.cpp', '.cs', '.rb', '.php', '.vue', '.svelte', '.swift', '.kt', '.r'])
const imageExtensions = new Map<string, ImagePreviewContent['mimeType']>([['.png', 'image/png'], ['.jpg', 'image/jpeg'], ['.jpeg', 'image/jpeg'], ['.gif', 'image/gif'], ['.webp', 'image/webp']])
const documentExtensions = new Set(['.pdf', '.docx', '.xlsx', '.pptx'])
const textLimit = 5 * 1024 * 1024
const imageLimit = 20 * 1024 * 1024
const documentLimit = 50 * 1024 * 1024
const fingerprint = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex')
const samePath = (left: string, right: string) => resolve(left).toLowerCase() === resolve(right).toLowerCase()
const within = (root: string, path: string) => { const part = relative(resolve(root), resolve(path)); return part === '' || part !== '..' && !part.startsWith('..\\') && !part.startsWith('../') && !isAbsolute(part) }

function request(value: unknown, keys: string[]): Record<string, string> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('请选择本会话登记的成果。')
  const object = value as Record<string, unknown>
  if (Object.keys(object).length !== keys.length || keys.some(key => typeof object[key] !== 'string' || !object[key])) throw new Error('请选择本会话登记的成果。')
  return object as Record<string, string>
}

async function readSnapshot(path: string, limit: number): Promise<Buffer> {
  if (!isAbsolute(path) || (await lstat(path)).isSymbolicLink() || !samePath(await realpath(path), path)) throw new Error('文件位置包含链接，无法直接预览。')
  const handle = await open(path, 'r')
  try {
    const before = await handle.stat()
    if (!before.isFile()) throw new Error('目标不是普通文件。')
    if (before.size > limit) throw Object.assign(new Error('文件超过预览大小上限；可以在文件夹中定位原件。'), { code: 'PREVIEW_TOO_LARGE' })
    const buffer = Buffer.alloc(before.size + 1)
    let length = 0
    while (length < buffer.length) {
      const part = await handle.read(buffer, length, buffer.length - length)
      if (part.bytesRead === 0) break
      length += part.bytesRead
    }
    const bytes = buffer.subarray(0, length)
    const after = await handle.stat()
    if (bytes.length > limit) throw Object.assign(new Error('文件超过预览大小上限；可以在文件夹中定位原件。'), { code: 'PREVIEW_TOO_LARGE' })
    if (before.size !== after.size || before.mtimeMs !== after.mtimeMs || bytes.length !== before.size) throw new Error('读取时文件正在变化，请稍后刷新。')
    return bytes
  } finally { await handle.close() }
}

function decodeText(bytes: Uint8Array): string {
  const encoding = bytes[0] === 0xff && bytes[1] === 0xfe ? 'utf-16le' : bytes[0] === 0xfe && bytes[1] === 0xff ? 'utf-16be' : 'utf-8'
  const text = new TextDecoder(encoding, { fatal: true }).decode(bytes)
  if (text.includes('\u0000')) throw new Error('文件不是可直接读取的普通文字。')
  return text
}

function imageDimensions(bytes: Buffer, mimeType: ImagePreviewContent['mimeType']): { width: number; height: number } | null {
  if (mimeType === 'image/png' && bytes.length >= 24) return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) }
  if (mimeType === 'image/gif' && bytes.length >= 10) return { width: bytes.readUInt16LE(6), height: bytes.readUInt16LE(8) }
  if (mimeType === 'image/webp' && bytes.length >= 30) {
    const format = bytes.subarray(12, 16).toString('ascii')
    if (format === 'VP8X') return { width: 1 + bytes.readUIntLE(24, 3), height: 1 + bytes.readUIntLE(27, 3) }
    if (format === 'VP8 ' && bytes.subarray(23, 26).equals(Buffer.from([157, 1, 42]))) return { width: bytes.readUInt16LE(26) & 0x3fff, height: bytes.readUInt16LE(28) & 0x3fff }
    if (format === 'VP8L' && bytes[20] === 47) return { width: 1 + ((bytes[22] & 63) << 8 | bytes[21]), height: 1 + ((bytes[24] & 15) << 10 | bytes[23] << 2 | bytes[22] >> 6) }
  }
  if (mimeType === 'image/jpeg') {
    let offset = 2
    while (offset + 4 < bytes.length && bytes[offset] === 255) {
      const marker = bytes[offset + 1]
      if (marker === 255) { offset++; continue }
      const length = bytes.readUInt16BE(offset + 2)
      if (length < 2 || offset + length + 2 > bytes.length) break
      if ([192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207].includes(marker) && length >= 7) return { width: bytes.readUInt16BE(offset + 7), height: bytes.readUInt16BE(offset + 5) }
      offset += length + 2
    }
  }
  return null
}

export class ArtifactPreviews {
  private conversations = new Map<string, Turn[]>()
  private active = ''
  private snapshots = new Map<string, { conversationId: string; requestId: string; controller: AbortController; close?: () => Promise<void>; operation?: Promise<unknown>; closing?: Promise<void> }>()

  constructor(private options: { runtimeRoot: string; projectRoot: string; imageSize: (bytes: Buffer, mimeType: ImagePreviewContent['mimeType']) => { width: number; height: number } | Promise<{ width: number; height: number }>; prepareDocument?: (input: DocumentPreviewInput) => Promise<PreparedDocumentPreview> }) {}

  observe(event: ServiceEvent): void {
    if (event.event === 'ready' || event.event === 'conversation') this.active = event.path
    if (event.event === 'snapshot') {
      this.active = event.active_path
      for (const conversation of event.conversations) this.conversations.set(conversation.path, conversation.turns)
    }
    if (event.event === 'conversation') this.conversations.set(event.path, event.turns)
    if (event.event === 'started') {
      const path = event.path ?? this.active
      const turns = this.conversations.get(path) ?? []
      this.conversations.set(path, [...turns, { prompt: event.prompt, answer: '', status: 'running', error: null, requests: 0, input_tokens: null, output_tokens: null, thinking: false, task: event.task, approval_settings: event.approval_settings }])
    }
    if (event.event === 'artifacts' || event.event === 'finished') {
      const turns = [...(this.conversations.get(event.path) ?? [])]
      const index = turns.findLastIndex(turn => turn.task?.id === event.turn.task?.id)
      if (index < 0) turns.push(event.turn)
      else turns[index] = event.turn
      this.conversations.set(event.path, turns)
    }
  }

  private locate(input: ArtifactPreviewRequest): { request: ArtifactPreviewRequest; artifact: DeliveredArtifact; action: Action; change: FileChange } {
    if (input.conversationId !== this.active) throw new Error('请切换到成果所属会话后再打开。')
    const turns = this.conversations.get(input.conversationId) ?? []
    const turn = turns.find(item => item.artifacts?.some(artifact => artifact.id === input.artifactId))
    const artifact = turn?.artifacts?.find(item => item.id === input.artifactId)
    const action = turns.flatMap(item => item.actions ?? []).find(item => item.id === artifact?.source_action_id)
    const change = action?.file_changes?.find(item => item.id === artifact?.file_change_id)
    if (!turn || !artifact || !action || !change || action.status !== 'completed' || action.effect !== 'completed' || action.task_id !== artifact.task_id || action.delegation_id !== artifact.delegation_id || !samePath(change.path, artifact.path) || change.after_sha256 !== artifact.delivered_sha256) throw new Error('成果缺少对应的真实交付依据。')
    const scope = artifact.delegation_id ? turn.delegations?.find(item => item.id === artifact.delegation_id)?.scope ?? turn.execution_scope : turn.execution_scope ?? turn.task?.scope
    const settings = action.approval_settings
    const roots = [scope?.root, ...(settings?.access.writable_roots ?? [])].filter((item): item is string => !!item)
    const declaredOutputs = action.prepared_parameters?.output_paths
    const approvedTargets = [action.target, ...(Array.isArray(declaredOutputs) ? declaredOutputs.filter((target): target is string => typeof target === 'string') : [])]
    const exactApproval = ['user', 'assistant'].includes(action.approval_basis ?? '') && approvedTargets.some(path => isAbsolute(path) && samePath(path, artifact.path))
    if (!isAbsolute(artifact.path) || settings?.access.denied_paths.some(path => within(path, artifact.path)) || !(settings?.access.sandbox === 'full_access' || roots.some(root => within(root, artifact.path)) || exactApproval)) throw new Error('成果位置不在该动作保存的有效范围内。')
    return { request: { conversationId: input.conversationId, artifactId: input.artifactId }, artifact, action, change }
  }

  private async revealLocated(artifact: DeliveredArtifact): Promise<string> {
    const info = await lstat(artifact.path)
    if (!info.isFile() || info.isSymbolicLink() || !samePath(await realpath(artifact.path), artifact.path)) throw new Error('成果目前不存在，或不是普通真实文件。')
    return artifact.path
  }

  async reveal(value: unknown): Promise<string> {
    const { conversationId, artifactId } = request(value, ['conversationId', 'artifactId'])
    return this.revealLocated(this.locate({ conversationId, artifactId }).artifact)
  }

  async open(value: unknown): Promise<ArtifactPreviewResult> {
    const input = request(value, ['conversationId', 'artifactId', 'requestId'])
    if ([...this.snapshots.values()].some(item => item.requestId === input.requestId)) throw new Error('这次预览已经打开。')
    const located = this.locate({ conversationId: input.conversationId, artifactId: input.artifactId })
    const { artifact, change, action } = located
    const base = { ...located.request, name: artifact.name }
    const extension = extname(artifact.path).toLowerCase()
    const kind = textExtensions.has(extension) ? 'text' : imageExtensions.has(extension) ? 'image' : documentExtensions.has(extension) ? 'document' : null
    if (!kind) return this.revealLocated(artifact).then(() => ({ ...base, status: 'unsupported' as const, message: '此格式暂不支持只读预览。旧版或带宏 Office 文件请在文件夹中查看原件。' }), (error: NodeJS.ErrnoException) => ({ ...base, status: error.code === 'ENOENT' ? 'missing' as const : 'unreadable' as const, message: error.code === 'ENOENT' ? '交付文件目前已丢失；成果记录仍保留。' : error.message }))
    const snapshotId = randomUUID()
    const resource: { conversationId: string; requestId: string; controller: AbortController; close?: () => Promise<void>; operation?: Promise<unknown>; closing?: Promise<void> } = { conversationId: base.conversationId, requestId: input.requestId, controller: new AbortController() }
    this.snapshots.set(snapshotId, resource)
    const operation = (async (): Promise<ArtifactPreviewResult> => {
      const bytes = await readSnapshot(artifact.path, kind === 'text' ? textLimit : kind === 'image' ? imageLimit : documentLimit)
      if (resource.controller.signal.aborted) throw new Error('预览已取消。')
      const sha256 = fingerprint(bytes)
      const result = { ...base, status: 'ready' as const, snapshotId, sha256, deliveryState: sha256 === artifact.delivered_sha256 ? 'unchanged' as const : 'changed' as const }
      if (kind === 'text') {
        const text = decodeText(bytes)
        let history: TextPreviewContent['history'] = { status: 'unavailable', message: '没有可核对的历史原件；下面显示当前全文。' }
        if (change.change_kind === 'created') history = { status: 'created', message: '此动作新建文件，没有修改前版本。' }
        else if (sha256 !== change.after_sha256) history = { status: 'unavailable', message: '当前文件在该次修改后又有变化，不能把当前全文当作当时的修改后版本。' }
        else if (change.backup_path && change.before_sha256) {
          const beforeText = await readSnapshot(change.backup_path, textLimit).then(before => fingerprint(before) === change.before_sha256 ? decodeText(before) : null).catch(() => null)
          if (beforeText !== null) history = { status: 'available', actionId: action.id, beforeText, afterText: text }
          else history = { status: 'unavailable', message: '历史原件缺失、无法读取或指纹不符，无法可靠显示修改对照；下面显示当前全文。' }
        }
        return { ...result, content: { kind: 'text', text, format: ['.md', '.markdown'].includes(extension) ? 'markdown' : extension === '.txt' ? 'plain' : 'code', language: extension.slice(1) || null, history } }
      }
      if (kind === 'image') {
        const signatures: Record<ImagePreviewContent['mimeType'], boolean> = { 'image/png': bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])), 'image/jpeg': bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255, 'image/gif': /^GIF8[79]a/.test(bytes.subarray(0, 6).toString('ascii')), 'image/webp': bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WEBP' }
        const mimeType = imageExtensions.get(extension)!
        if (!signatures[mimeType]) throw new Error('图片内容与文件类型不符，无法预览。')
        const dimensions = imageDimensions(bytes, mimeType)
        if (dimensions && dimensions.width * dimensions.height > 40_000_000) throw Object.assign(new Error('图片超过四千万像素的预览上限。'), { code: 'PREVIEW_TOO_LARGE' })
        const size = await this.options.imageSize(bytes, mimeType)
        if (!size.width || !size.height) throw new Error('图片无法完整解码。')
        if (size.width * size.height > 40_000_000) throw Object.assign(new Error('图片超过四千万像素的预览上限。'), { code: 'PREVIEW_TOO_LARGE' })
        return { ...result, content: { kind: 'image', mimeType, base64: bytes.toString('base64'), ...size } }
      }
      if (!this.options.prepareDocument) return { ...base, status: 'unsupported', message: '文档预览尚未就绪；可以在文件夹中定位原件。' }
      const prepared = await this.options.prepareDocument({ snapshotId, name: artifact.name, extension, bytes, sha256, runtimeRoot: this.options.runtimeRoot, projectRoot: this.options.projectRoot, signal: resource.controller.signal })
      resource.close = prepared.close
      return { ...result, content: prepared.content }
    })()
    resource.operation = operation
    return operation.then(async result => {
      if (resource.controller.signal.aborted || this.active !== base.conversationId) {
        await this.close({ snapshotId })
        throw new Error('预览已关闭。')
      }
      if (result.status !== 'ready') await this.close({ snapshotId })
      return result
    }, async (error: NodeJS.ErrnoException) => {
      await this.close({ snapshotId })
      return { ...base, status: error.code === 'ENOENT' ? 'missing' : error.code === 'PREVIEW_TOO_LARGE' ? 'too_large' : 'unreadable', message: error.code === 'ENOENT' ? '交付文件目前已丢失；成果记录仍保留。' : error instanceof TypeError ? '文字编码无法完整读取；支持 UTF-8 或带标记的 UTF-16。' : error.message }
    })
  }

  async close(value: unknown): Promise<void> {
    const byRequest = value !== null && typeof value === 'object' && 'requestId' in value
    const input = request(value, [byRequest ? 'requestId' : 'snapshotId'])
    const snapshotId = byRequest ? [...this.snapshots].find(([, item]) => item.requestId === input.requestId)?.[0] : input.snapshotId
    if (!snapshotId) return
    const resource = this.snapshots.get(snapshotId)
    if (!resource) return
    if (!resource.closing) resource.closing = (async () => {
      resource.controller.abort()
      await resource.operation?.catch(() => {})
      await resource.close?.()
      this.snapshots.delete(snapshotId)
    })().catch(error => { resource.closing = undefined; throw error })
    await resource.closing
  }

  async clear(): Promise<void> {
    await Promise.all([...this.snapshots.keys()].map(snapshotId => this.close({ snapshotId })))
  }
}
