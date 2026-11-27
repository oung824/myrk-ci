// @vitest-environment node
import { copyFile, mkdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { startDesktop } from '../../tests/desktop-process'
import { testDirectory } from '../../tests/run-context.mjs'

test('the real desktop opens delivered PNG, JPEG, GIF and WebP images from fixed bytes', async () => {
  const directory = await testDirectory('electron-image-formats-')
  const task = join(directory, 'task')
  await mkdir(task)
  const outputs = ['png', 'jpg', 'gif', 'webp'].map(extension => [`sample.${extension}`, `交付图片.${extension}`])
  for (const [source] of outputs) await copyFile(new URL(`../../tests/image-preview-fixtures/${source}`, import.meta.url), join(task, source))
  const desktop = await startDesktop(join(directory, 'desktop'), 'tests/image-preview.electron.mjs', {
    MYRK_SERVICE_MODULE: 'tests.document_preview_fixture', MYRK_IMAGE_CASE: directory,
    MYRK_DOCUMENT_OUTPUTS: JSON.stringify(outputs),
    ...(process.env.MYRK_IMAGE_EVIDENCE_DIRECTORY ? { MYRK_IMAGE_EVIDENCE_DIRECTORY: process.env.MYRK_IMAGE_EVIDENCE_DIRECTORY } : {}),
  })
  let closing
  try {
    await expect.poll(() => desktop.child.exitCode, { timeout: 35000 }).not.toBeNull()
    expect(await desktop.closed, desktop.diagnostic()).toBe(0)
    const failure = await readFile(join(directory, 'failure.json'), 'utf8').catch(error => { if (error.code === 'ENOENT') return ''; throw error })
    expect(failure, desktop.diagnostic()).toBe('')
    const result = await readFile(join(directory, 'result.json'), 'utf8').catch(async () => { throw new Error(await readFile(join(directory, 'failure.json'), 'utf8')) })
    expect(JSON.parse(result)).toMatchObject({ allPassed: true, rows: [
      { extension: 'png', status: 'ready', width: 3, height: 2 },
      { extension: 'jpg', status: 'ready', width: 3, height: 2 },
      { extension: 'gif', status: 'ready', width: 3, height: 2 },
      { extension: 'webp', status: 'ready', width: 3, height: 2 },
    ], workspaceEscape: true, rendererErrors: [], externalRequests: [] })
  } finally { closing = await desktop.finish() }
  expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
}, 50000)
