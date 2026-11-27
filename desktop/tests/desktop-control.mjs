import { appendFileSync, existsSync } from 'node:fs'
import { isAbsolute } from 'node:path'

export function installNormalExit(app) {
  const flag = process.env.MYRK_TEST_NORMAL_EXIT_FLAG
  if (!flag || !isAbsolute(flag)) throw new Error('A dedicated MYRK_TEST_NORMAL_EXIT_FLAG is required before desktop startup')
  let requested = false
  const observe = () => {
    if (requested || !existsSync(flag)) return
    requested = true
    appendFileSync(`${flag}.jsonl`, JSON.stringify({ action: 'normal-exit-requested', pid: process.pid, ready: app.isReady(), at: new Date().toISOString() }) + '\n')
    app.quit()
  }
  const timer = setInterval(observe, 50)
  app.once('will-quit', () => {
    clearInterval(timer)
    appendFileSync(`${flag}.jsonl`, JSON.stringify({ action: 'will-quit', pid: process.pid, requested, at: new Date().toISOString() }) + '\n')
  })
  observe()
  return { get requested() { return requested } }
}
