import { mkdir, mkdtemp, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import type { DocumentPreviewInput, PreparedDocumentPreview } from '../shared/artifact-preview'
import { OfficePreviewCleanupError, openOfficePreview } from './office-preview'

export async function prepareDocumentPreview(options: DocumentPreviewInput): Promise<PreparedDocumentPreview> {
  if (options.signal.aborted) throw new Error('本次预览已取消。')
  if (options.extension === '.pdf') return {
    content: { kind: 'document', format: 'pdf', sections: [{ id: 'document', label: options.name, pdfBase64: Buffer.from(options.bytes).toString('base64') }] },
    close: async () => {},
  }
  if (options.extension !== '.docx' && options.extension !== '.xlsx' && options.extension !== '.pptx') {
    throw new Error('此格式暂不支持预览。仅支持 PDF、docx、xlsx 和 pptx；旧版及带宏 Office 文件请在文件夹中查看。')
  }
  const format = options.extension.slice(1) as 'docx' | 'xlsx' | 'pptx'
  const project = await realpath(options.projectRoot)
  const withinProject = (path: string) => {
    const location = relative(project, path)
    return location !== '..' && !location.startsWith(`..${sep}`) && !isAbsolute(location)
  }
  if (!isAbsolute(options.runtimeRoot) || !withinProject(resolve(options.runtimeRoot))) throw new Error('预览临时目录必须位于项目内，不能写入项目外的位置。')
  let ancestor = options.runtimeRoot
  let actual: string | undefined
  while (!actual) {
    actual = await realpath(ancestor).catch((error: NodeJS.ErrnoException) => {
      if (error.code === 'ENOENT') return undefined
      throw error
    })
    if (!actual) ancestor = dirname(ancestor)
  }
  if (!withinProject(actual)) throw new Error('预览临时目录必须位于项目内，不能经由链接写入项目外的位置。')
  await mkdir(options.runtimeRoot, { recursive: true })
  const directory = await mkdtemp(join(options.runtimeRoot, 'document-preview-'))
  const owner = {
    owner: 'document-preview', project_root: options.projectRoot, snapshot_id: options.snapshotId, runtime_root: options.runtimeRoot, runtime_directory: directory,
    pid: process.pid, created_at: new Date().toISOString(), process_started_at: new Date(Date.now() - process.uptime() * 1000).toISOString(),
    purpose: 'read-only fixed document snapshot', source_sha256: options.sha256,
    delete_when: 'Close or cancel the preview after the owned Office conversion has exited',
  }
  let office: Awaited<ReturnType<typeof openOfficePreview>> | undefined
  const release = async () => {
    await office?.close()
    await rm(directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  }
  const retainFailure = async (error: unknown): Promise<never> => {
    let recordingError = ''
    await writeFile(join(directory, 'owner.json'), JSON.stringify(owner, null, 2)).catch(reason => { recordingError = `归属记录未能保存：${String(reason)}` })
    await writeFile(join(directory, 'cleanup-failure.json'), JSON.stringify({ owner: owner.owner, snapshot_id: options.snapshotId,
      reason: String(error), failed_at: new Date().toISOString(), delete_when: 'Release the owned file lock and retry close',
    }, null, 2)).catch(reason => { recordingError += `清理失败记录未能保存：${String(reason)}` })
    throw new OfficePreviewCleanupError(directory, `预览资源未能释放，已保留本次资料：${String(error)}${recordingError}`)
  }
  try {
    await writeFile(join(directory, 'owner.json'), JSON.stringify(owner, null, 2))
    const sourcePath = join(directory, `source${options.extension}`)
    await writeFile(sourcePath, options.bytes)
    office = await openOfficePreview({ sourcePath, directory: join(directory, 'conversion'), signal: options.signal })
    const sizes = await Promise.all(office.sections.map(section => stat(section.pdfPath)))
    if (sizes.reduce((total, item) => total + item.size, 0) > 50 * 1024 * 1024) throw new Error('转换后的文档超过 50 MB，暂时无法预览。请在文件夹中查看原文件。')
    const sections: PreparedDocumentPreview['content']['sections'] = []
    for (const section of office.sections) {
      if (options.signal.aborted) throw new Error('本次预览已取消。')
      sections.push({ id: section.id, label: section.label, pdfBase64: (await readFile(section.pdfPath)).toString('base64') })
    }
    if (options.signal.aborted) throw new Error('本次预览已取消。')
    let closing: Promise<void> | undefined
    return { content: { kind: 'document', format, sections }, close: () => closing ??= release().catch(error => {
      closing = undefined
      return retainFailure(error)
    }) }
  } catch (error) {
    if (error instanceof OfficePreviewCleanupError) {
      error.message = `本次 Office 预览未能完全退出，已保留本次资料供检查。${error.message}`
      throw error
    }
    await release().catch(retainFailure)
    const reason = error instanceof Error ? error.message : String(error)
    if (/正在运行|转换后的文档超过/.test(reason)) throw error
    if (/cancelled|已取消/i.test(reason)) throw new Error('本次预览已取消。')
    if (/macros|external|embedded/i.test(reason)) throw new Error('文件含宏、外部链接或可执行的嵌入内容，无法进行只读预览。可在文件夹中查看原文件。')
    if (/password|encrypt|protected|密码|保护/i.test(reason)) throw new Error('文件受到密码或权限保护，无法预览。请在原应用中检查。')
    if (/Invalid|corrupt|archive|package|损坏/i.test(reason)) throw new Error('无法读取此 Office 文件。它可能已损坏、受到密码保护，或使用了不支持的格式。请在原应用中检查。')
    if (/timed out/i.test(reason)) throw new Error('本机 Office 转换超时，无法预览。请稍后重试。')
    throw new Error(`本机 Office 未能生成预览。请检查 Office 是否可用。原因：${reason}`)
  }
}
