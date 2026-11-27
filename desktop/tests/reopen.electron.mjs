import { app } from 'electron'
import { existsSync, writeFileSync } from 'node:fs'
import { setTimeout as pause } from 'node:timers/promises'
import { builtMain } from './run-context.mjs'
import { installNormalExit } from './desktop-control.mjs'

const control = installNormalExit(app)
app.setPath('userData', process.env.MYRK_USER_DATA_DIRECTORY)
app.disableHardwareAcceleration()
async function run() {
  try {
    while (process.env.MYRK_REOPEN_RELEASE && !existsSync(process.env.MYRK_REOPEN_RELEASE) && !control.requested) await pause(25)
    if (!control.requested) {
      await import(builtMain())
      writeFileSync(process.env.MYRK_REOPEN_RESULT, JSON.stringify({ step: 'loaded', pid: process.pid }))
    }
  } catch (error) {
    writeFileSync(process.env.MYRK_REOPEN_RESULT, JSON.stringify({ step: 'failed', pid: process.pid, error: String(error), stack: error.stack }))
    app.quit()
  }
}
void run()
