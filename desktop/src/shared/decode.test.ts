import { expect, test } from 'vitest'
import { decodeCommand, decodeEvent } from './decode'

test('explicit supplement commands preserve their identities and exact submitted text', () => {
  const identity = { path: 'records/current.json', task_id: 'active', supplement_id: 'request-1' }
  const submit = { action: 'submit_supplement', ...identity, prompt: ' 补充正文\n' }
  const revoke = { action: 'revoke_supplement', ...identity }
  expect(decodeCommand(submit)).toEqual(submit)
  expect(decodeCommand(revoke)).toEqual(revoke)
  expect(() => decodeCommand({ ...submit, prompt: ' \n' })).toThrow()
  expect(() => decodeCommand({ ...submit, supplement_id: '' })).toThrow()
  expect(() => decodeCommand({ ...revoke, task_id: undefined })).toThrow()
})

test('steering queued work is bound to the current conversation and both tasks', () => {
  const steer = { action: 'steer_queued', path: 'records/current.json', task_id: 'active', queued_task_id: 'waiting' }
  expect(decodeCommand({ ...steer, prompt: 'renderer-supplied replacement' })).toEqual(steer)
  expect(() => decodeCommand({ ...steer, path: ' ' })).toThrow()
  expect(() => decodeCommand({ ...steer, task_id: undefined })).toThrow()
  expect(() => decodeCommand({ ...steer, queued_task_id: '' })).toThrow()
})

test('supplement events decode legacy records, selection, revocation, and correlated errors', () => {
  const supplement = { id: 'request-1', text: '补充正文', received_at: '2026-11-18T00:00:00Z', applied_request: null }
  const identity = { path: 'records/current.json', task_id: 'active' }
  const receipt = { event: 'supplement', state: 'received', saved: true, ...identity, supplement }
  const revoked = { ...receipt, state: 'revoked', supplement: { ...supplement, revoked_at: '2026-11-18T00:00:01Z' } }
  const failure = { event: 'error', message: '未保存补充', ...identity, supplement_id: supplement.id }
  expect(decodeEvent(JSON.stringify(receipt))).toEqual(receipt)
  expect(decodeEvent(JSON.stringify(revoked))).toEqual(revoked)
  expect(decodeEvent(JSON.stringify(failure))).toEqual(failure)

  const task = { id: 'active', goal: '整理资料', scope: null, request_limit: null, supplements: [{ ...supplement, selected: true }] }
  expect(decodeEvent(JSON.stringify({ event: 'task', ...identity, task }))).toMatchObject({ task, ...identity })
  expect(() => decodeEvent(JSON.stringify({ ...receipt, supplement: { ...supplement, selected: 'true' } }))).toThrow()
})

test('capacity configuration rejects invalid values and preserves legacy defaults at the boundary', () => {
  const provider = { name: 'Local', base_url: 'http://127.0.0.1:9001/v1', model: 'offline', protocol: 'openai-chat' }
  const legacy = decodeCommand({ action: 'configure', provider_config: provider })
  expect(legacy).toMatchObject({ provider_config: provider })
  for (const invalid of [{ context_window_override: 0 }, { input_token_limit_override: -1 }, { output_token_reserve: 0 }, { context_safety_margin: 0.5 }, { auto_compaction: 'false' }, { compaction_trigger: 0 }, { compaction_target: 1.1 }, { compaction_recent: true }]) {
    expect(() => decodeCommand({ action: 'configure', provider_config: { ...provider, ...invalid } })).toThrow()
  }
  expect(decodeCommand({ action: 'configure', provider_config: { ...provider, context_window_override: null, input_token_limit_override: null, context_safety_margin: 0, auto_compaction: false } })).toMatchObject({ provider_config: { context_window_override: null, input_token_limit_override: null, context_safety_margin: 0, auto_compaction: false } })
})

test('credential controls expose metadata while restoration stays main-process only', () => {
  const provider = { name: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'deepseek' }
  expect(decodeCommand({ action: 'get_connection_storage' })).toEqual({ action: 'get_connection_storage' })
  expect(decodeCommand({ action: 'forget_connection', provider_config: provider, request_id: 'renderer-untrusted' })).toEqual({ action: 'forget_connection', provider_config: provider })
  expect(decodeEvent(JSON.stringify({ event: 'connection_storage', available: [provider], saved: [provider], error: null, key: 'never-echo' }))).toEqual({ event: 'connection_storage', available: [provider], saved: [provider], error: null })
  expect(decodeEvent(JSON.stringify({ event: 'configured', configured: true, request_id: 'main-owned' }))).toMatchObject({ request_id: 'main-owned' })
  expect(() => decodeCommand({ action: 'restore_connections', connections: [], selected_provider: provider })).toThrow()
})

