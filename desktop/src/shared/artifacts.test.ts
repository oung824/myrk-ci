import { expect, test } from 'vitest'
import { decodeEvent } from './decode'

test('a saved delivery event preserves its observed file change, source action and owning turn', () => {
  const change = { id: 'change-one', path: 'E:\\work\\report.md', change_kind: 'created', before_sha256: null, after_sha256: 'a'.repeat(64), size_bytes: 12, backup_path: null }
  const artifact = { id: 'delivered-one', task_id: 'task-one', source_action_id: 'write-one', file_change_id: change.id, delegation_id: null, source_record_path: 'records/current.json', name: 'report.md', path: change.path, change_kind: 'created', delivered_sha256: change.after_sha256, size_bytes: 12, delivered_at: '2026-11-14T00:00:00Z' }
  const action = { id: 'write-one', task_id: 'task-one', kind: 'create', target: change.path, status: 'completed', started_at: '2026-11-14', finished_at: '2026-11-14', output: '', error: null, full_output_path: null, effect: 'completed', before_sha256: null, after_sha256: change.after_sha256, file_changes: [change], prepared_parameters: { path: change.path } }
  const turn = { prompt: '生成报告', answer: '报告已交付。', status: 'running', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false, actions: [action], artifacts: [artifact] }
  expect(decodeEvent(JSON.stringify({ event: 'artifacts', path: 'records/current.json', task_id: 'task-one', artifacts: [artifact], turn }))).toMatchObject({
    event: 'artifacts', path: 'records/current.json', task_id: 'task-one', artifacts: [artifact], turn: { artifacts: [artifact], actions: [action] },
  })
})
