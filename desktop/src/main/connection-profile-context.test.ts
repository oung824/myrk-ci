// @vitest-environment node
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import { startDesktop } from '../../tests/desktop-process'

test('the formal owner restores a connection saved in its original encryption context', async () => {
  const directory = await testDirectory('connection-profile-context-')
  for (const phase of ['save', 'restore']) {
    const desktop = await startDesktop(directory, 'tests/connection-profile-context.electron.mjs', {
      MYRK_PROFILE_CONTEXT_DIRECTORY: directory, MYRK_PROFILE_CONTEXT_PHASE: phase,
    })
    try {
      await expect.poll(() => desktop.child.exitCode, { timeout: 30000 }).not.toBeNull()
      expect(await desktop.closed, desktop.diagnostic()).toBe(0)
      expect(JSON.parse(await readFile(join(directory, `${phase}.json`), 'utf8')), desktop.diagnostic()).toMatchObject({ passed: true, automatic_work: false })
    } finally {
      expect(await desktop.finish()).toMatchObject({ complete: true, rootClosed: true })
    }
  }
}, 70000)
