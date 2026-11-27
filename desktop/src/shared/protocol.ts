import type { ArtifactPreviewCloseRequest, ArtifactPreviewOpenRequest, ArtifactPreviewRequest, ArtifactPreviewResult } from './artifact-preview'

export type TurnStatus = 'running' | 'completed' | 'cancelled' | 'failed' | 'limited'

export type ApprovalMode = 'request_approval' | 'auto_review' | 'full_access'

export interface ApprovalSettings {
  mode: ApprovalMode
  access: {
    sandbox: 'restricted' | 'full_access'
    writable_roots: string[]
    denied_paths: string[]
    read_only: boolean
    legacy_scope: boolean
    network?: {
      allowed_hosts: string[]
      denied_hosts: string[]
      allow_local_binding: boolean
      decision_timeout_seconds: number
    }
  }
  condition: 'on_request' | 'never'
  reviewer: 'user' | 'assistant' | 'none'
  review_mandatory?: boolean
  revision: string
}

export interface ProviderConfig {
  name: string
  base_url: string
  model: string
  protocol: 'deepseek' | 'openai-chat' | 'google'
  review_model_override?: string | null
  preferred_review_model?: string | null
  available_models?: string[] | null
  model_catalog_url?: string | null
  basic_session_source?: boolean
  review_reasoning_efforts?: string[]
  model_specialty?: string | null
  context_window_override?: number | null
  input_token_limit_override?: number | null
  output_token_reserve?: number
  context_safety_margin?: number
  auto_compaction?: boolean
  compaction_trigger?: number
  compaction_target?: number
  compaction_recent?: number
}

export interface ConnectionStorageState {
  available: ProviderConfig[]
  saved: ProviderConfig[]
  error: string | null
}

export interface GoogleCatalogConnection {
  name: string
  base_url: string
  protocol: 'google'
}

export interface ModelSelection {
  provider_config: ProviderConfig
  reasoning: string
}

export interface ChatModel {
  id: string
  name: string
  capability: 'chat' | 'unknown'
  reasoning_options: { id: string; label: string }[]
  default_reasoning: string
  reasoning_note: string | null
  tools_supported: boolean | null
  unavailable_reason: string | null
}

export interface ModelControlsState {
  selection: ModelSelection
  models: ChatModel[]
  error: string | null
}

export interface ModelCatalogResult {
  request_id: string
  models: { id: string; name: string; capability: 'unknown' }[]
  error: string | null
}

export interface ProviderChange {
  id: string
  task_id: string
  previous: ProviderConfig
  target: ProviderConfig
  materials: string
  compatible: boolean
  reason: string
  approved: boolean | null
}

export interface WireRequestSummary {
  body_sha256: string
  body_bytes: number
  system_sha256: string
  tools_sha256: string
  messages_sha256: string
  message_count: number
  tool_count: number
  message_hashes: string[]
  change_reasons: string[]
}

export interface RequestObservation {
  provider: string
  endpoint: string
  protocol: 'deepseek' | 'openai-chat' | 'google' | 'unknown'
  model: string
  layout_version: number | null
  input_tokens: number | null
  output_tokens: number | null
  thinking_tokens?: number | null
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  input_source: string | null
  output_source: string | null
  thinking_source?: string | null
  cache_read_source: string | null
  cache_write_source: string | null
  usage_present: boolean
  usage_status: 'unknown' | 'partial' | 'complete' | 'conflict'
  raw_usage: Record<string, number>
  field_presence: Record<string, boolean>
  sdk_usage: Record<string, number>
  conflicts: string[]
  elapsed_seconds: number | null
  first_output_seconds: number | null
  finish_reason?: string | null
  wire: WireRequestSummary | null
}

export interface RequestAttempt {
  number: number
  purpose?: 'work' | 'compaction'
  observation?: RequestObservation | null
  own_number?: number | null
  actor?: 'main' | 'delegation' | 'memory_review' | 'action_review'
  model: string
  provider: string
  delegation_id: string | null
  status: 'sent' | 'completed' | 'failed'
  http_status: number | null
  error: string | null
  decision: string
  input_tokens: number | null
  output_tokens: number | null
}

export interface TaskScope {
  root: string
  allowed_actions: ('list' | 'read' | 'search' | 'edit' | 'calculate' | 'check')[]
  provider: 'deepseek' | 'openai-compatible' | 'google'
  recipients: ['deepseek'] | ['openai-compatible'] | ['google']
  checks?: { name: string; argv: string[]; cwd?: string; timeout_seconds?: number }[]
}