test('model controls retain the chosen model and exact reasoning value on requests and replies', () => {
  const selection = { provider_config: { name: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'deepseek' }, reasoning: 'high' }
  const event = { event: 'model_controls', path: 'records/current.json', selection, models: [{ id: 'deepseek-flash', name: 'DeepSeek Flash', capability: 'chat', reasoning_options: [{ id: 'low', label: '低' }, { id: 'high', label: '高' }], default_reasoning: 'high', reasoning_note: null, tools_supported: true, unavailable_reason: null }], error: null }
  expect(decodeEvent(JSON.stringify(event))).toEqual(event)
  expect(decodeCommand({ action: 'select_model', path: event.path, model: 'deepseek-flash', reasoning: 'high' })).toEqual({ action: 'select_model', path: event.path, model: 'deepseek-flash', reasoning: 'high' })
  expect(decodeCommand({ action: 'get_model_controls', path: event.path, refresh: true })).toEqual({ action: 'get_model_controls', path: event.path, refresh: true })
  expect(decodeCommand({ action: 'send', path: event.path, prompt: '整理资料', model_selection: selection })).toMatchObject({ model_selection: selection })
  expect(() => decodeEvent(JSON.stringify({ ...event, models: [{ ...event.models[0], tools_supported: 'yes' }] }))).toThrow()
  for (const invalid of [{ default_reasoning: 3 }, { reasoning_note: [] }]) expect(() => decodeEvent(JSON.stringify({ ...event, models: [{ ...event.models[0], ...invalid }] }))).toThrow()
})

test('Google request observations keep raw usage sources and the original finish reason', () => {
  const observation = {
    provider: 'google-gla', endpoint: 'https://generativelanguage.googleapis.com', protocol: 'google', model: 'gemini-3-flash-preview',
    layout_version: 3, input_tokens: 100, output_tokens: 12, thinking_tokens: 0, cache_read_tokens: null, cache_write_tokens: null,
    input_source: 'usageMetadata.promptTokenCount', output_source: 'usageMetadata.candidatesTokenCount + usageMetadata.thoughtsTokenCount',
    thinking_source: 'usageMetadata.thoughtsTokenCount', cache_read_source: null, cache_write_source: null,
    finish_reason: 'MAX_TOKENS', usage_present: true, usage_status: 'partial',
    raw_usage: { promptTokenCount: 100, candidatesTokenCount: 12, thoughtsTokenCount: 0 },
    field_presence: { thoughtsTokenCount: true, cachedContentTokenCount: false }, sdk_usage: { input_tokens: 100, output_tokens: 12 }, conflicts: [],
    elapsed_seconds: 0.2, first_output_seconds: 0.1, wire: null,
  }
  const event = { event: 'finished', path: 'records/google.json', saved: true, turn: {
    prompt: '继续', answer: '部分回答', status: 'failed', error: '达到输出限制', requests: 1, input_tokens: 100, output_tokens: 12, thinking: true,
    request_attempts: [{ number: 1, model: 'gemini-3-flash-preview', provider: 'google-gla', delegation_id: null, status: 'completed', http_status: null, error: null, decision: '', input_tokens: 100, output_tokens: 12, observation }],
  } }
  expect(decodeEvent(JSON.stringify(event))).toMatchObject({ event: 'finished', turn: { request_attempts: [{ observation }] } })
})

