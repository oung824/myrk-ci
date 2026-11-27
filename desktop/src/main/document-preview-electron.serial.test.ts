// @vitest-environment node
import { copyFile, mkdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { startDesktop } from '../../tests/desktop-process'
import { testDirectory } from '../../tests/run-context.mjs'

test('the built desktop renders a real delivered PDF with a local worker, page navigation and zoom', async () => {
  const directory = await testDirectory('electron-document-preview-')
  const task = join(directory, 'task')
  await mkdir(task)
  await copyFile(new URL('../../scripts/office-preview-fixtures/pdf-layout.pdf', import.meta.url), join(task, 'input.pdf'))
  const desktop = await startDesktop(join(directory, 'desktop'), 'tests/document-preview.electron.mjs', {
    MYRK_SERVICE_MODULE: 'tests.document_preview_fixture', MYRK_DOCUMENT_CASE: directory,
    MYRK_DOCUMENT_OUTPUTS: JSON.stringify([['input.pdf', '中文分页.pdf']]),
    ...(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY ? { MYRK_DOCUMENT_EVIDENCE_DIRECTORY: join(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY, 'pdf') } : {}),
  })
  let closing
  try {
    await expect.poll(() => desktop.child.exitCode, { timeout: 35000 }).not.toBeNull()
    expect(await desktop.closed, desktop.diagnostic()).toBe(0)
    expect(JSON.parse(await readFile(join(directory, 'result.json'), 'utf8'))).toMatchObject({ allPassed: true, pages: 5, rendererErrors: [] })
  } finally {
    closing = await desktop.finish()
  }
  expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
}, 50000)

test.runIf(process.platform === 'win32')('the built desktop reads real delivered Office pages, switches sheets and slides, and cancels a pending conversion', async () => {
  const directory = await testDirectory('electron-office-documents-')
  const task = join(directory, 'task')
  await mkdir(task)
  const outputs = [['word-layout.docx', '分页表格.docx'], ['workbook-layout.xlsx', '多表图表.xlsx'], ['slides-layout.pptx', '母版幻灯片.pptx']]
  for (const [source] of outputs) await copyFile(new URL(`../../scripts/office-preview-fixtures/${source}`, import.meta.url), join(task, source))
  const desktop = await startDesktop(join(directory, 'desktop'), 'tests/document-preview.electron.mjs', {
    MYRK_SERVICE_MODULE: 'tests.document_preview_fixture', MYRK_DOCUMENT_CASE: directory, MYRK_DOCUMENT_MODE: 'office',
    MYRK_DOCUMENT_OUTPUTS: JSON.stringify(outputs),
    ...(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY ? { MYRK_DOCUMENT_EVIDENCE_DIRECTORY: join(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY, 'office') } : {}),
  })
  let closing
  try {
    await expect.poll(() => desktop.child.exitCode, { timeout: 85000 }).not.toBeNull()
    expect(await desktop.closed, desktop.diagnostic()).toBe(0)
    expect(JSON.parse(await readFile(join(directory, 'result.json'), 'utf8'))).toMatchObject({ allPassed: true, wordPages: 5, worksheets: 2, slides: 2, pendingCancelled: true, rendererErrors: [] })
  } finally {
    closing = await desktop.finish()
  }
  expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
}, 100000)

test.runIf(process.platform === 'win32')('the built desktop explains encrypted and corrupt delivered Office files, locates their unchanged sources and releases preview resources', async () => {
  const directory = await testDirectory('electron-invalid-office-')
  const task = join(directory, 'task')
  await mkdir(task)
  const outputs = [['password-protected.docx', '密码保护.docx'], ['corrupt-document.docx', '损坏文档.docx']]
  for (const [source] of outputs) await copyFile(new URL(`../../scripts/office-preview-fixtures/${source}`, import.meta.url), join(task, source))
  const desktop = await startDesktop(join(directory, 'desktop'), 'tests/office-errors.electron.mjs', {
    MYRK_SERVICE_MODULE: 'tests.document_preview_fixture', MYRK_DOCUMENT_CASE: directory,
    MYRK_DOCUMENT_OUTPUTS: JSON.stringify(outputs),
    ...(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY ? { MYRK_DOCUMENT_EVIDENCE_DIRECTORY: join(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY, 'invalid-office') } : {}),
  })
  let closing
  try {
    await expect.poll(() => desktop.child.exitCode, { timeout: 40000 }).not.toBeNull()
    expect(await desktop.closed, desktop.diagnostic()).toBe(0)
    expect(JSON.parse(await readFile(join(directory, 'result.json'), 'utf8'))).toMatchObject({ allPassed: true, cases: [
      { name: '密码保护.docx', sourceUnchanged: true, resourcesReleased: true, existingOfficeUnchanged: true, located: true },
      { name: '损坏文档.docx', sourceUnchanged: true, resourcesReleased: true, existingOfficeUnchanged: true, located: true },
    ], rendererErrors: [] })
  } finally {
    closing = await desktop.finish()
    if (process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY) {
      const evidence = join(process.env.MYRK_DOCUMENT_EVIDENCE_DIRECTORY, 'invalid-office')
      await mkdir(evidence, { recursive: true })
      await copyFile(join(desktop.directory, 'processes.json'), join(evidence, 'processes.json'))
    }
  }
  expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
}, 55000)
