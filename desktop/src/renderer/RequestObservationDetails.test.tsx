import { renderToStaticMarkup } from 'react-dom/server'
import { expect, test } from 'vitest'
import type { RequestObservation } from '../shared/protocol'
import { RequestObservationDetails } from './RequestObservationDetails'

test('Google request details distinguish missing counters from zero and explain original sources', () => {
  const observation: RequestObservation = {
    provider: 'google-gla', endpoint: 'https://generativelanguage.googleapis.com', protocol: 'google', model: 'gemini-3-flash-preview',
    layout_version: 3, input_tokens: 100, output_tokens: 12, thinking_tokens: 0, cache_read_tokens: null, cache_write_tokens: null,
    input_source: 'usageMetadata.promptTokenCount', output_source: 'usageMetadata.candidatesTokenCount + usageMetadata.thoughtsTokenCount',
    thinking_source: 'usageMetadata.thoughtsTokenCount', cache_read_source: null, cache_write_source: null,
    finish_reason: 'CONTINUATION', usage_present: true, usage_status: 'partial',
    raw_usage: { promptTokenCount: 100, candidatesTokenCount: 12, thoughtsTokenCount: 0 },
    field_presence: { thoughtsTokenCount: true, cachedContentTokenCount: false }, sdk_usage: { input_tokens: 100, output_tokens: 12 }, conflicts: [],
    elapsed_seconds: 0.2, first_output_seconds: 0.1, wire: null,
  }
  const element = document.createElement('div')
  element.innerHTML = renderToStaticMarkup(<RequestObservationDetails observation={observation} />)

  expect(element.textContent).toContain('协议：Google Gemini')
  expect(element.textContent).toContain('输出总量12；其中思考0')
  expect(element.textContent).toContain('缓存读取未知；缓存写入未知')
  expect(element.textContent).toContain('输出总量包含候选回答和思考；任一计数缺失时，总量保持未知。')
  expect(element.textContent).toContain('思考来源：usageMetadata.thoughtsTokenCount')
  expect(element.textContent).toContain('服务原始结束原因：CONTINUATION')
})

test('Google request details keep missing thinking and combined output unknown even when candidate output exists', () => {
  const observation: RequestObservation = {
    provider: 'google-gla', endpoint: 'https://generativelanguage.googleapis.com', protocol: 'google', model: 'gemini-3-flash-preview',
    layout_version: 3, input_tokens: 100, output_tokens: null, thinking_tokens: null, cache_read_tokens: 0, cache_write_tokens: null,
    input_source: 'usageMetadata.promptTokenCount', output_source: null, thinking_source: null,
    cache_read_source: 'usageMetadata.cachedContentTokenCount', cache_write_source: null, finish_reason: null,
    usage_present: true, usage_status: 'partial', raw_usage: { promptTokenCount: 100, candidatesTokenCount: 12, cachedContentTokenCount: 0 },
    field_presence: { thoughtsTokenCount: false, cachedContentTokenCount: true }, sdk_usage: { input_tokens: 100, output_tokens: 12 }, conflicts: [],
    elapsed_seconds: 0.2, first_output_seconds: 0.1, wire: null,
  }
  const element = document.createElement('div')
  element.innerHTML = renderToStaticMarkup(<RequestObservationDetails observation={observation} />)

  expect(element.textContent).toContain('输出总量未知；其中思考未知')
  expect(element.textContent).toContain('缓存读取0；缓存写入未知')
  expect(element.textContent).toContain('思考来源：未知；服务原始结束原因：未知')
  expect(element.textContent).toContain('candidatesTokenCount12')
})

test('request details explain known layout changes and preserve custom reasons', () => {
  const observation: RequestObservation = {
    provider: 'deepseek', endpoint: 'https://api.deepseek.com', protocol: 'deepseek', model: 'deepseek-flash',
    layout_version: 3, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null,
    input_source: null, output_source: null, cache_read_source: null, cache_write_source: null,
    usage_present: false, usage_status: 'unknown', raw_usage: {}, field_presence: {}, sdk_usage: {}, conflicts: [],
    elapsed_seconds: null, first_output_seconds: null,
    wire: {
      body_sha256: 'body', body_bytes: 100, system_sha256: 'system', tools_sha256: 'tools', messages_sha256: 'messages',
      message_count: 2, tool_count: 1, message_hashes: ['first', 'second'],
      change_reasons: [
        'new_task', 'authorization_changed', 'provider_changed', 'document_changed', 'goal_changed',
        'instructions_changed', 'capacity_compression', 'memory_refresh', 'core_tools_changed',
        'final_turn_tools_removed', 'extension_schema_refresh_required', 'first_request', '自定义变化说明',
      ],
    },
  }
  const element = document.createElement('div')
  element.innerHTML = renderToStaticMarkup(<RequestObservationDetails observation={observation} />)

  expect(element.textContent).toContain('变化原因：新任务；授权范围改变；服务或型号改变；授权资料改变；任务目标改变；指令改变；容量变化，整理待发记录；刷新事实笔记；核心工具定义改变；最后一轮停用动作工具；扩展工具定义需要刷新；首次请求；自定义变化说明。')
})
