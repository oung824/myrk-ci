import type { Action, ApprovalMode, ApprovalSettings, ChatModel, Command, CompactionSource, CompactionStatus, Delegation, MemoryChange, ModelSelection, ProviderChange, ProviderConfig, QueuedTask, RecoveryReport, RequestAccount, RequestAttempt, RequestObservation, ReviewRecord, ServiceEvent, Supplement, Task, TaskScope, Turn, TurnStatus } from './protocol'
import type { DeliveredArtifact, FileChange } from './protocol'

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('Invalid message')
  return value as Record<string, unknown>
}

function string(value: unknown): string {
  if (typeof value !== 'string') throw new Error('Invalid text')
  return value
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error('Invalid text list')
  return value.map(string)
}

function approvalMode(value: unknown): ApprovalMode {
  if (value !== 'request_approval' && value !== 'auto_review' && value !== 'full_access') throw new Error('Invalid approval mode')
  return value
}

function approvalSettings(value: unknown): ApprovalSettings | null {
  if (value === null) return null
  const data = object(value)
  const access = object(data.access)
  if (access.sandbox !== 'restricted' && access.sandbox !== 'full_access') throw new Error('Invalid access restriction')
  if (typeof access.read_only !== 'boolean' || typeof access.legacy_scope !== 'boolean') throw new Error('Invalid access setting')
  if (data.condition !== 'on_request' && data.condition !== 'never') throw new Error('Invalid approval condition')
  if (data.reviewer !== 'user' && data.reviewer !== 'assistant' && data.reviewer !== 'none') throw new Error('Invalid reviewer')
  if (data.review_mandatory !== undefined && typeof data.review_mandatory !== 'boolean') throw new Error('Invalid mandatory review')
  const network = access.network === undefined ? undefined : object(access.network)
  if (network && typeof network.allow_local_binding !== 'boolean') throw new Error('Invalid local network setting')
  return { mode: approvalMode(data.mode), access: { sandbox: access.sandbox, writable_roots: strings(access.writable_roots), denied_paths: strings(access.denied_paths), read_only: access.read_only, legacy_scope: access.legacy_scope,
    ...(network ? { network: { allowed_hosts: strings(network.allowed_hosts), denied_hosts: strings(network.denied_hosts), allow_local_binding: network.allow_local_binding as boolean, decision_timeout_seconds: number(network.decision_timeout_seconds) } } : {}),
  }, condition: data.condition, reviewer: data.reviewer, revision: string(data.revision), ...(data.review_mandatory === undefined ? {} : { review_mandatory: data.review_mandatory }) }
}

function nullableNumber(value: unknown): number | null {
  if (value === null) return null
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error('Invalid number')
  return value
}

function number(value: unknown, minimum = 0, integer = false): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || integer && !Number.isSafeInteger(value)) throw new Error('Invalid number')
  return value
}

function provider(value: unknown): ProviderConfig {
  const data = object(value)
  const name = string(data.name)
  const base_url = string(data.base_url)
  const model = string(data.model)
  const address = new URL(base_url)
  if (!name.trim() || !model.trim() || !['http:', 'https:'].includes(address.protocol) || !address.hostname || address.username || address.password || address.search || address.hash) throw new Error('Invalid service address')
  if (!['deepseek', 'openai-chat', 'google'].includes(data.protocol as string) || data.protocol === 'deepseek' && address.hostname !== 'api.deepseek.com' || data.protocol === 'google' && base_url !== 'https://generativelanguage.googleapis.com') throw new Error('Invalid service protocol')
  if (data.basic_session_source !== undefined && typeof data.basic_session_source !== 'boolean') throw new Error('Invalid service session source')
  if (data.auto_compaction !== undefined && typeof data.auto_compaction !== 'boolean') throw new Error('Invalid automatic compaction setting')
  const fraction = (value: unknown): number => {
    const result = number(value, Number.MIN_VALUE)
    if (result > 1) throw new Error('Invalid compaction ratio')
    return result
  }
  return { name, base_url, model, protocol: data.protocol as ProviderConfig['protocol'],
    ...(data.review_model_override === undefined ? {} : { review_model_override: data.review_model_override === null ? null : string(data.review_model_override) }),
    ...(data.preferred_review_model === undefined ? {} : { preferred_review_model: data.preferred_review_model === null ? null : string(data.preferred_review_model) }),
    ...(data.available_models === undefined ? {} : { available_models: data.available_models === null ? null : strings(data.available_models) }),
    ...(data.model_catalog_url === undefined ? {} : { model_catalog_url: data.model_catalog_url === null ? null : string(data.model_catalog_url) }),
    ...(data.basic_session_source === undefined ? {} : { basic_session_source: data.basic_session_source }),
    ...(data.review_reasoning_efforts === undefined ? {} : { review_reasoning_efforts: strings(data.review_reasoning_efforts) }),
    ...(data.model_specialty === undefined ? {} : { model_specialty: data.model_specialty === null ? null : string(data.model_specialty) }),
    ...(data.context_window_override === undefined ? {} : { context_window_override: data.context_window_override === null ? null : number(data.context_window_override, 1, true) }),
    ...(data.input_token_limit_override === undefined ? {} : { input_token_limit_override: data.input_token_limit_override === null ? null : number(data.input_token_limit_override, 1, true) }),
    ...(data.output_token_reserve === undefined ? {} : { output_token_reserve: number(data.output_token_reserve, 1, true) }),
    ...(data.context_safety_margin === undefined ? {} : { context_safety_margin: number(data.context_safety_margin, 0, true) }),
    ...(data.auto_compaction === undefined ? {} : { auto_compaction: data.auto_compaction }),
    ...(data.compaction_trigger === undefined ? {} : { compaction_trigger: fraction(data.compaction_trigger) }),
    ...(data.compaction_target === undefined ? {} : { compaction_target: fraction(data.compaction_target) }),
    ...(data.compaction_recent === undefined ? {} : { compaction_recent: fraction(data.compaction_recent) }),
  }
}

function providerChange(value: unknown): ProviderChange {
  const data = object(value)
  if (typeof data.compatible !== 'boolean' || data.approved !== null && typeof data.approved !== 'boolean') throw new Error('Invalid receiver approval')
  return { id: string(data.id), task_id: string(data.task_id), previous: provider(data.previous), target: provider(data.target), materials: string(data.materials), compatible: data.compatible, reason: string(data.reason), approved: data.approved }
}

function modelSelection(value: unknown): ModelSelection {
  const data = object(value)
  const reasoning = string(data.reasoning)
  if (!reasoning.trim()) throw new Error('Missing reasoning selection')
  return { provider_config: provider(data.provider_config), reasoning }
}

