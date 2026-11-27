// @vitest-environment node
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import { startDesktop } from '../../tests/desktop-process'

test('real desktop runs three approval modes, preserves trust and confirms stop and reopen effects', async ({ annotate }) => {
  const parent = process.env.MYRK_APPROVAL_ACCEPTANCE_PARENT
  if (parent) await mkdir(parent, { recursive: true })
  const directory = parent ? await mkdtemp(join(parent, 'run-')) : await testDirectory('am-')
  const project = join(directory, '项目')
  await mkdir(project)
  await mkdir(join(directory, '替换项目'))
  await writeFile(join(project, 'review-evidence.txt'), '仅供独立审核核对：这个受控检查只创建单个小文字文件。\n', 'utf8')
  const desktop = await startDesktop(directory, 'tests/approval-modes.electron.mjs', {
    MYRK_SERVICE_MODULE: 'tests.approval_desktop_fixture', MYRK_APPROVAL_ACCEPTANCE_ROOT: directory,
  })
  let closing
  try {
    await expect.poll(() => desktop.child.exitCode, { timeout: 90000 }).not.toBeNull()
    await desktop.closed
    const failure = await readFile(join(directory, 'failure.json'), 'utf8').catch(() => '')
    expect({ diagnostic: desktop.diagnostic(), failure }).toMatchObject({ failure: '' })
    expect(desktop.child.exitCode).toBe(0)
    const result = JSON.parse(await readFile(join(directory, 'result.json'), 'utf8'))
    expect(result.allPassed).toBe(true)
    expect(result.checks.length).toBeGreaterThanOrEqual(18)
    if (process.env.MYRK_UI_LAYOUT_EVIDENCE === '1') {
      const layoutPath = join(directory, 'ui-layout.json')
      const layout = JSON.parse(await readFile(layoutPath, 'utf8'))
      expect(layout.scaleMode).toBe('page_zoom')
      expect(layout.captures).toHaveLength(10)
      for (const capture of layout.captures) await annotate(capture.name, { path: join(directory, 'screenshots', capture.screenshot), contentType: 'image/png' })
      await annotate('Native control reachability and page zoom geometry', { path: layoutPath, contentType: 'application/json' })
    }
  } finally {
    closing = await desktop.finish()
  }
  expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
}, 100000)
