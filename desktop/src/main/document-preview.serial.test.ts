// @vitest-environment node
import { access, mkdir, readFile, readdir, symlink } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import { prepareDocumentPreview } from './document-preview'

const projectRoot = dirname(execFileSync('git', ['rev-parse', '--path-format=absolute', '--git-common-dir'], { encoding: 'utf8', windowsHide: true }).trim())

test('a PDF preview keeps the exact fixed bytes after its caller changes the input buffer', async () => {
  const runtimeRoot = await testDirectory('document-preview-pdf-')
  const bytes = new Uint8Array(await readFile(new URL('../../scripts/office-preview-fixtures/pdf-layout.pdf', import.meta.url)))
  const original = Buffer.from(bytes)
  const preview = await prepareDocumentPreview({ snapshotId: 'fixed-pdf', name: '中文报告.pdf', extension: '.pdf', bytes, sha256: 'validated-by-main', runtimeRoot, projectRoot, signal: new AbortController().signal })
  bytes.fill(0)
  expect(preview.content).toMatchObject({ kind: 'document', format: 'pdf', sections: [{ id: 'document', label: '中文报告.pdf' }] })
  expect(Buffer.from(preview.content.sections[0].pdfBase64, 'base64')).toEqual(original)
  await preview.close()
  await preview.close()
})

test.runIf(process.platform === 'win32')('a fixed Word snapshot becomes a local PDF and releases all of its temporary content on close', async () => {
  const runtimeRoot = await testDirectory('document-preview-word-')
  const bytes = new Uint8Array(await readFile(new URL('../../scripts/office-preview-fixtures/word-layout.docx', import.meta.url)))
  const preview = await prepareDocumentPreview({ snapshotId: 'word-snapshot', name: '分页表格.docx', extension: '.docx', bytes, sha256: 'validated-by-main', runtimeRoot, projectRoot, signal: new AbortController().signal })
  expect(preview.content.format).toBe('docx')
  expect(Buffer.from(preview.content.sections[0].pdfBase64, 'base64').subarray(0, 5).toString()).toBe('%PDF-')
  await preview.close()
  expect(await readdir(runtimeRoot)).toEqual([])
}, 60000)

test.each(['outside', 'junction'])('Office preview refuses a %s runtime location before creating its output', async mode => {
  const directory = await testDirectory('document-preview-boundary-')
  const isolatedProject = join(directory, 'project')
  const outside = join(directory, 'outside')
  await mkdir(isolatedProject)
  await mkdir(outside)
  if (mode === 'junction') await symlink(outside, join(isolatedProject, 'redirect'), 'junction')
  const runtimeRoot = mode === 'junction' ? join(isolatedProject, 'redirect', 'never-created') : join(outside, 'never-created')
  await expect(prepareDocumentPreview({ snapshotId: 'invalid-root', name: 'test.docx', extension: '.docx', bytes: new Uint8Array(), sha256: 'validated-by-main', runtimeRoot, projectRoot: isolatedProject, signal: new AbortController().signal })).rejects.toThrow('预览临时目录必须位于项目内')
  await expect(access(join(outside, 'never-created'))).rejects.toThrow()
})