function chatModel(value: unknown): ChatModel {
  const data = object(value)
  if (!['chat', 'unknown'].includes(data.capability as string) || !Array.isArray(data.reasoning_options) || data.tools_supported !== null && typeof data.tools_supported !== 'boolean') throw new Error('Invalid chat model capability')
  return { id: string(data.id), name: string(data.name), capability: data.capability as ChatModel['capability'], default_reasoning: string(data.default_reasoning), reasoning_note: data.reasoning_note === null ? null : string(data.reasoning_note), tools_supported: data.tools_supported, unavailable_reason: data.unavailable_reason === null ? null : string(data.unavailable_reason), reasoning_options: data.reasoning_options.map(value => {
    const option = object(value)
    return { id: string(option.id), label: string(option.label) }
  }) }
}

function nullableCount(value: unknown): number | null {
  return value === null ? null : number(value, 0, true)
}

function usageCounts(value: unknown): Record<string, number> {
  return Object.fromEntries(Object.entries(object(value)).map(([key, value]) => [key, number(value, 0, true)]))
}

function requestObservation(value: unknown): RequestObservation | null {
  if (value === undefined || value === null) return null
  const data = object(value)
  if (data.protocol !== 'deepseek' && data.protocol !== 'openai-chat' && data.protocol !== 'google' && data.protocol !== 'unknown') throw new Error('Invalid observation protocol')
  if (data.usage_status !== 'unknown' && data.usage_status !== 'partial' && data.usage_status !== 'complete' && data.usage_status !== 'conflict') throw new Error('Invalid usage status')
  if (typeof data.usage_present !== 'boolean') throw new Error('Invalid usage presence')
  const field_presence = Object.fromEntries(Object.entries(object(data.field_presence)).map(([key, value]) => {
    if (typeof value !== 'boolean') throw new Error('Invalid usage field presence')
    return [key, value]
  }))
  const wire = data.wire === null ? null : object(data.wire)
  return {
    provider: string(data.provider), endpoint: string(data.endpoint), protocol: data.protocol, model: string(data.model), layout_version: nullableCount(data.layout_version),
    input_tokens: nullableCount(data.input_tokens), output_tokens: nullableCount(data.output_tokens), cache_read_tokens: nullableCount(data.cache_read_tokens), cache_write_tokens: nullableCount(data.cache_write_tokens),
    thinking_tokens: data.thinking_tokens === undefined ? null : nullableCount(data.thinking_tokens),
    input_source: data.input_source === null ? null : string(data.input_source), output_source: data.output_source === null ? null : string(data.output_source),
    thinking_source: data.thinking_source === undefined || data.thinking_source === null ? null : string(data.thinking_source),
    cache_read_source: data.cache_read_source === null ? null : string(data.cache_read_source), cache_write_source: data.cache_write_source === null ? null : string(data.cache_write_source),
    usage_present: data.usage_present, usage_status: data.usage_status, raw_usage: usageCounts(data.raw_usage), field_presence, sdk_usage: usageCounts(data.sdk_usage), conflicts: strings(data.conflicts),
    elapsed_seconds: data.elapsed_seconds === null ? null : number(data.elapsed_seconds), first_output_seconds: data.first_output_seconds === null ? null : number(data.first_output_seconds),
    finish_reason: data.finish_reason === undefined || data.finish_reason === null ? null : string(data.finish_reason),
    wire: wire === null ? null : {
      body_sha256: string(wire.body_sha256), body_bytes: number(wire.body_bytes, 0, true), system_sha256: string(wire.system_sha256), tools_sha256: string(wire.tools_sha256), messages_sha256: string(wire.messages_sha256),
      message_count: number(wire.message_count, 0, true), tool_count: number(wire.tool_count, 0, true), message_hashes: strings(wire.message_hashes), change_reasons: strings(wire.change_reasons),
    },
  }
}

function attempts(value: unknown): RequestAttempt[] {
  if (value === undefined) return []
  if (!Array.isArray(value)) throw new Error('Invalid service attempts')
  return value.map(value => {
    const data = object(value)
    if (!['sent', 'completed', 'failed'].includes(data.status as string)) throw new Error('Invalid service attempt status')
    if (data.actor !== undefined && !['main', 'delegation', 'memory_review', 'action_review'].includes(data.actor as string)) throw new Error('Invalid service attempt owner')
    if (data.purpose !== undefined && !['work', 'compaction'].includes(data.purpose as string)) throw new Error('Invalid service attempt purpose')
    return { number: number(data.number, 1, true), purpose: data.purpose === undefined ? 'work' : data.purpose as RequestAttempt['purpose'], observation: requestObservation(data.observation), ...(data.own_number === undefined ? {} : { own_number: data.own_number === null ? null : number(data.own_number, 1, true) }), actor: data.actor === undefined ? 'main' : data.actor as RequestAttempt['actor'], model: string(data.model), provider: string(data.provider), delegation_id: data.delegation_id === null ? null : string(data.delegation_id), status: data.status as RequestAttempt['status'], http_status: data.http_status === null ? null : number(data.http_status, 100, true), error: data.error === null ? null : string(data.error), decision: string(data.decision), input_tokens: nullableNumber(data.input_tokens), output_tokens: nullableNumber(data.output_tokens) }
  })
}

function compactions(value: unknown): CompactionStatus[] {
  if (value === undefined) return []
  if (!Array.isArray(value)) throw new Error('Invalid compaction records')
  const sources = (value: unknown): CompactionSource[] => {
    if (!Array.isArray(value)) throw new Error('Invalid compaction sources')
    return value.map(value => {
      const source = object(value)
      const sha256 = string(source.sha256)
      if (!/^[a-f0-9]{64}$/.test(sha256)) throw new Error('Invalid source fingerprint')
      return { message_index: number(source.message_index, 0, true), part_index: number(source.part_index, 0, true), sha256 }
    })
  }
  return value.map(value => {
    const data = object(value)
    if (!['preparing', 'completed', 'failed', 'cancelled'].includes(data.status as string) || data.target_reached !== null && typeof data.target_reached !== 'boolean' || !Array.isArray(data.request_numbers)) throw new Error('Invalid compaction state')
    return { id: string(data.id), version: number(data.version, 1, true), status: data.status as CompactionStatus['status'], trigger: string(data.trigger), task_id: string(data.task_id), model: string(data.model), layout_version: number(data.layout_version, 1, true), replaced_id: data.replaced_id === null ? null : string(data.replaced_id), source: sources(data.source), retained: sources(data.retained), covered: sources(data.covered), summary: string(data.summary), before_tokens: number(data.before_tokens, 0, true), after_tokens: data.after_tokens === null ? null : number(data.after_tokens, 0, true), input_budget_tokens: number(data.input_budget_tokens, 1, true), measurement_source: string(data.measurement_source), target_reached: data.target_reached, request_numbers: data.request_numbers.map(value => number(value, 1, true)), error: data.error === null ? null : string(data.error) }
  })
}

