// @vitest-environment node
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import { startDesktop } from '../../tests/desktop-process'

test('the same hidden desktop verifies an accidental crash then exits normally and reopens', async () => {
  const directory = await testDirectory('electron-recovery04-')
  await writeFile(join(directory, 'notes.txt'), '苹果 3\n', 'utf8')
  await writeFile(join(directory, 'check.py'), "from pathlib import Path\np=Path('runs.txt')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nassert Path('notes.txt').read_text(encoding='utf-8') == '苹果 5\\n'\nprint('real-recovered-check')\n", 'utf8')
  const processes: object[] = []
  for (const phase of ['crash', 'recover', 'normal-reopen']) {
    const phaseDirectory = join(directory, phase)
    await mkdir(phaseDirectory)
    const crashFlag = join(phaseDirectory, 'intentional-crash.flag')
    const desktop = await startDesktop(phaseDirectory, 'tests/recovery.electron.mjs', {
      MYRK_SERVICE_MODULE: 'tests.recovery_fixture',
      MYRK_USER_DATA_DIRECTORY: join(directory, 'user-data'), MYRK_DATA_DIRECTORY: join(directory, 'records'),
      MYRK_RECOVERY_DIRECTORY: directory, MYRK_RECOVERY_PHASE: phase,
      MYRK_RECOVERY_POINT: phase === 'crash' ? 'after_edit' : '', MYRK_RECOVERY_MARKER: join(directory, 'interrupt.pid'),
      MYRK_RECOVERY_CRASH_FLAG: crashFlag,
    })
    const { child, closed } = desktop
    let closing
    try {
      if (phase === 'crash') {
        await expect.poll(async () => readFile(join(directory, 'crash.jsonl'), 'utf8').catch(() => ''), { timeout: 20000 }).toContain('ready-to-crash')
        const before = await desktop.observer.status()
        expect(desktop.root).toMatchObject({ state: 'running', ownership: 'owned' })
        expect(before.processes.every(item => item.ownership === 'owned')).toBe(true)
        await writeFile(join(phaseDirectory, 'before-intentional-crash.json'), JSON.stringify({ root: desktop.root, before, directSpawn: { pid: child.pid, executable: child.spawnfile, argv: child.spawnargs } }, null, 2))
        await writeFile(crashFlag, 'intentional crash after exact identity capture\n')
        const servicePid = Number(await readFile(join(directory, 'records', 'service.pid'), 'utf8'))
        await expect.poll(async () => (await desktop.observer.status()).processes.find(item => item.pid === servicePid)?.state, { timeout: 5000 }).toBe('exited')
        expect(child.exitCode).toBeNull()
        expect(child.signalCode).toBeNull()
        expect(child.kill()).toBe(true)
      } else await expect.poll(() => child.exitCode, { timeout: 20000 }).toBe(0)
      await closed
      const diagnostic = desktop.diagnostic()
      const log = await readFile(join(directory, `${phase}.jsonl`), 'utf8')
      expect({ phase, diagnostic, log }).not.toMatchObject({ log: expect.stringContaining('"step":"failed"') })
      if (phase !== 'crash') expect(log).toContain('normal-exit')
      processes.push({ phase, pid: child.pid, exit: child.exitCode, log })
    } finally {
      closing = await desktop.finish()
      await writeFile(join(directory, `${phase}-diagnostic.txt`), desktop.diagnostic())
      await writeFile(join(directory, 'processes.json'), JSON.stringify(processes, null, 2))
    }
    expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
  }
  expect(await readFile(join(directory, 'runs.txt'), 'utf8')).toBe('x')
}, 65000)
