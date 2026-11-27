// @vitest-environment node
import { readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { build } from 'vite'
import { expect, test } from 'vitest'
import { startDesktop } from '../../tests/desktop-process'
import { testDirectory } from '../../tests/run-context.mjs'

test('real Electron encryption restores saved connections after full process exit', async () => {
  const directory = await testDirectory('connection-store-electron-')
  const compiled = join(directory, 'compiled')
  await build({ configFile: false, logLevel: 'silent', cacheDir: join(directory, 'cache'), build: {
    ssr: resolve('src/main/connection-store.ts'), outDir: compiled,
    rolldownOptions: { output: { entryFileNames: 'store.mjs' } },
  } })
  const identities: number[] = []
  for (const phase of ['save', 'restore']) {
    const desktop = await startDesktop(directory, 'tests/connection-store.electron.mjs', {
      MYRK_CONNECTION_STORE_MODULE: join(compiled, 'store.mjs'), MYRK_CONNECTION_STORE_PHASE: phase,
      MYRK_CONNECTION_STORE_DIRECTORY: directory,
    })
    identities.push(desktop.child.pid!)
    try {
      await expect.poll(() => desktop.child.exitCode, { timeout: 15000 }).not.toBeNull()
      expect(await desktop.closed, desktop.diagnostic()).toBe(0)
      expect(JSON.parse(await readFile(join(directory, `${phase}.json`), 'utf8'))).toMatchObject({ phase, complete: true, electron: '45.2.1' })
    } finally {
      expect(await desktop.finish()).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
    }
  }
  expect(identities[0]).not.toBe(identities[1])
}, 30000)