function scope(value: unknown): TaskScope {
  const data = object(value)
  const root = string(data.root)
  if (!root.trim() || !['deepseek', 'openai-compatible', 'google'].includes(data.provider as string) || !Array.isArray(data.recipients) || data.recipients.length !== 1 || data.recipients[0] !== data.provider) throw new Error('Invalid recipient')
  if (!Array.isArray(data.allowed_actions) || !data.allowed_actions.every(action => ['list', 'read', 'search', 'edit', 'calculate', 'check'].includes(action))) throw new Error('Invalid permitted action')
  if (data.checks !== undefined && !Array.isArray(data.checks)) throw new Error('Invalid checks')
  const checks = data.checks === undefined ? undefined : (data.checks as unknown[]).map(value => {
    const check = object(value)
    if (!Array.isArray(check.argv) || !check.argv.length) throw new Error('Missing check command')
    const timeout = check.timeout_seconds === undefined ? 60 : number(check.timeout_seconds, Number.MIN_VALUE)
    if (timeout > 60) throw new Error('Check exceeds 60 seconds')
    return { name: string(check.name), argv: check.argv.map(string), cwd: check.cwd === undefined ? '.' : string(check.cwd), timeout_seconds: timeout }
  })
  return { root, allowed_actions: data.allowed_actions as TaskScope['allowed_actions'], provider: data.provider as TaskScope['provider'], recipients: data.recipients as TaskScope['recipients'], ...(checks === undefined ? {} : { checks }) }
}

function supplement(value: unknown): Supplement {
  const data = object(value)
  if (data.selected !== undefined && typeof data.selected !== 'boolean') throw new Error('Invalid supplement selection')
  return { id: string(data.id), text: string(data.text), received_at: string(data.received_at), applied_request: data.applied_request === null ? null : number(data.applied_request, 1, true),
    ...(data.selected === undefined ? {} : { selected: data.selected }),
    ...(data.revoked_at === undefined ? {} : { revoked_at: data.revoked_at === null ? null : string(data.revoked_at) }) }
}

function task(value: unknown): Task {
  const data = object(value)
  if (data.request_limit === undefined) throw new Error('Missing task request policy limit')
  const account = requestAccount(data)
  if (data.request_policy !== undefined && !['independent', 'shared'].includes(data.request_policy as string)) throw new Error('Invalid request policy')
  if (data.supplements !== undefined && !Array.isArray(data.supplements)) throw new Error('Invalid supplements')
  if (data.legacy_thinking !== undefined && data.legacy_thinking !== null && typeof data.legacy_thinking !== 'boolean') throw new Error('Invalid legacy thinking')
  return {
    id: string(data.id), goal: string(data.goal), scope: data.scope === null ? null : scope(data.scope), ...account,
    request_policy: data.request_policy === undefined ? 'shared' : data.request_policy as Task['request_policy'],
    total_requests: data.total_requests === undefined ? account.requests : number(data.total_requests, 0, true),
    delegation_max_turns: data.delegation_max_turns === undefined ? 250 : number(data.delegation_max_turns, 1, true),
    ...(data.active_seconds === undefined ? {} : { active_seconds: number(data.active_seconds) }),
    ...(data.active_seconds_limit === undefined ? {} : { active_seconds_limit: data.active_seconds_limit === null ? null : number(data.active_seconds_limit, Number.MIN_VALUE) }),
    supplements: data.supplements === undefined ? [] : (data.supplements as unknown[]).map(supplement),
    ...(data.provider_config === undefined ? {} : { provider_config: provider(data.provider_config) }),
    ...(data.reasoning === undefined ? {} : { reasoning: data.reasoning === null ? null : string(data.reasoning) }),
    ...(data.legacy_thinking === undefined ? {} : { legacy_thinking: data.legacy_thinking as boolean | null }),
    ...(data.approval === undefined ? {} : { approval: approvalSettings(data.approval) ?? undefined }),
  }
}

function queue(value: unknown): QueuedTask[] {
  if (value === undefined) return []
  if (!Array.isArray(value)) throw new Error('Invalid queue')
  return value.map(value => { const data = object(value); return { task: task(data.task), document: data.document === null ? null : string(data.document) } })
}

function review(value: unknown): ReviewRecord | null {
  if (value === null) return null
  const data = object(value)
  if (!['running', 'allowed', 'denied', 'failed', 'timeout', 'cancelled', 'context_changed'].includes(data.status as string)) throw new Error('Invalid review status')
  if (data.risk !== null && !['low', 'medium', 'high', 'critical'].includes(data.risk as string)) throw new Error('Invalid review risk')
  if (data.authorization !== null && !['unknown', 'low', 'medium', 'high'].includes(data.authorization as string)) throw new Error('Invalid review authorization')
  if (typeof data.user_override !== 'boolean' || typeof data.override_used !== 'boolean' || data.human_fallback !== undefined && typeof data.human_fallback !== 'boolean') throw new Error('Invalid review permission')
  return { request_attempts: attempts(data.request_attempts), request_owner_id: data.request_owner_id === undefined || data.request_owner_id === null ? null : string(data.request_owner_id), status: data.status as ReviewRecord['status'], risk: data.risk as ReviewRecord['risk'], authorization: data.authorization as ReviewRecord['authorization'], reason: string(data.reason), attempts: number(data.attempts, 0, true), seconds: number(data.seconds), input_tokens: nullableNumber(data.input_tokens), output_tokens: nullableNumber(data.output_tokens), ...(data.unmeasured_requests === undefined ? {} : { unmeasured_requests: number(data.unmeasured_requests, 0, true) }), user_override: data.user_override, override_used: data.override_used, human_fallback: data.human_fallback === true, model: string(data.model), provider: string(data.provider) }
}

