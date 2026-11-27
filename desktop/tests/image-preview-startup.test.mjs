import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { setTimeout as pause } from 'node:timers/promises'
import { runInNewContext } from 'node:vm'

const source = await readFile(new URL('./image-preview.electron.mjs', import.meta.url), 'utf8')
const start = source.indexOf("  await execute('window.imageEvents=[]")
const configure = source.indexOf("{ action: 'configure'", start)
const end = source.lastIndexOf('  await execute(', configure)
assert.ok(start >= 0 && end > start, 'exercise the real image driver startup through folder trust')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const startup = new AsyncFunction('execute', 'waitFor', 'task', source.slice(start, end))

for (const alreadyReady of [false, true]) {
  test(`image startup waits for a fresh snapshot without a mounted send button; already ready=${alreadyReady}`, async () => {
    const initialized = Promise.withResolvers()
    const trace = []
    const listeners = []
    const timer = alreadyReady ? undefined : setTimeout(() => initialized.resolve(), 20)
    if (alreadyReady) initialized.resolve()
    const browser = {
      document: { querySelector: () => null },
      window: { myrk: {
        subscribe(listener) { listeners.push(listener) },
        async command(command) {
          await initialized.promise
          if (command.action === 'snapshot') {
            trace.push('snapshot')
            for (const listener of listeners) listener({ event: 'snapshot', active_path: 'current-conversation' })
          } else {
            assert.equal(command.action, 'set_project_trust')
            assert.deepEqual(trace, ['snapshot', 'selected'])
            trace.push('trusted')
          }
        },
        async selectWorkspace() {
          assert.deepEqual(trace, ['snapshot'], 'wait for the current conversation before selecting its folder')
          trace.push('selected')
          return 'synthetic-task'
        },
      } },
    }
    const execute = code => runInNewContext(code, browser)
    const waitFor = async predicate => {
      const deadline = Date.now() + 1000
      while (!await predicate()) {
        assert.ok(Date.now() < deadline, 'startup receives its fresh snapshot')
        await pause(1)
      }
    }
    try {
      await startup(execute, waitFor, 'synthetic-task')
      assert.deepEqual(trace, ['snapshot', 'selected', 'trusted'])
    } finally { clearTimeout(timer) }
  })
}