test('legacy request observations keep new fields unknown and malformed Google fields are rejected', () => {
  const observation = {
    provider: 'deepseek', endpoint: 'https://api.deepseek.com', protocol: 'deepseek', model: 'deepseek-flash',
    layout_version: 3, input_tokens: 100, output_tokens: 12, cache_read_tokens: 0, cache_write_tokens: null,
    input_source: 'usage.prompt_tokens', output_source: 'usage.completion_tokens', cache_read_source: 'usage.prompt_cache_hit_tokens', cache_write_source: null,
    usage_present: true, usage_status: 'partial', raw_usage: { prompt_tokens: 100 }, field_presence: {}, sdk_usage: {}, conflicts: [],
    elapsed_seconds: 0.2, first_output_seconds: 0.1, wire: null,
  }
  const event = { event: 'finished', path: 'records/old.json', saved: true, turn: {
    prompt: '继续', answer: '旧回答', status: 'completed', error: null, requests: 1, input_tokens: 100, output_tokens: 12, thinking: false,
    request_attempts: [{ number: 1, model: 'deepseek-flash', provider: 'deepseek', delegation_id: null, status: 'completed', http_status: null, error: null, decision: '', input_tokens: 100, output_tokens: 12, observation }],
  } }
  expect(decodeEvent(JSON.stringify(event))).toMatchObject({ event: 'finished', turn: { request_attempts: [{ observation: { thinking_tokens: null, thinking_source: null, finish_reason: null, cache_read_tokens: 0 } }] } })
  for (const invalid of [{ thinking_tokens: -1 }, { thinking_tokens: 0.5 }, { thinking_tokens: true }, { thinking_source: 2 }, { finish_reason: ['STOP'] }]) {
    const attempt = { ...event.turn.request_attempts[0], observation: { ...observation, protocol: 'google', ...invalid } }
    expect(() => decodeEvent(JSON.stringify({ ...event, turn: { ...event.turn, request_attempts: [attempt] } }))).toThrow()
  }
})

test('accepted settings report the actual provider and allowances and reject malformed confirmation fields', () => {
  const accepted = { event: 'configured', provider_config: { name: 'Local', base_url: 'http://127.0.0.1:9001/v1', model: 'local-model', protocol: 'openai-chat' }, request_limit: null, delegation_max_turns: 7, active_seconds_limit: 120, thinking: true }
  expect(decodeEvent(JSON.stringify(accepted))).toEqual({ ...accepted, active_seconds_limit: null })
  for (const invalid of [{ delegation_max_turns: 0 }, { delegation_max_turns: 1.5 }, { active_seconds_limit: -1 }, { thinking: 'yes' }]) expect(() => decodeEvent(JSON.stringify({ ...accepted, ...invalid }))).toThrow()
})

test('connection settings keep explicit limits and thinking and reject unusable limits', () => {
  expect(decodeCommand({ action: 'configure', key: 'test-only', delegation_max_turns: 3, thinking: true })).toEqual({
    action: 'configure', key: 'test-only', delegation_max_turns: 3, thinking: true,
  })
  expect(decodeCommand({ action: 'configure', delegation_max_turns: 250, thinking: false })).toEqual({
    action: 'configure', delegation_max_turns: 250, thinking: false,
  })
  expect(decodeCommand({ action: 'configure', key: 'test-only' })).toEqual({
    action: 'configure', key: 'test-only', delegation_max_turns: 250,
  })
  for (const delegation_max_turns of [0, -1, 1.5, '3', true]) {
    expect(() => decodeCommand({ action: 'configure', key: 'test-only', delegation_max_turns })).toThrow()
  }
  expect(() => decodeCommand({ action: 'configure', key: 'test-only', thinking: 'true' })).toThrow()
})

test('independent turns keep an unlimited main and each delegation distinct from real request attempts', () => {
  const scope = { root: 'E:\\materials', allowed_actions: ['read'], provider: 'deepseek', recipients: ['deepseek'] }
  const result = decodeEvent(JSON.stringify({ event: 'finished', path: 'records/current.json', saved: true, turn: {
    prompt: '交付两份成果', answer: '等待核对', status: 'completed', error: null, requests: 2, input_tokens: null, output_tokens: null, thinking: false,
    task: { id: 'main', goal: '交付两份成果', scope, request_limit: null, max_turns: null, turns_used: 2, requests: 2, request_policy: 'independent', total_requests: 15, delegation_max_turns: 250, active_seconds_limit: null },
    request_owner_id: null, request_account: null,
    delegations: [{ id: 'child', goal: '整理报告', context: '', scope, evidence_requirements: '', expected_files: [], record_path: 'records/child.json', status: 'limited', verification_status: 'pending', verified_files: [], report: '已有部分', error: null, request_limit: null, max_turns: 250, turns_used: 10, requests: 13, review_requests: 2, review_seconds: 1, input_tokens: null, output_tokens: null }],
    request_attempts: [{ number: 15, own_number: 13, actor: 'delegation', model: 'controlled', provider: 'local', delegation_id: 'child', status: 'completed', http_status: null, error: null, decision: '', input_tokens: null, output_tokens: null }],
  } }))
  expect(result).toMatchObject({ event: 'finished', turn: {
    request_owner_id: null, request_account: null,
    task: { request_limit: null, max_turns: null, turns_used: 2, requests: 2, request_policy: 'independent', total_requests: 15, delegation_max_turns: 250, active_seconds_limit: null },
    delegations: [{ request_limit: null, max_turns: 250, turns_used: 10, requests: 13, review_requests: 2 }], request_attempts: [{ number: 15, own_number: 13 }],
  } })
  expect(decodeCommand({ action: 'continue_task', task_id: 'main', request_limit: 20 })).toEqual({ action: 'continue_task', task_id: 'main', request_limit: 20 })
})