export interface RequestAccount {
  request_limit: number | null
  requests?: number
  max_turns?: number | null
  turns_used?: number
  review_requests?: number
  review_seconds?: number
  review_input_tokens?: number | null
  review_output_tokens?: number | null
  review_unmeasured_requests?: number
}

export interface Task extends RequestAccount {
  id: string
  goal: string
  scope: TaskScope | null
  request_policy?: 'independent' | 'shared'
  total_requests?: number
  delegation_max_turns?: number
  active_seconds?: number
  active_seconds_limit?: number | null
  supplements?: Supplement[]
  provider_config?: ProviderConfig
  reasoning?: string | null
  legacy_thinking?: boolean | null
  approval?: ApprovalSettings
}

export interface ReviewRecord {
  request_attempts?: RequestAttempt[]
  request_owner_id?: string | null
  status: 'running' | 'allowed' | 'denied' | 'failed' | 'timeout' | 'cancelled' | 'context_changed'
  risk: 'low' | 'medium' | 'high' | 'critical' | null
  authorization: 'unknown' | 'low' | 'medium' | 'high' | null
  reason: string
  attempts: number
  seconds: number
  input_tokens: number | null
  output_tokens: number | null
  user_override: boolean
  unmeasured_requests?: number
  override_used: boolean
  human_fallback?: boolean
  model: string
  provider: string
}

export interface Supplement {
  id: string
  text: string
  received_at: string
  applied_request: number | null
  selected?: boolean
  revoked_at?: string | null
}

export interface QueuedTask {
  task: Task
  document: string | null
}

export interface SessionSnapshot {
  compactions?: CompactionStatus[]
  model_selection?: ModelSelection | null
  approval_settings?: ApprovalSettings | null
  path: string
  turns: Turn[]
  queue: QueuedTask[]
  running: boolean
  stopping: boolean
  recovery?: RecoveryReport | null
  earlier_recoveries?: RecoveryReport[]
  save_failures?: string[]
  last_saved_at?: string | null
}

export interface CompactionSource {
  message_index: number
  part_index: number
  sha256: string
}

export interface CompactionStatus {
  id: string
  version: number
  status: 'preparing' | 'completed' | 'failed' | 'cancelled'
  trigger: string
  task_id: string
  model: string
  layout_version: number
  replaced_id: string | null
  source: CompactionSource[]
  retained: CompactionSource[]
  covered: CompactionSource[]
  summary: string
  before_tokens: number
  after_tokens: number | null
  input_budget_tokens: number
  measurement_source: string
  target_reached: boolean | null
  request_numbers: number[]
  error: string | null
}

export interface CompactionEvidence {
  summary_id: string
  message_index: number
  part_index: number
  start: number
  text: string
  next_start: number | null
  error: string | null
}

export interface RecoveryReport {
  task_id: string
  required: boolean
  can_resume: boolean
  message: string
  actions: { action_id: string; effect: 'not_started' | 'completed' | 'unknown'; observation: string; checked_at: string }[]
}

export interface FileChange {
  id: string
  path: string
  change_kind: 'created' | 'modified'
  before_sha256: string | null
  after_sha256: string
  size_bytes: number
  backup_path: string | null
}

export interface DeliveredArtifact {
  id: string
  task_id: string
  source_action_id: string
  file_change_id: string
  delegation_id: string | null
  source_record_path: string
  name: string
  path: string
  change_kind: 'created' | 'modified'
  delivered_sha256: string
  size_bytes: number
  delivered_at: string
}

export interface Action {
  id: string
  task_id: string
  kind: string
  target: string
  status: 'started' | 'awaiting_approval' | 'completed' | 'denied' | 'failed' | 'conflict' | 'cancelled'
  started_at: string
  finished_at: string | null
  output: string
  error: string | null
  full_output_path: string | null
  tool_name?: string | null
  tool_arguments?: Record<string, unknown> | null
  tool_source?: string | null
  tool_schema_version?: string | null
  prepared_request?: number
  impact?: string
  approval_id?: string | null
  approval_preview?: string
  approval_basis?: 'task_scope' | 'pending' | 'user' | 'assistant' | 'denied'
  denial_source?: 'user' | 'assistant' | 'scope' | 'review_incomplete' | null
  backup_path?: string | null
  before_sha256?: string | null
  after_sha256?: string | null
  file_changes?: FileChange[]
  prepared_parameters?: Record<string, unknown>
  stdout?: string
  stderr?: string
  exit_code?: number | null
  timed_out?: boolean
  cwd?: string | null
  process_id?: number | null
  effect?: 'not_started' | 'completed' | 'unknown'
  cleanup_complete?: boolean | null
  memory_deferred?: boolean
  delegation_id?: string | null
  approval_settings?: ApprovalSettings | null
  authorization_revision?: string | null
  sandbox_type?: string | null
  review?: ReviewRecord | null
}

