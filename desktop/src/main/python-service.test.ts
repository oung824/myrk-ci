// @vitest-environment node
import { readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { expect, test } from 'vitest'
import type { ServiceEvent } from '../shared/protocol'
import { PythonService } from './python-service'
import { testDirectory } from '../../tests/run-context.mjs'

test('connection requests wait for backend acknowledgement and never forward their private replies', async () => {
  const events: ServiceEvent[] = []
  const service = new PythonService({
    python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'),
    dataDirectory: await testDirectory('credential-ack-'), module: 'tests.desktop_fixture',
  }, event => events.push(event))
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 15000 }).toBe(true)
    await expect(service.request({ action: 'configure' })).rejects.toThrow('连接密码')
    const result = await service.request({ action: 'configure', key: 'synthetic-ack-secret' })
    expect(result).toMatchObject({ event: 'configured', configured: true })
    expect(events.some(event => event.event === 'configured')).toBe(false)
    expect(JSON.stringify(events)).not.toContain('synthetic-ack-secret')
  } finally {
    await service.shutdown()
  }
}, 25000)

test('service failure evidence survives the real desktop connection and a rejected receiver sends nothing', async () => {
  const directory = await testDirectory('myrk-provider-recovery-09-')
  const events: ServiceEvent[] = []
  const service = new PythonService({
    python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'),
    dataDirectory: directory, module: 'tests.desktop_fixture',
  }, event => events.push(event))
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    await service.send({ action: 'configure', key: 'recovery-controlled' })
    await service.send({ action: 'send', prompt: '暂时失败保留原工作' })
    await expect.poll(() => events.find(event => event.event === 'finished'), { timeout: 10000 }).toBeTruthy()
    const failed = events.find(event => event.event === 'finished')!
    expect(failed).toMatchObject({ event: 'finished', saved: true, turn: { status: 'failed', service_failure: true, requests: 3, request_attempts: [
      { number: 1, actor: 'main', status: 'failed', http_status: 503 }, { number: 2, actor: 'main', status: 'failed', http_status: 503 }, { number: 3, actor: 'main', status: 'failed', http_status: 503 },
    ] } })
    if (failed.event !== 'finished' || !failed.turn.task) throw new Error('Missing failed task')
    const target = { name: 'DeepSeek', base_url: 'http://127.0.0.1:1/v1', model: 'second-controlled', protocol: 'openai-chat' as const }
    await service.send({ action: 'configure', key: 'new-controlled-password', provider_config: target })
    await service.send({ action: 'preview_provider', task_id: failed.turn.task.id, provider_config: target })
    await expect.poll(() => events.find(event => event.event === 'provider_preview'), { timeout: 10000 }).toBeTruthy()
    const preview = events.find(event => event.event === 'provider_preview')!
    expect(preview).toMatchObject({ event: 'provider_preview', change: { compatible: true, target } })
    if (preview.event !== 'provider_preview') throw new Error('Missing receiver preview')
    expect(preview.change.materials).toContain('暂时失败保留原工作')
    await service.send({ action: 'decide_provider', change_id: preview.change.id, approved: false })
    await expect.poll(() => events.find(event => event.event === 'provider_decided')).toBeTruthy()
    expect(events.filter(event => event.event === 'started')).toHaveLength(1)
    await service.send({ action: 'resume_task', task_id: failed.turn.task.id })
    await expect.poll(() => events.filter(event => event.event === 'finished')).toHaveLength(2)
    const resumed = events.filter(event => event.event === 'finished')[1]
    expect(resumed).toMatchObject({ event: 'finished', turn: { status: 'completed', task: { id: failed.turn.task.id, requests: 4 } } })
    expect(events.some(event => event.event === 'error' && event.fatal)).toBe(false)
  } finally {
    await service.shutdown()
  }
}, 20000)

test('desktop connection stops a live reply, saves partial text and closes its owned process', async () => {
  const directory = await testDirectory('myrk-desktop-')
  const events: ServiceEvent[] = []
  const service = new PythonService({
    python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe',
    root: resolve('..'),
    dataDirectory: directory,
    module: 'tests.desktop_fixture',
  }, event => events.push(event))
  const waitFor = async (kind: ServiceEvent['event']) => {
    await expect.poll(() => events.find(event => event.event === kind), { timeout: 10000 }).toBeTruthy()
    return events.find(event => event.event === kind)!
  }
  try {
    await waitFor('ready')
    await service.send({ action: 'configure', key: 'synthetic-secret-never-save' })
    await waitFor('configured')
    await service.send({ action: 'send', prompt: '请停止这次工作' })
    await waitFor('text')
    await service.send({ action: 'stop' })
    await waitFor('stopping')
    const finished = await waitFor('finished')
    expect(finished).toMatchObject({ event: 'finished', saved: true, turn: { status: 'cancelled', answer: '第一段' } })
    if (finished.event !== 'finished') throw new Error('Missing result')
    expect(await readFile(finished.path, 'utf8')).not.toContain('synthetic-secret-never-save')
    await service.shutdown()
    expect(service.exited).toBe(true)
    expect(events.at(-1)).toEqual({ event: 'closed' })
  } finally {
    await service.shutdown()
  }
}, 20000)

test('closing the desktop connection during a reply waits for the saved cancellation', async () => {
  const directory = await testDirectory('myrk-desktop-close-')
  const events: ServiceEvent[] = []
  const service = new PythonService({
    python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', root: resolve('..'),
    dataDirectory: directory, module: 'tests.desktop_fixture',
  }, event => events.push(event))
  try {
    await expect.poll(() => events.some(event => event.event === 'ready'), { timeout: 10000 }).toBe(true)
    await service.send({ action: 'configure', key: 'synthetic-secret-never-save' })
    await service.send({ action: 'send', prompt: '停止并关闭' })
    await expect.poll(() => events.some(event => event.event === 'text')).toBe(true)
    await service.shutdown()
    expect(service.exited).toBe(true)
    const finished = events.find(event => event.event === 'finished')
    expect(finished).toMatchObject({ event: 'finished', saved: true, turn: { status: 'cancelled', answer: '第一段' } })
    if (finished?.event !== 'finished') throw new Error('Missing cancellation')
    const record = JSON.parse(await readFile(finished.path, 'utf8'))
    expect(record.turns[0]).toMatchObject({ prompt: '停止并关闭', answer: '第一段', status: 'cancelled' })
  } finally {
    await service.shutdown()
  }
}, 20000)

test('a missing Python program shows a safe startup failure and shutdown completes', async () => {
  const events: ServiceEvent[] = []
  const service = new PythonService({ python: resolve('missing-python.exe'), root: resolve('..') }, event => events.push(event))
  await expect.poll(() => events.some(event => event.event === 'error')).toBe(true)
  expect(await service.shutdown()).toBe(true)
  expect(service.exited).toBe(true)
  expect(events).toContainEqual({ event: 'error', message: expect.stringContaining('无法启动办事程序'), fatal: true })
})

test('a launched Python program still needs a shutdown acknowledgement even if it exits cleanly', async () => {
  const root = await testDirectory('python-clean-exit-')
  await writeFile(resolve(root, 'clean_exit_fixture.py'), 'pass\n')
  const service = new PythonService({
    python: process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe',
    root,
    module: 'clean_exit_fixture',
  }, () => {})
  await expect.poll(() => service.exited).toBe(true)
  expect(await service.shutdown()).toBe(false)
})