function fileChanges(value: unknown): FileChange[] {
  if (!Array.isArray(value)) throw new Error('Invalid file changes')
  return value.map(value => {
    const data = object(value)
    if (data.change_kind !== 'created' && data.change_kind !== 'modified') throw new Error('Invalid file change kind')
    return { id: string(data.id), path: string(data.path), change_kind: data.change_kind,
      before_sha256: data.before_sha256 === null ? null : string(data.before_sha256), after_sha256: string(data.after_sha256),
      size_bytes: number(data.size_bytes, 0, true), backup_path: data.backup_path === null ? null : string(data.backup_path) }
  })
}

function artifacts(value: unknown): DeliveredArtifact[] {
  if (!Array.isArray(value)) throw new Error('Invalid delivered artifacts')
  return value.map(value => {
    const data = object(value)
    if (data.change_kind !== 'created' && data.change_kind !== 'modified') throw new Error('Invalid delivered change kind')
    return { id: string(data.id), task_id: string(data.task_id), source_action_id: string(data.source_action_id),
      file_change_id: string(data.file_change_id), delegation_id: data.delegation_id === null ? null : string(data.delegation_id),
      source_record_path: string(data.source_record_path), name: string(data.name), path: string(data.path), change_kind: data.change_kind,
      delivered_sha256: string(data.delivered_sha256), size_bytes: number(data.size_bytes, 0, true), delivered_at: string(data.delivered_at) }
  })
}

function action(value: unknown): Action {
  const data = object(value)
  if (!['started', 'awaiting_approval', 'completed', 'denied', 'failed', 'conflict', 'cancelled'].includes(data.status as string)) throw new Error('Invalid action')
  if (data.approval_basis !== undefined && !['task_scope', 'pending', 'user', 'assistant', 'denied'].includes(data.approval_basis as string)) throw new Error('Invalid approval basis')
  if (data.denial_source !== undefined && data.denial_source !== null && !['user', 'assistant', 'scope', 'review_incomplete'].includes(data.denial_source as string)) throw new Error('Invalid denial source')
  if (data.timed_out !== undefined && typeof data.timed_out !== 'boolean') throw new Error('Invalid timeout')
  if (data.effect !== undefined && !['not_started', 'completed', 'unknown'].includes(data.effect as string)) throw new Error('Invalid effect')
  if (data.cleanup_complete !== undefined && data.cleanup_complete !== null && typeof data.cleanup_complete !== 'boolean') throw new Error('Invalid cleanup result')
  if (data.memory_deferred !== undefined && typeof data.memory_deferred !== 'boolean') throw new Error('Invalid pending memory')
  return {
    ...(data.tool_name === undefined ? {} : { tool_name: data.tool_name === null ? null : string(data.tool_name) }),
    ...(data.tool_arguments === undefined ? {} : { tool_arguments: data.tool_arguments === null ? null : object(data.tool_arguments) }),
    ...(data.tool_source === undefined ? {} : { tool_source: data.tool_source === null ? null : string(data.tool_source) }),
    ...(data.tool_schema_version === undefined ? {} : { tool_schema_version: data.tool_schema_version === null ? null : string(data.tool_schema_version) }),
    ...(data.prepared_request === undefined ? {} : { prepared_request: number(data.prepared_request, 0, true) }),
    ...(data.prepared_parameters === undefined ? {} : { prepared_parameters: object(data.prepared_parameters) }),
    ...(data.before_sha256 === undefined ? {} : { before_sha256: data.before_sha256 === null ? null : string(data.before_sha256) }),
    ...(data.after_sha256 === undefined ? {} : { after_sha256: data.after_sha256 === null ? null : string(data.after_sha256) }),
    ...(data.file_changes === undefined ? {} : { file_changes: fileChanges(data.file_changes) }),
    id: string(data.id), task_id: string(data.task_id), kind: string(data.kind), target: string(data.target),
    status: data.status as Action['status'], started_at: string(data.started_at),
    finished_at: data.finished_at === null ? null : string(data.finished_at), output: string(data.output),
    error: data.error === null ? null : string(data.error), full_output_path: data.full_output_path === null ? null : string(data.full_output_path),
    ...(data.impact === undefined ? {} : { impact: string(data.impact) }),
    ...(data.approval_id === undefined ? {} : { approval_id: data.approval_id === null ? null : string(data.approval_id) }),
    ...(data.approval_preview === undefined ? {} : { approval_preview: string(data.approval_preview) }),
    ...(data.approval_basis === undefined ? {} : { approval_basis: data.approval_basis as Action['approval_basis'] }),
    ...(data.denial_source === undefined ? {} : { denial_source: data.denial_source as Action['denial_source'] }),
    ...(data.backup_path === undefined ? {} : { backup_path: data.backup_path === null ? null : string(data.backup_path) }),
    ...(data.stdout === undefined ? {} : { stdout: string(data.stdout) }), ...(data.stderr === undefined ? {} : { stderr: string(data.stderr) }),
    ...(data.cwd === undefined ? {} : { cwd: data.cwd === null ? null : string(data.cwd) }),
    ...(data.exit_code === undefined ? {} : { exit_code: nullableNumber(data.exit_code) }),
    ...(data.process_id === undefined ? {} : { process_id: data.process_id === null ? null : number(data.process_id, 1, true) }),
    ...(data.timed_out === undefined ? {} : { timed_out: data.timed_out as boolean }),
    ...(data.effect === undefined ? {} : { effect: data.effect as Action['effect'] }),
    ...(data.cleanup_complete === undefined ? {} : { cleanup_complete: data.cleanup_complete as boolean | null }),
    ...(data.memory_deferred === undefined ? {} : { memory_deferred: data.memory_deferred as boolean }),
    ...(data.delegation_id === undefined ? {} : { delegation_id: data.delegation_id === null ? null : string(data.delegation_id) }),
    ...(data.approval_settings === undefined ? {} : { approval_settings: approvalSettings(data.approval_settings) }),
    ...(data.authorization_revision === undefined ? {} : { authorization_revision: data.authorization_revision === null ? null : string(data.authorization_revision) }),
    ...(data.sandbox_type === undefined ? {} : { sandbox_type: data.sandbox_type === null ? null : string(data.sandbox_type) }),
    ...(data.review === undefined ? {} : { review: review(data.review) }),
  }
}

function delegation(value: unknown): Delegation {
  const data = object(value)
  if (!['running', 'completed', 'cancelled', 'failed', 'limited'].includes(data.status as string) || !['pending', 'passed', 'gaps'].includes(data.verification_status as string)) throw new Error('Invalid delegation state')
  return {
    id: string(data.id), goal: string(data.goal), context: string(data.context), scope: scope(data.scope),
    evidence_requirements: string(data.evidence_requirements), expected_files: strings(data.expected_files),
    record_path: string(data.record_path), status: data.status as TurnStatus, verification_status: data.verification_status as Delegation['verification_status'],
    verified_files: strings(data.verified_files), report: string(data.report), error: data.error === null ? null : string(data.error),
    ...requestAccount(data),
    input_tokens: nullableNumber(data.input_tokens), output_tokens: nullableNumber(data.output_tokens),
  }
}