export interface Delegation {
  id: string
  goal: string
  context: string
  scope: TaskScope
  evidence_requirements: string
  expected_files: string[]
  record_path: string
  status: TurnStatus
  verification_status: 'pending' | 'passed' | 'gaps'
  verified_files: string[]
  report: string
  error: string | null
  requests: number
  request_limit?: number | null
  max_turns?: number | null
  turns_used?: number
  review_requests?: number
  review_seconds?: number
  review_input_tokens?: number | null
  review_output_tokens?: number | null
  review_unmeasured_requests?: number
  input_tokens: number | null
  output_tokens: number | null
}

export interface Turn {
  execution_facts?: string
  execution_scope?: TaskScope | null
  request_owner_id?: string | null
  request_account?: RequestAccount | null
  approval_settings?: ApprovalSettings | null
  project_trusted?: boolean
  prompt: string
  answer: string
  status: TurnStatus
  error: string | null
  requests: number
  input_tokens: number | null
  output_tokens: number | null
  thinking: boolean
  reasoning?: string | null
  task?: Task | null
  actions?: Action[]
  artifacts?: DeliveredArtifact[]
  context_notes?: string[]
  checks_passed?: boolean | null
  delivery_status?: 'not_delivered' | 'awaiting_acceptance' | 'accepted'
  plan?: string[]
  delegations?: Delegation[]
  skill_catalog?: { name: string; description: string; path: string }[]
  request_attempts?: RequestAttempt[]
  service_failure?: boolean
}

export interface ConversationSummary {
  path: string
  title: string
}

export interface MemoryFact {
  id: string
  text: string
  scope: 'personal' | 'project'
  project: string | null
}

export interface MemoryChange {
  action: 'add' | 'replace' | 'patch' | 'remove'
  text?: string
  old_text?: string
  replacement?: string
  scope?: 'personal' | 'project'
}

export interface MemoryState {
  facts: MemoryFact[]
  approve_writes: boolean
  warnings: string[]
  location: string
}

export type Command =
  | { action: 'get_compaction_evidence'; path: string; summary_id: string; message_index: number; part_index: number; start: number }
  | { action: 'get_connection_storage' }
  | { action: 'forget_connection'; provider_config: ProviderConfig }
  | { action: 'get_model_controls'; path?: string; refresh?: boolean; request_id?: string }
  | { action: 'select_model'; path?: string; model: string; reasoning: string; request_id?: string }
  | { action: 'set_project_trust'; root: string; trusted: boolean }
  | { action: 'configure'; key?: string; request_limit?: number | null; delegation_max_turns?: number; thinking?: boolean; active_seconds_limit?: number | null; provider_config?: ProviderConfig; approval_mode?: ApprovalMode }
  | { action: 'fetch_model_catalog'; request_id: string; key?: string; provider_config: GoogleCatalogConnection }
  | { action: 'preview_provider'; path?: string; task_id: string; provider_config: ProviderConfig }
  | { action: 'decide_provider'; path?: string; change_id: string; approved: boolean }
  | { action: 'memory_list' }
  | { action: 'memory_settings'; approve_writes: boolean }
  | { action: 'memory_change'; path?: string; project?: string; changes: MemoryChange[] }
  | { action: 'send'; prompt: string; document?: string; scope?: TaskScope; path?: string; new_task?: boolean; approval_mode?: ApprovalMode; model_selection?: ModelSelection }
  | { action: 'steer_queued'; path: string; task_id: string; queued_task_id: string }
  | { action: 'submit_supplement'; path: string; task_id: string; supplement_id: string; prompt: string }
  | { action: 'revoke_supplement'; path: string; task_id: string; supplement_id: string }
  | { action: 'set_turn_reviewer'; path?: string; task_id: string; reviewer: 'user' | 'assistant' }
  | { action: 'set_conversation_approval'; path?: string; approval_mode: ApprovalMode }
  | { action: 'new' }
  | { action: 'list'; query: string }
  | { action: 'open'; path: string }
  | { action: 'import'; path: string }
  | { action: 'stop'; path?: string }
  | { action: 'snapshot' }
  | { action: 'verify_recovery' | 'resume_task'; path?: string; task_id: string }
  | { action: 'continue_task'; path?: string; task_id: string; request_limit: number; active_seconds_limit?: number | null }
  | { action: 'start_next'; path?: string }
  | { action: 'approve_action'; path: string; task_id: string; action_id: string; approval_id: string; approved: boolean; active_seconds_limit?: number | null }
  | { action: 'accept_delivery'; path: string; task_id: string }
  | { action: 'retry_denied_action'; path: string; task_id: string; action_id: string }
  | { action: 'shutdown' }

