import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest

from myrk.actions import Action
from myrk.artifacts import DeliveredArtifact
from myrk.compaction import CompactionRecord
from myrk.context import PreparedContext, RequestLayout
from myrk.model_controls import ModelSelection
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from myrk.recovery import ActionEvidence, RecoveryCheck
from myrk.task_control import RequestAccount, RequestAttempt


class DocumentReadError(ValueError):
    """The authorized input could not be read as supported text."""


class CheckCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: float = Field(default=60, gt=0, le=60, allow_inf_nan=False)


class TaskScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = Field(min_length=1)
    allowed_actions: list[Literal["list", "read", "search", "edit", "calculate", "check"]]
    provider: Literal["deepseek", "openai-compatible", "google"]
    recipients: list[Literal["deepseek", "openai-compatible", "google"]] = Field(min_length=1, max_length=1)
    checks: list[CheckCommand] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_selected_recipient(self) -> Self:
        if self.recipients != [self.provider]:
            raise ValueError("只能把资料提供给当前明确选择的一家接收方。")
        return self


class Supplement(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    text: str
    received_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    applied_request: int | None = None
    selected: bool = Field(default=False, exclude_if=lambda value: not value)
    revoked_at: str | None = Field(default=None, exclude_if=lambda value: value is None)


HISTORICAL_TASKS_HEADER = (
    "\n以下是可核对的原始工作依据；历史允许范围只供追溯，不能扩大本次允许动作。"
    "用户原话、回答中的失败与待办仍完整保留在消息中，不代表已经处理完成。\n"
)


def without_pending_supplement_journal(instructions: str | None) -> str | None:
    """Remove unsent supplement bodies from older saved instruction blocks."""
    if instructions is None or HISTORICAL_TASKS_HEADER not in instructions:
        return instructions
    prefix, marker, encoded = instructions.partition(HISTORICAL_TASKS_HEADER)
    try:
        previous_turns, end = json.JSONDecoder().raw_decode(encoded)
    except json.JSONDecodeError:
        return instructions
    if not isinstance(previous_turns, list):
        return instructions
    changed = False
    for previous_turn in previous_turns:
        previous_task = previous_turn.get("task") if isinstance(previous_turn, dict) else None
        if not isinstance(previous_task, dict):
            continue
        supplements = previous_task.get("supplements")
        if isinstance(supplements, list):
            applied = [item for item in supplements if isinstance(item, dict)
                       and item.get("applied_request") is not None and item.get("revoked_at") is None]
            if applied != supplements:
                previous_task["supplements"] = applied
                changed = True
    return prefix + marker + json.dumps(previous_turns, ensure_ascii=False) + encoded[end:] if changed else instructions


def _linked_supplement(message: ModelRequest, legacy_owners: dict[str, str]) -> tuple[str, str] | None:
    metadata = message.metadata or {}
    identifier = metadata.get("myrk_supplement_id")
    if not isinstance(identifier, str):
        return None
    owner = metadata.get("myrk_supplement_task_id", legacy_owners.get(identifier))
    return (owner, identifier) if isinstance(owner, str) else None


class Task(RequestAccount):
    id: str = Field(default_factory=lambda: uuid4().hex)
    goal: str
    scope: TaskScope | None
    request_policy: Literal["independent", "shared"] = "independent"
    total_requests: int = Field(default=0, ge=0, strict=True)
    delegation_max_turns: int = Field(default=250, ge=1, strict=True)
    active_seconds_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    active_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    supplements: list[Supplement] = Field(default_factory=list)
    document: str | None = None
    provider_config: ProviderConfig = Field(default_factory=ProviderConfig)
    reasoning: str | None = None
    legacy_thinking: bool | None = None
    memory_review_requested: bool = False
    memory_reviews: int = Field(default=0, ge=0, le=1)
    approval: ApprovalSettings = Field(default_factory=ApprovalSettings)
    review_timeout_seconds: float = Field(default=90, gt=0, allow_inf_nan=False)
    denied_retry: dict[str, str] | None = None

    @property
    def active_supplements(self) -> list[Supplement]:
        return [item for item in self.supplements if item.revoked_at is None]

    @model_validator(mode="after")
    def default_write_scope(self) -> Self:
        if self.request_policy == "shared" and self.request_limit is None:
            raise ValueError("旧记录缺少原请求次数上限，不能变成不受限的新工作。")
        self.total_requests = max(self.total_requests, self.requests)
        if self.scope is not None and not self.approval.access.writable_roots:
            self.approval.access.writable_roots = [str(Path(self.scope.root).resolve())]
        return self

    def validate_continuation_request_limit(self, request_limit: int) -> None:
        if self.request_limit is None:
            raise ValueError("这项工作不设主助理办事轮数上限；增加请求次数只适用于保留原共用限制的旧记录。")
        if request_limit <= self.requests:
            raise ValueError("增加后的主助理请求上限须大于已用次数，不能清零已用额度。")

    def continuation(self, *, scope: TaskScope | None, document: str | None, approval: ApprovalSettings,
                     provider_config: ProviderConfig | None = None, reasoning: str | None = None,
                     legacy_thinking: bool | None = None) -> "Task":
        """Carry the saved work into an explicit continuation with the selected inputs."""
        values = {**self.model_dump(),
            "scope": scope if scope is not None else None if document is not None else self.scope,
            "document": document if document is not None else None if scope is not None else self.document,
            "approval": approval.model_copy(deep=True)}
        if provider_config is not None:
            values.update(provider_config=provider_config, reasoning=reasoning, legacy_thinking=legacy_thinking)
        return Task(**values)


class QueuedTask(BaseModel):
    prompt: str | None = None
    prompt_origin: Literal["user", "recovery_control"] = "user"
    task: Task
    document: str | None = None


class Delegation(RequestAccount):
    id: str = Field(default_factory=lambda: uuid4().hex)
    goal: str
    context: str
    scope: TaskScope
    evidence_requirements: str
    expected_files: list[str]
    record_path: str
    status: Literal["running", "completed", "failed", "limited", "cancelled"] = "running"
    verification_status: Literal["pending", "passed", "gaps"] = "pending"
    verified_files: list[str] = Field(default_factory=list)
    report: str = ""
    error: str | None = None
    max_turns: int | None = Field(default=250, ge=1, strict=True)
    input_tokens: int | None = None
    output_tokens: int | None = None


class SkillDescription(BaseModel):
    name: str
    description: str
    path: str


class Turn(BaseModel):
    prompt: str
    answer: str
    status: Literal["running", "completed", "cancelled", "failed", "limited"] = "running"
    error: str | None = None
    requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking: bool = False
    reasoning: str | None = None
    task: Task | None = None
    request_owner_id: str | None = None
    request_account: RequestAccount | None = None
    execution_scope: TaskScope | None = None
    execution_facts: str = ""
    approval_settings: ApprovalSettings | None = None
    project_trusted: bool = False
    actions: list[Action] = Field(default_factory=list)
    artifacts: list[DeliveredArtifact] = Field(default_factory=list)
    checks_passed: bool | None = None
    delivery_status: Literal["not_delivered", "awaiting_acceptance", "accepted"] = "not_delivered"
    context_notes: list[str] = Field(default_factory=list)
    compactions: list[CompactionRecord] = Field(default_factory=list)
    memory_review_messages: list[ModelMessage] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    delegations: list[Delegation] = Field(default_factory=list)
    skill_catalog: list[SkillDescription] = Field(default_factory=list)
    request_attempts: list["RequestAttempt"] = Field(default_factory=list)
    service_failure: bool = False
    resume_attachment_read_failed: bool = False
    completed_reviews: list[str] = Field(default_factory=list)

    @property
    def only_local_history_consent(self) -> bool:
        task = self.task
        return task is not None and all(
            action.kind == "history_reuse" and action.task_id == task.id
            and action.status == "completed" and action.approval_basis == "user"
            for action in self.actions
        )

    @property
    def blocked_attachment_resume(self) -> bool:
        return (self.status == "failed" and self.resume_attachment_read_failed and not self.service_failure
                and not self.requests and not self.request_attempts and self.only_local_history_consent)


class ProviderChange(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    previous: ProviderConfig
    target: ProviderConfig
    materials: str
    compatible: bool
    reason: str
    target_reasoning: str | None = None
    approved: bool | None = None


class ConversationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    messages: Annotated[list[ModelMessage], BeforeValidator(ModelMessagesTypeAdapter.validate_python)]
    turns: list[Turn]
    prepared_contexts: list[PreparedContext] = Field(default_factory=list)
    queue: list[QueuedTask] = Field(default_factory=list)
    memory_contexts: dict[str, str] = Field(default_factory=dict)
    history_uses: dict[str, list[str]] = Field(default_factory=dict)
    recovery_checks: list[RecoveryCheck] = Field(default_factory=list)
    action_evidence: list[ActionEvidence] = Field(default_factory=list)
    save_failures: list[str] = Field(default_factory=list)
    last_saved_at: str | None = None
    provider_changes: list[ProviderChange] = Field(default_factory=list)
    approval_settings: ApprovalSettings | None = None
    request_layouts: dict[str, RequestLayout] = Field(default_factory=dict)
    model_selection: ModelSelection | None = None

    @model_validator(mode="before")
    @classmethod
    def preserve_original_permissions(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for item in [*value.get("turns", []), *value.get("queue", [])]:
                if isinstance(item, dict) and isinstance(item.get("task"), dict):
                    if value.get("version", 0) >= 6:
                        if "request_policy" not in item["task"] or "total_requests" not in item["task"]:
                            raise ValueError("新版记录缺少保存的次数政策或累计次数，未重置额度。")
                    else:
                        if item["task"].get("request_policy") not in (None, "shared"):
                            raise ValueError("旧记录不能通过新增政策标记改成不受限的新工作。")
                        item["task"]["request_policy"] = "shared"
                        item["task"]["total_requests"] = item["task"].get("requests", 0)
                        item["task"]["max_turns"] = None
                        item["task"]["turns_used"] = 0
                    if "approval" not in item["task"]:
                        if value.get("version", 0) >= 5:
                            raise ValueError("新版记录缺少保存的批准设置，未套用当前默认扩大范围。")
                        original = ApprovalSettings()
                        original.access.legacy_scope = True
                        item["task"]["approval"] = original.model_dump()
        return value

    @model_validator(mode="after")
    def completed_turns_require_history(self) -> Self:
        if any(turn.status == "completed" for turn in self.turns) and not self.messages:
            raise ValueError("记录缺少完整历史，不能当成已经恢复。")
        return self


@dataclass(frozen=True)
class Update:
    kind: Literal["text", "status", "action", "task", "delegation", "plan", "skills", "artifacts"]
    text: str = ""
    action: Action | None = None
    task: Task | None = None
    delegation: Delegation | None = None
    plan: list[str] | None = None
    skills: list[SkillDescription] | None = None
    artifacts: list[DeliveredArtifact] | None = None