function requestAccount(data: Record<string, unknown>): RequestAccount & { requests: number } {
  return { request_limit: data.request_limit === null || data.request_limit === undefined ? null : number(data.request_limit, 1, true), requests: number(data.requests === undefined ? 0 : data.requests, 0, true),
    max_turns: data.max_turns === null || data.max_turns === undefined ? null : number(data.max_turns, 1, true), turns_used: number(data.turns_used === undefined ? 0 : data.turns_used, 0, true),
    review_requests: number(data.review_requests === undefined ? 0 : data.review_requests, 0, true), review_seconds: number(data.review_seconds === undefined ? 0 : data.review_seconds),
    review_input_tokens: nullableNumber(data.review_input_tokens ?? null), review_output_tokens: nullableNumber(data.review_output_tokens ?? null),
    review_unmeasured_requests: number(data.review_unmeasured_requests === undefined ? 0 : data.review_unmeasured_requests, 0, true),
  }
}

function skills(value: unknown): NonNullable<Turn['skill_catalog']> {
  if (!Array.isArray(value)) throw new Error('Invalid skill catalog')
  return value.map(value => { const data = object(value); return { name: string(data.name), description: string(data.description), path: string(data.path) } })
}

function turn(value: unknown): Turn {
  const data = object(value)
  const statuses: TurnStatus[] = ['running', 'completed', 'cancelled', 'failed', 'limited']
  if (!statuses.includes(data.status as TurnStatus) || typeof data.thinking !== 'boolean' || typeof data.requests !== 'number') throw new Error('Invalid result')
  if (data.checks_passed !== undefined && data.checks_passed !== null && typeof data.checks_passed !== 'boolean') throw new Error('Invalid check result')
  if (data.delivery_status !== undefined && !['not_delivered', 'awaiting_acceptance', 'accepted'].includes(data.delivery_status as string)) throw new Error('Invalid delivery')
  if (data.service_failure !== undefined && typeof data.service_failure !== 'boolean') throw new Error('Invalid service failure')
  if (data.project_trusted !== undefined && typeof data.project_trusted !== 'boolean') throw new Error('Invalid project trust')
  return {
    execution_facts: data.execution_facts === undefined ? '' : string(data.execution_facts),
    execution_scope: data.execution_scope === undefined || data.execution_scope === null ? null : scope(data.execution_scope),
    request_owner_id: data.request_owner_id === undefined || data.request_owner_id === null ? null : string(data.request_owner_id),
    request_account: data.request_account === undefined || data.request_account === null ? null : requestAccount(object(data.request_account)),
    prompt: string(data.prompt), answer: string(data.answer), status: data.status as TurnStatus,
    ...(data.project_trusted === undefined ? {} : { project_trusted: data.project_trusted }),
    error: data.error === null ? null : string(data.error), requests: data.requests,
    input_tokens: nullableNumber(data.input_tokens), output_tokens: nullableNumber(data.output_tokens), thinking: data.thinking,
    ...(data.reasoning === undefined ? {} : { reasoning: data.reasoning === null ? null : string(data.reasoning) }),
    task: data.task === undefined || data.task === null ? null : task(data.task),
    ...(data.approval_settings === undefined ? {} : { approval_settings: approvalSettings(data.approval_settings) }),
    ...(data.checks_passed === undefined ? {} : { checks_passed: data.checks_passed as boolean | null }),
    ...(data.delivery_status === undefined ? {} : { delivery_status: data.delivery_status as Turn['delivery_status'] }),
    context_notes: data.context_notes === undefined ? [] : (() => { if (!Array.isArray(data.context_notes)) throw new Error("Invalid notes"); return data.context_notes.map(string) })(),
    actions: data.actions === undefined ? [] : (() => {
      if (!Array.isArray(data.actions)) throw new Error('Invalid actions')
      return data.actions.map(action)
    })(),
    ...(data.artifacts === undefined ? {} : { artifacts: artifacts(data.artifacts) }),
    plan: data.plan === undefined ? [] : strings(data.plan),
    delegations: data.delegations === undefined ? [] : (() => {
      if (!Array.isArray(data.delegations)) throw new Error('Invalid delegations')
      return data.delegations.map(delegation)
    })(),
    skill_catalog: data.skill_catalog === undefined ? [] : skills(data.skill_catalog),
    request_attempts: attempts(data.request_attempts),
    ...(data.service_failure === undefined ? {} : { service_failure: data.service_failure }),
  }
}

function recovery(value: unknown): RecoveryReport | null {
  if (value === undefined || value === null) return null
  const data = object(value)
  if (typeof data.required !== 'boolean' || typeof data.can_resume !== 'boolean' || !Array.isArray(data.actions)) throw new Error('Invalid recovery')
  return { task_id: string(data.task_id), required: data.required, can_resume: data.can_resume, message: string(data.message), actions: data.actions.map(value => {
    const item = object(value)
    if (!['not_started', 'completed', 'unknown'].includes(item.effect as string)) throw new Error('Invalid recovery effect')
    return { action_id: string(item.action_id), effect: item.effect as 'not_started' | 'completed' | 'unknown', observation: string(item.observation), checked_at: string(item.checked_at) }
  }) }
}

function earlierRecoveries(value: unknown): RecoveryReport[] {
  if (!Array.isArray(value)) throw new Error('Invalid earlier recoveries')
  return value.map(item => {
    const report = recovery(item)
    if (report === null) throw new Error('Missing earlier recovery')
    return report
  })
}

function savedProgress(data: Record<string, unknown>) {
  if (data.save_failures !== undefined && !Array.isArray(data.save_failures)) throw new Error('Invalid save failures')
  return { recovery: recovery(data.recovery), ...(data.earlier_recoveries === undefined ? {} : { earlier_recoveries: earlierRecoveries(data.earlier_recoveries) }), save_failures: data.save_failures === undefined ? [] : (data.save_failures as unknown[]).map(string), last_saved_at: data.last_saved_at === undefined || data.last_saved_at === null ? null : string(data.last_saved_at) }
}