export type ServiceEvent = (
  | ({ event: 'compaction_evidence' } & CompactionEvidence)
  | { event: 'compactions'; compactions: CompactionStatus[] }
  | ({ event: 'connection_storage' } & ConnectionStorageState)
  | ({ event: 'model_controls'; request_id?: string } & ModelControlsState)
  | ({ event: 'model_catalog' } & ModelCatalogResult)
  | { event: 'project_trust'; root: string; trusted: boolean; saved: boolean }
  | ({ event: 'memory' } & MemoryState)
  | { event: 'ready'; path: string }
  | { event: 'configured'; configured?: boolean; provider_config?: ProviderConfig; request_limit?: number | null; delegation_max_turns?: number; active_seconds_limit?: number | null; thinking?: boolean; approval_mode?: ApprovalMode; trusted_projects?: string[] }
  | { event: 'provider_preview'; change: ProviderChange }
  | { event: 'provider_decided'; approved: boolean }
  | { event: 'turn_approval'; path: string; task_id: string; approval_settings: ApprovalSettings; saved: boolean }
  | { event: 'conversation_approval'; path: string; approval_settings: ApprovalSettings; saved: boolean }
  | { event: 'conversation'; path: string; compactions?: CompactionStatus[]; model_selection?: ModelSelection | null; approval_settings?: ApprovalSettings | null; turns: Turn[]; queue?: QueuedTask[]; running?: boolean; stopping?: boolean; recovery?: RecoveryReport | null; earlier_recoveries?: RecoveryReport[]; save_failures?: string[]; last_saved_at?: string | null }
  | { event: 'recovery'; recovery: RecoveryReport; saved: boolean; earlier?: boolean }
  | { event: 'conversations'; items: ConversationSummary[]; unreadable: number }
  | { event: 'started'; prompt: string; task?: Task; approval_settings?: ApprovalSettings | null }
  | { event: 'text' | 'status'; text: string }
  | { event: 'action'; action: Action }
  | { event: 'artifacts'; path: string; task_id: string; artifacts: DeliveredArtifact[]; turn: Turn }
  | { event: 'delivery'; turn: Turn; saved: boolean }
  | { event: 'stopping' }
  | { event: 'finished'; turn: Turn; saved: boolean; path: string; recovery?: RecoveryReport | null; earlier_recoveries?: RecoveryReport[] }
  | { event: 'error'; message: string; fatal?: boolean; supplement_id?: string }
  | { event: 'closed' }
  | { event: 'shutdown_blocked'; message: string }
  | { event: 'task'; task: Task }
  | { event: 'plan'; plan: string[] }
  | { event: 'delegation'; delegation: Delegation }
  | { event: 'skills'; skills: { name: string; description: string; path: string }[] }
  | { event: 'queue'; queue: QueuedTask[]; saved?: boolean }
  | { event: 'supplement'; state: 'received' | 'applied' | 'revoked'; supplement: Supplement; saved: boolean }
  | { event: 'snapshot'; active_path: string; configured: boolean; provider_config?: ProviderConfig; request_limit?: number | null; delegation_max_turns?: number; active_seconds_limit?: number | null; thinking?: boolean; approval_mode?: ApprovalMode; trusted_projects?: string[]; conversations: SessionSnapshot[] }
) & { path?: string; task_id?: string; request_id?: string }

export interface DesktopAPI {
  openArtifactPreview(request: ArtifactPreviewOpenRequest): Promise<ArtifactPreviewResult>
  closeArtifactPreview(request: ArtifactPreviewCloseRequest): Promise<void>
  revealArtifact(request: ArtifactPreviewRequest): Promise<void>
  command(command: Exclude<Command, { action: 'shutdown' | 'import' }>): Promise<void>
  subscribe(listener: (event: ServiceEvent) => void): () => void
  copy(text: string): Promise<void>
  revealFile(path: string): Promise<void>
  importConversation(): Promise<void>
  selectDocument(): Promise<string | null>
  selectWorkspace(): Promise<string | null>
  quit(): Promise<void>
}

declare global {
  interface Window {
    myrk: DesktopAPI
  }
}
