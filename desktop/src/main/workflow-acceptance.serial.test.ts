// @vitest-environment node
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { startDesktop } from '../../tests/desktop-process'
import { testDirectory } from '../../tests/run-context.mjs'

for (const mode of ['programming', 'documents']) test(`the production desktop completes the ${mode} workflow with saved real deliveries and reading controls`, async () => {
  const directory = await testDirectory(`workflow-${mode}-`)
  const task = join(directory, 'task')
  await mkdir(task)
  const outputs = [['pdf-layout.pdf', '分页资料.pdf'], ['word-layout.docx', '分页资料.docx'], ['workbook-layout.xlsx', '多表资料.xlsx'], ['slides-layout.pptx', '幻灯资料.pptx'], ['input.png', '图形资料.png'], ['input.md', '完整资料.md']]
  if (mode === 'programming') {
    await writeFile(join(task, 'calculator.py'), '# 完整代码首行\n' + Array.from({ length: 100 }, (_, index) => `# 原始说明 ${index + 1}\n`).join('') + 'def add(left, right):\n    return left - right\n# 完整代码末行\n', 'utf8')
    await writeFile(join(task, 'check.py'), "from calculator import add\nassert add(7, 5) == 12\nassert add(-3, 2) == -1\nprint('actual-workflow-check-passed')\n", 'utf8')
  } else {
    for (const [source] of outputs.slice(0, 4)) await copyFile(new URL(`../../scripts/office-preview-fixtures/${source}`, import.meta.url), join(task, source))
    await writeFile(join(task, 'input.md'), '# 完整资料首行\n\n' + Array.from({ length: 150 }, (_, index) => `资料第 ${index + 1} 段：原始内容完整保留。\n\n`).join('') + '完整资料末行。\n', 'utf8')
  }
  for (const phase of ['run', 'reopen']) {
    const desktop = await startDesktop(join(directory, 'desktop'), 'tests/workflow-acceptance.electron.mjs', {
      MYRK_SERVICE_MODULE: 'tests.workflow_desktop_fixture', MYRK_WORKFLOW_CASE: directory, MYRK_WORKFLOW_MODE: mode,
      MYRK_WORKFLOW_PHASE: phase, MYRK_WORKFLOW_OUTPUTS: JSON.stringify(outputs),
    })
    let closing
    let result: { evidenceDirectory?: string } | undefined
    const prefix = phase === 'run' ? '' : 'reopen-'
    try {
      await expect.poll(() => desktop.child.exitCode, { timeout: 120000 }).not.toBeNull()
      const failure = await readFile(join(directory, `${prefix}failure.json`), 'utf8').catch(() => '')
      expect({ diagnostic: desktop.diagnostic(), failure }).toMatchObject({ failure: '' })
      expect(await desktop.closed, desktop.diagnostic()).toBe(0)
      result = JSON.parse(await readFile(join(directory, `${prefix}result.json`), 'utf8'))
      expect(result).toMatchObject({ allPassed: true, mode, phase, rendererErrors: [] })
    } finally {
      closing = await desktop.finish()
    }
    expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
    if (result?.evidenceDirectory) await writeFile(join(result.evidenceDirectory, 'processes.json'), JSON.stringify({ root: desktop.root, ...closing }, null, 2))
  }
}, 160000)

test('the narrow production desktop reserves readable document and chat areas at 125 percent zoom', async () => {
  const directory = await testDirectory('workflow-reading-layout-')
  const task = join(directory, 'task')
  await mkdir(task)
  await copyFile(new URL('../../scripts/office-preview-fixtures/pdf-layout.pdf', import.meta.url), join(task, 'input.pdf'))
  const desktop = await startDesktop(join(directory, 'desktop'), 'tests/workflow-acceptance.electron.mjs', {
    MYRK_SERVICE_MODULE: 'tests.workflow_desktop_fixture', MYRK_WORKFLOW_CASE: directory, MYRK_WORKFLOW_MODE: 'layout',
    MYRK_WORKFLOW_OUTPUTS: JSON.stringify([['input.pdf', '分页资料.pdf']]),
  })
  let closing
  let result: { evidenceDirectory?: string } | undefined
  try {
    await expect.poll(() => desktop.child.exitCode, { timeout: 35000 }).not.toBeNull()
    const failure = await readFile(join(directory, 'failure.json'), 'utf8').catch(() => '')
    expect({ diagnostic: desktop.diagnostic(), failure }).toMatchObject({ failure: '' })
    expect(await desktop.closed, desktop.diagnostic()).toBe(0)
    result = JSON.parse(await readFile(join(directory, 'result.json'), 'utf8'))
    expect(result).toMatchObject({ allPassed: true, mode: 'layout', rendererErrors: [] })
  } finally {
    closing = await desktop.finish()
  }
  expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
  if (result?.evidenceDirectory) await writeFile(join(result.evidenceDirectory, 'processes.json'), JSON.stringify({ root: desktop.root, ...closing }, null, 2))
}, 50000)