function acceptedSettings(data: Record<string, unknown>) {
  if (data.delegation_max_turns !== undefined) number(data.delegation_max_turns, 1, true)
  if (data.request_limit !== undefined && data.request_limit !== null && (typeof data.request_limit !== 'number' || !Number.isSafeInteger(data.request_limit) || data.request_limit < 1)) throw new Error('Invalid configured request limit')
  if (data.active_seconds_limit !== undefined && data.active_seconds_limit !== null && (typeof data.active_seconds_limit !== 'number' || !Number.isFinite(data.active_seconds_limit) || data.active_seconds_limit <= 0)) throw new Error('Invalid configured active time')
  if (data.thinking !== undefined && typeof data.thinking !== 'boolean') throw new Error('Invalid configured thinking setting')
  if (data.configured !== undefined && typeof data.configured !== 'boolean') throw new Error('Invalid connection setting')
  return { ...(data.trusted_projects === undefined ? {} : { trusted_projects: strings(data.trusted_projects) }), ...(data.configured === undefined ? {} : { configured: data.configured }), ...(data.approval_mode === undefined ? {} : { approval_mode: approvalMode(data.approval_mode) }), ...(data.provider_config === undefined ? {} : { provider_config: provider(data.provider_config) }), ...(data.request_limit === undefined ? {} : { request_limit: data.request_limit }), ...(data.delegation_max_turns === undefined ? {} : { delegation_max_turns: data.delegation_max_turns as number }), ...(data.active_seconds_limit === undefined ? {} : { active_seconds_limit: null }), ...(data.thinking === undefined ? {} : { thinking: data.thinking }) }
}

export function decodeCommand(value: unknown): Exclude<Command, { action: 'shutdown' | 'import' }> {
  const data = object(value)
  switch (data.action) {
    case 'get_connection_storage': return { action: 'get_connection_storage' }
    case 'forget_connection': return { action: 'forget_connection', provider_config: provider(data.provider_config) }
    case 'get_model_controls':
      if (data.refresh !== undefined && typeof data.refresh !== 'boolean') throw new Error('Invalid model refresh')
      return { action: 'get_model_controls', ...(data.request_id === undefined ? {} : { request_id: string(data.request_id) }), ...(data.path === undefined ? {} : { path: string(data.path) }), ...(data.refresh === undefined ? {} : { refresh: data.refresh }) }
    case 'select_model': {
      const model = string(data.model).trim()
      const reasoning = string(data.reasoning).trim()
      if (!model || !reasoning) throw new Error('Missing model selection')
      return { action: 'select_model', model, reasoning, ...(data.request_id === undefined ? {} : { request_id: string(data.request_id) }), ...(data.path === undefined ? {} : { path: string(data.path) }) }
    }
    case 'fetch_model_catalog': {
      const connection = object(data.provider_config)
      const name = string(connection.name)
      const request_id = string(data.request_id)
      const key = data.key === undefined ? undefined : string(data.key).trim()
      if (!name.trim() || !request_id.trim() || key === '' || connection.protocol !== 'google' || connection.base_url !== 'https://generativelanguage.googleapis.com') throw new Error('Invalid model catalog connection')
      return { action: 'fetch_model_catalog', request_id, ...(key === undefined ? {} : { key }), provider_config: { name, base_url: connection.base_url, protocol: 'google' } }
    }
    case 'set_project_trust': {
      const root = string(data.root)
      if (typeof data.trusted !== 'boolean' || !root.trim()) throw new Error('Invalid project trust')
      return { action: 'set_project_trust', root, trusted: data.trusted }
    }
    case 'memory_list': return { action: 'memory_list' }
    case 'memory_settings':
      if (typeof data.approve_writes !== 'boolean') throw new Error('Invalid note approval setting')
      return { action: 'memory_settings', approve_writes: data.approve_writes }
    case 'memory_change':
      if (!Array.isArray(data.changes) || !data.changes.length) throw new Error('Missing note changes')
      return { action: 'memory_change', ...(data.path === undefined ? {} : { path: string(data.path) }), ...(data.project === undefined ? {} : { project: string(data.project) }), changes: data.changes.map(value => {
        const change = object(value)
        if (!['add', 'replace', 'patch', 'remove'].includes(change.action as string) || change.scope !== undefined && !['personal', 'project'].includes(change.scope as string)) throw new Error('Invalid note change')
        return { action: change.action as MemoryChange['action'], ...(change.scope === undefined ? {} : { scope: change.scope as MemoryChange['scope'] }), ...(change.text === undefined ? {} : { text: string(change.text) }), ...(change.old_text === undefined ? {} : { old_text: string(change.old_text) }), ...(change.replacement === undefined ? {} : { replacement: string(change.replacement) }) }
      }) }
    case 'configure': {
      const approval_mode = data.approval_mode === undefined ? undefined : approvalMode(data.approval_mode)
      if (approval_mode !== undefined && data.key === undefined && data.provider_config === undefined && data.request_limit === undefined && data.delegation_max_turns === undefined && data.active_seconds_limit === undefined && data.thinking === undefined) return { action: 'configure', approval_mode }
      const key = data.key === undefined ? undefined : string(data.key).trim()
      if (key === '') throw new Error('Missing key')
      const delegation_max_turns = data.delegation_max_turns === undefined ? 250 : number(data.delegation_max_turns, 1, true)
      const thinking = data.thinking
      if (data.request_limit !== undefined && data.request_limit !== null) number(data.request_limit, 1, true)
      if (thinking !== undefined && typeof thinking !== 'boolean') throw new Error('Invalid thinking setting')
      if (data.active_seconds_limit !== undefined && data.active_seconds_limit !== null && (typeof data.active_seconds_limit !== 'number' || !Number.isFinite(data.active_seconds_limit) || data.active_seconds_limit <= 0)) throw new Error('Invalid active time')
      return { ...(data.active_seconds_limit === undefined ? {} : { active_seconds_limit: null }), action: 'configure', ...(key === undefined ? {} : { key }), ...(data.request_limit === undefined ? {} : { request_limit: data.request_limit as number | null }), delegation_max_turns, ...(thinking === undefined ? {} : { thinking }), ...(data.provider_config === undefined ? {} : { provider_config: provider(data.provider_config) }), ...(approval_mode === undefined ? {} : { approval_mode }) }
    }
    case 'send': {
      const prompt = string(data.prompt).trim()
      if (!prompt) throw new Error('Missing prompt')
      const document = data.document === undefined ? undefined : string(data.document)
      if (document !== undefined && !document.trim()) throw new Error('Missing document')
      if (document !== undefined && data.scope !== undefined) throw new Error('Choose one source')
      if (data.new_task !== undefined && typeof data.new_task !== 'boolean') throw new Error('Invalid new task')
      return { action: 'send', prompt, ...(data.model_selection === undefined ? {} : { model_selection: modelSelection(data.model_selection) }), ...(data.approval_mode === undefined ? {} : { approval_mode: approvalMode(data.approval_mode) }), ...(data.path === undefined ? {} : { path: string(data.path) }), ...(data.new_task === undefined ? {} : { new_task: data.new_task }), ...(document === undefined ? {} : { document }), ...(data.scope === undefined ? {} : { scope: scope(data.scope) }) }
    }
    case 'steer_queued': {
      const path = string(data.path)
      const task_id = string(data.task_id)
      const queued_task_id = string(data.queued_task_id)
      if (!path.trim() || !task_id.trim() || !queued_task_id.trim()) throw new Error('Missing queued steer identity')
      return { action: 'steer_queued', path, task_id, queued_task_id }
    }
    case 'submit_supplement': case 'revoke_supplement': {
      const path = string(data.path)
      const task_id = string(data.task_id)
      const supplement_id = string(data.supplement_id)
      if (!path.trim() || !task_id.trim() || !supplement_id.trim()) throw new Error('Missing supplement identity')
      if (data.action === 'revoke_supplement') return { action: 'revoke_supplement', path, task_id, supplement_id }
      const prompt = string(data.prompt)
      if (!prompt.trim()) throw new Error('Missing supplement text')
      return { action: 'submit_supplement', path, task_id, supplement_id, prompt }
    }
    case 'set_turn_reviewer':
      if (data.reviewer !== 'user' && data.reviewer !== 'assistant') throw new Error('Invalid turn reviewer')
      return { action: 'set_turn_reviewer', task_id: string(data.task_id), reviewer: data.reviewer, ...(data.path === undefined ? {} : { path: string(data.path) }) }
    case 'set_conversation_approval': return { action: 'set_conversation_approval', ...(data.path === undefined ? {} : { path: string(data.path) }), approval_mode: approvalMode(data.approval_mode) }
    case 'new': return { action: 'new' }
    case 'list': return { action: 'list', query: string(data.query) }
    case 'open': return { action: 'open', path: string(data.path) }
    case 'stop': case 'start_next': return { action: data.action, ...(data.path === undefined ? {} : { path: string(data.path) }) }
    case 'snapshot': return { action: 'snapshot' }
    case 'get_compaction_evidence': return { action: 'get_compaction_evidence', path: string(data.path), summary_id: string(data.summary_id), message_index: number(data.message_index, 0, true), part_index: number(data.part_index, 0, true), start: number(data.start, 0, true) }
    case 'preview_provider': return { action: 'preview_provider', task_id: string(data.task_id), provider_config: provider(data.provider_config), ...(data.path === undefined ? {} : { path: string(data.path) }) }
    case 'decide_provider':
      if (typeof data.approved !== 'boolean') throw new Error('Invalid receiver decision')
      return { action: 'decide_provider', change_id: string(data.change_id), approved: data.approved, ...(data.path === undefined ? {} : { path: string(data.path) }) }
    case 'verify_recovery': case 'resume_task': return { action: data.action, task_id: string(data.task_id), ...(data.path === undefined ? {} : { path: string(data.path) }) }
    case 'approve_action':
      if (typeof data.approved !== 'boolean') throw new Error('Invalid approval')
      return { action: 'approve_action', path: string(data.path), task_id: string(data.task_id), action_id: string(data.action_id), approval_id: string(data.approval_id), approved: data.approved, ...(data.active_seconds_limit === undefined ? {} : { active_seconds_limit: data.active_seconds_limit === null ? null : number(data.active_seconds_limit, Number.MIN_VALUE) }) }
    case 'accept_delivery': return { action: 'accept_delivery', path: string(data.path), task_id: string(data.task_id) }
    case 'retry_denied_action': return { action: 'retry_denied_action', path: string(data.path), task_id: string(data.task_id), action_id: string(data.action_id) }
    case 'continue_task':
      if (typeof data.request_limit !== 'number' || !Number.isSafeInteger(data.request_limit) || data.request_limit < 1) throw new Error('Invalid increase')
      return { action: 'continue_task', task_id: string(data.task_id), request_limit: data.request_limit, ...(data.active_seconds_limit === undefined ? {} : { active_seconds_limit: data.active_seconds_limit === null ? null : number(data.active_seconds_limit, Number.MIN_VALUE) }), ...(data.path === undefined ? {} : { path: string(data.path) }) }
    default: throw new Error('Unknown action')
  }
}

