// @vitest-environment node
import { readdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import { startDesktop } from '../../tests/desktop-process'

test('the real desktop saves, fully restarts, restores and forgets a local credential', async () => {
  const directory = await testDirectory('native-connection-persistence-')
  for (const phase of ['save', 'restore', 'forgotten']) {
    const desktop = await startDesktop(directory, 'tests/connection-persistence.electron.mjs', { MYRK_CREDENTIAL_TEST_DIRECTORY: directory, MYRK_CREDENTIAL_TEST_PHASE: phase })
    try {
      await expect.poll(() => desktop.child.exitCode, { timeout: 30000 }).not.toBeNull()
      expect(await desktop.closed, desktop.diagnostic()).toBe(0)
      expect(JSON.parse(await readFile(join(directory, `${phase}.json`), 'utf8')), desktop.diagnostic()).toMatchObject({ passed: true, noAutomaticWork: true, noSecretEvents: true })
    } finally {
      expect(await desktop.finish()).toMatchObject({ complete: true, rootClosed: true })
    }
  }
  const inspect = async (path: string): Promise<void> => {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      const target = join(path, entry.name)
      if (entry.isDirectory()) await inspect(target)
      else if (entry.isFile()) expect((await readFile(target)).includes(Buffer.from('synthetic-native-credential-7f83')), target).toBe(false)
    }
  }
  await inspect(directory)
}, 100000)