test('task scope accepts typed read permissions and rejects new recipients and unsupported actions', () => {
  const scope = { root: 'E:\\materials', allowed_actions: ['list', 'read', 'search'], provider: 'deepseek', recipients: ['deepseek'] }
  expect(decodeCommand({ action: 'send', prompt: '读取规定', scope })).toEqual({ action: 'send', prompt: '读取规定', scope })
  for (const invalid of [
    { ...scope, recipients: ['deepseek', 'unapproved-service'] },
    { ...scope, provider: 'unapproved-service' },
    { ...scope, allowed_actions: ['delete'] },
  ]) {
    expect(() => decodeCommand({ action: 'send', prompt: '读取规定', scope: invalid })).toThrow()
  }
  expect(() => decodeCommand({ action: 'send', prompt: '读取规定', document: 'a.txt', scope })).toThrow()
})


test('legacy request records retain their shared policy while malformed new turn accounts are rejected', () => {
  const task = { id: 'legacy', goal: '旧工作', scope: null, request_limit: 10, requests: 7 }
  expect(decodeEvent(JSON.stringify({ event: 'task', task }))).toMatchObject({ task: { request_policy: 'shared', request_limit: 10, requests: 7, total_requests: 7, max_turns: null, turns_used: 0 } })
  for (const invalid of [{ max_turns: 0 }, { turns_used: -1 }, { total_requests: null }, { delegation_max_turns: 1.5 }, { request_policy: 'other' }]) {
    expect(() => decodeEvent(JSON.stringify({ event: 'task', task: { ...task, ...invalid } }))).toThrow()
  }
})

test.each([
  { tool_name: 'calculate_table', tool_arguments: { path: '统计.csv', column: '金额', filters: { active: true }, groups: ['部门'] }, tool_source: 'extension', tool_schema_version: 'schema-7' },
  { tool_name: null, tool_arguments: null, tool_source: null, tool_schema_version: null },
])('action events preserve the actual tool request and its origin, including empty legacy origin: %j', origin => {
  const action = { id: 'tool-action', task_id: 'task-one', kind: 'calculate', target: '统计.csv', status: 'awaiting_approval', started_at: '2026-11-09', finished_at: null, output: '', error: null, full_output_path: null, ...origin }
  expect(decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', action }))).toEqual({ event: 'action', path: 'records/current.json', action })
})

test('legacy action events without tool origin remain readable', () => {
  const action = { id: 'legacy-action', task_id: 'old-task', kind: 'read', target: '原记录.txt', status: 'completed', started_at: '2026-11-01', finished_at: null, output: '原内容', error: null, full_output_path: null }
  expect(decodeEvent(JSON.stringify({ event: 'action', path: 'records/old.json', action }))).toEqual({ event: 'action', path: 'records/old.json', action })
})

test.each([
  { tool_name: 7 }, { tool_source: ['extension'] }, { tool_schema_version: false },
  { tool_arguments: [] }, { tool_arguments: 'path=统计.csv' }, { tool_arguments: 12 },
])('action events reject malformed actual tool origin at the service boundary: %j', origin => {
  const action = { id: 'invalid-action', task_id: 'task-one', kind: 'calculate', target: '统计.csv', status: 'started', started_at: '2026-11-09', finished_at: null, output: '', error: null, full_output_path: null, ...origin }
  expect(() => decodeEvent(JSON.stringify({ event: 'action', path: 'records/current.json', action }))).toThrow()
})