export function decodeEvent(line: string): ServiceEvent {
  const data = object(JSON.parse(line))
  const requestId = data.request_id === undefined ? undefined : string(data.request_id)
  return { ...event(data, requestId), ...(requestId === undefined ? {} : { request_id: requestId }), ...(data.path === undefined ? {} : { path: string(data.path) }), ...(data.task_id === undefined || data.task_id === null ? {} : { task_id: string(data.task_id) }) }
}

function event(data: Record<string, unknown>, requestId: string | undefined): ServiceEvent {
  switch (data.event) {
    case 'connection_storage':
      if (!Array.isArray(data.available) || !Array.isArray(data.saved)) throw new Error('Invalid connection storage state')
      return { event: 'connection_storage', available: data.available.map(provider), saved: data.saved.map(provider), error: data.error === null ? null : string(data.error) }
    case 'model_controls':
      if (!Array.isArray(data.models)) throw new Error('Invalid model controls')
      return { event: 'model_controls', selection: modelSelection(data.selection), models: data.models.map(chatModel), error: data.error === null ? null : string(data.error) }
    case 'model_catalog': {
      if (!Array.isArray(data.models) || requestId === undefined) throw new Error('Invalid model catalog')
      return { event: 'model_catalog', request_id: requestId, error: data.error === null ? null : string(data.error), models: data.models.map(value => {
        const item = object(value)
        if (item.capability !== 'unknown') throw new Error('Invalid model capability')
        return { id: string(item.id), name: string(item.name), capability: 'unknown' }
      }) }
    }
    case 'project_trust':
      if (typeof data.trusted !== 'boolean' || typeof data.saved !== 'boolean') throw new Error('Invalid project trust')
      return { event: 'project_trust', root: string(data.root), trusted: data.trusted, saved: data.saved }
    case 'memory':
      if (!Array.isArray(data.facts) || !Array.isArray(data.warnings) || typeof data.approve_writes !== 'boolean') throw new Error('Invalid notes')
      return { event: 'memory', approve_writes: data.approve_writes, location: string(data.location), warnings: data.warnings.map(string), facts: data.facts.map(value => {
        const fact = object(value)
        if (!['personal', 'project'].includes(fact.scope as string)) throw new Error('Invalid note scope')
        return { id: string(fact.id), text: string(fact.text), scope: fact.scope as 'personal' | 'project', project: fact.project === null ? null : string(fact.project) }
      }) }
    case 'ready': return { event: 'ready', path: string(data.path) }
    case 'configured': return { event: 'configured', ...acceptedSettings(data) }
    case 'provider_preview': return { event: 'provider_preview', change: providerChange(data.change) }
    case 'provider_decided':
      if (typeof data.approved !== 'boolean') throw new Error('Invalid receiver decision')
      return { event: 'provider_decided', approved: data.approved }
    case 'turn_approval': {
      const approval_settings = approvalSettings(data.approval_settings)
      if (approval_settings === null || typeof data.saved !== 'boolean') throw new Error('Invalid saved turn approval')
      return { event: 'turn_approval', path: string(data.path), task_id: string(data.task_id), approval_settings, saved: data.saved }
    }
    case 'conversation_approval': {
      const approval_settings = approvalSettings(data.approval_settings)
      if (approval_settings === null || typeof data.saved !== 'boolean') throw new Error('Invalid saved conversation approval')
      return { event: 'conversation_approval', path: string(data.path), approval_settings, saved: data.saved }
    }
    case 'compactions': return { event: 'compactions', compactions: compactions(data.compactions) }
    case 'compaction_evidence': return { event: 'compaction_evidence', summary_id: string(data.summary_id), message_index: number(data.message_index, 0, true), part_index: number(data.part_index, 0, true), start: number(data.start, 0, true), text: string(data.text), next_start: data.next_start === null ? null : number(data.next_start, 1, true), error: data.error === null ? null : string(data.error) }
    case 'conversation':
      if (data.running !== undefined && typeof data.running !== 'boolean' || data.stopping !== undefined && typeof data.stopping !== 'boolean') throw new Error('Invalid running state')
      if (!Array.isArray(data.turns)) throw new Error('Missing turns')
      return { event: 'conversation', path: string(data.path), compactions: compactions(data.compactions), ...(data.model_selection === undefined ? {} : { model_selection: data.model_selection === null ? null : modelSelection(data.model_selection) }), ...(data.approval_settings === undefined ? {} : { approval_settings: approvalSettings(data.approval_settings) }), turns: data.turns.map(turn), queue: queue(data.queue), running: data.running === true, stopping: data.stopping === true, ...savedProgress(data) }
    case 'recovery': {
      const report = recovery(data.recovery)
      if (report === null || typeof data.saved !== 'boolean' || data.earlier !== undefined && typeof data.earlier !== 'boolean') throw new Error('Invalid recovery result')
      return { event: 'recovery', recovery: report, saved: data.saved, ...(data.earlier === undefined ? {} : { earlier: data.earlier }) }
    }
    case 'conversations':
      if (!Array.isArray(data.items) || typeof data.unreadable !== 'number') throw new Error('Missing conversations')
      return { event: 'conversations', items: data.items.map(value => {
        const item = object(value)
        return { path: string(item.path), title: string(item.title) }
      }), unreadable: data.unreadable }
    case 'started': return { event: 'started', prompt: string(data.prompt), ...(data.task === undefined ? {} : { task: task(data.task) }), ...(data.approval_settings === undefined ? {} : { approval_settings: approvalSettings(data.approval_settings) }) }
    case 'text': case 'status': return { event: data.event, text: string(data.text) }
    case 'action': return { event: 'action', action: action(data.action) }
    case 'artifacts': return { event: 'artifacts', path: string(data.path), task_id: string(data.task_id), artifacts: artifacts(data.artifacts), turn: turn(data.turn) }
    case 'delivery':
      if (typeof data.saved !== 'boolean') throw new Error('Missing save state')
      return { event: 'delivery', turn: turn(data.turn), saved: data.saved }
    case 'stopping': return { event: 'stopping' }
    case 'finished':
      if (typeof data.saved !== 'boolean') throw new Error('Missing save state')
      return { event: 'finished', turn: turn(data.turn), path: string(data.path), saved: data.saved,
        ...(data.recovery === undefined ? {} : { recovery: recovery(data.recovery) }),
        ...(data.earlier_recoveries === undefined ? {} : { earlier_recoveries: earlierRecoveries(data.earlier_recoveries) }) }
    case 'error':
      if (data.fatal !== undefined && typeof data.fatal !== 'boolean') throw new Error('Invalid failure state')
      return { event: 'error', message: string(data.message), ...(data.fatal === undefined ? {} : { fatal: data.fatal }),
        ...(data.supplement_id === undefined ? {} : { supplement_id: string(data.supplement_id) }) }
    case 'task': return { event: 'task', task: task(data.task) }
    case 'plan': return { event: 'plan', plan: strings(data.plan) }
    case 'delegation': return { event: 'delegation', delegation: delegation(data.delegation) }
    case 'skills': return { event: 'skills', skills: skills(data.skills) }
    case 'queue': return { event: 'queue', queue: queue(data.queue), ...(typeof data.saved === 'boolean' ? { saved: data.saved } : {}) }
    case 'supplement':
      if ((data.state !== 'received' && data.state !== 'applied' && data.state !== 'revoked') || typeof data.saved !== 'boolean') throw new Error('Invalid supplement')
      return { event: 'supplement', state: data.state, supplement: supplement(data.supplement), saved: data.saved }
    case 'snapshot':
      if (!Array.isArray(data.conversations) || typeof data.configured !== 'boolean') throw new Error('Invalid snapshot')
      return { event: 'snapshot', configured: data.configured, ...acceptedSettings(data), active_path: string(data.active_path), conversations: data.conversations.map(value => {
        const data = object(value)
        if (!Array.isArray(data.turns) || typeof data.running !== 'boolean' || typeof data.stopping !== 'boolean') throw new Error('Invalid session')
        return { path: string(data.path), compactions: compactions(data.compactions), ...(data.model_selection === undefined ? {} : { model_selection: data.model_selection === null ? null : modelSelection(data.model_selection) }), ...(data.approval_settings === undefined ? {} : { approval_settings: approvalSettings(data.approval_settings) }), turns: data.turns.map(turn), queue: queue(data.queue), running: data.running, stopping: data.stopping, ...savedProgress(data) }
      }) }
    case 'closed': return { event: 'closed' }
    case 'shutdown_blocked': return { event: 'shutdown_blocked', message: string(data.message) }
    default: throw new Error('Unknown event')
  }
}
