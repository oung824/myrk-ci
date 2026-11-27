"""Real actions share one approval, evidence, and stop boundary."""

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from myrk.conversation import Task
from uuid import uuid4

from pydantic import BaseModel, Field
from pydantic_ai import RunCancelled
from pydantic_ai.exceptions import ModelRetry, UsageLimitExceeded

from myrk.task_control import TaskBudget
from myrk.memory import MemoryChange
from myrk.permissions import ApprovalSettings
from myrk.review import ReviewRecord
from myrk.artifacts import FileChange

_write_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class ToolOrigin:
    name: str
    arguments: dict[str, Any]
    source: str
    schema_version: str


tool_origin: ContextVar[ToolOrigin | None] = ContextVar('tool_origin', default=None)


def approval_context(task: "Task") -> str:
    return sha256(json.dumps({"task_id": task.id, "goal": task.goal,
        "scope": task.scope.model_dump() if task.scope else None,
        "approval": task.approval.model_dump(exclude={"reviewer"}),
        "supplements": [item.model_dump(exclude={"selected"}) for item in task.active_supplements]}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def folder_write_lock(root: str) -> asyncio.Lock:
    """All sessions in this desktop process serialize writes for the same folder."""
    return _write_locks.setdefault(root.casefold(), asyncio.Lock())


class Action(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    kind: str
    target: str
    status: Literal["started", "awaiting_approval", "completed", "denied", "failed", "conflict", "cancelled"] = "started"
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    output: str = ""
    error: str | None = None
    full_output_path: str | None = None
    prepared_request: int = 0
    impact: str = ""
    approval_id: str | None = None
    approval_basis: Literal["task_scope", "pending", "user", "assistant", "denied"] = "task_scope"
    denial_source: Literal["user", "assistant", "scope", "review_incomplete"] | None = None
    backup_path: str | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cwd: str | None = None
    process_id: int | None = None
    effect: Literal["not_started", "completed", "unknown"] = "not_started"
    cleanup_complete: bool | None = None
    memory_changes: list[MemoryChange] | None = None
    memory_project: str | None = None
    memory_deferred: bool = False
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    tool_source: str | None = None
    tool_schema_version: str | None = None
    before_sha256: str | None = None
    after_sha256: str | None = None
    file_changes: list[FileChange] = Field(default_factory=list)
    receipt_path: str | None = None
    job_name: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    delegation_id: str | None = None
    approval_settings: ApprovalSettings | None = None
    sandbox_type: str | None = None
    authorization_revision: str | None = None
    prepared_parameters: dict[str, object] = Field(default_factory=dict)
    approval_preview: str | None = Field(default=None, exclude=True)
    approval_context_sha256: str | None = None
    prepared_action_sha256: str | None = None
    review: ReviewRecord | None = None

    def prepared_digest(self) -> str:
        return sha256(self.model_dump_json(include={"kind", "target", "impact", "prepared_parameters", "approval_settings", "tool_name", "tool_arguments", "tool_source", "tool_schema_version"}).encode("utf-8")).hexdigest()


class ActionExecutor:
    def __init__(self, *, actions: list[Action], budget: TaskBudget, save: Callable[[], None],
                 notify: Callable[[Action], None], approve: Callable[[Action], Awaitable[bool]] | None = None,
                 evidence_directory: Path | None = None,
                 prepared_request: Callable[[], int] | None = None,
                 applied_supplements: set[str] | None = None,
                 delegation_id: str | None = None,
                 review: Callable[[Action], Awaitable[bool]] | None = None,
                 current_approval_settings: Callable[[], ApprovalSettings] | None = None) -> None:
        self.actions, self.budget, self.save, self.notify, self.approve = actions, budget, save, notify, approve
        self.evidence_directory = evidence_directory
        self.prepared_request = prepared_request
        self.applied_supplements = applied_supplements
        self.delegation_id = delegation_id
        self.review = review
        self.current_approval_settings = current_approval_settings
        self.completed_actions: list[Action] = []

    def reject(self, action: Action) -> None:
        """A user can decline a persisted proposal even after automatic work exhausted its budget."""
        action.status, action.approval_basis, action.effect = "denied", "denied", "not_started"
        action.denial_source = "user"
        action.error = "用户拒绝本次具体提议；事实笔记未改变。"
        action.finished_at = datetime.now(timezone.utc).isoformat()
        self.save()
        self.notify(action)

    async def run(self, kind: str, target: str, impact: str, operation: Callable[[Action], Awaitable[str]], *, requires_approval: bool = False, memory_changes: list[MemoryChange] | None = None, memory_project: str | None = None, defer_approval: bool = False, resume: Action | None = None, tool_call_id: str | None = None, replay_completed: bool = True, read_sha256: str | None = None, parameters: dict[str, object] | None = None, prepare_operation: Callable[[Action], Awaitable[None]] | None = None, approval_settings: ApprovalSettings | None = None) -> str:
        self.budget.checkpoint(applied_supplements=self.applied_supplements)
        completed = next((item for item in reversed(self.completed_actions)
                          if item.kind == kind and item.target == target and item.impact == impact), None)
        if completed is not None and kind == "edit" and read_sha256 is not None and completed.after_sha256 == read_sha256:
            replay_completed = True
        if completed is not None and kind in {"check", "command"}:
            history = self.completed_actions + self.actions
            if any(item.kind in {"create", "write", "edit", "delete"} and (item in self.completed_actions or item.effect == "completed")
                   for item in history[history.index(completed) + 1:]):
                completed = None
        if replay_completed and kind in {"create", "write", "edit", "delete", "send", "check", "command"} and completed is not None:
            return f"原动作{completed.id}已有可靠执行结果，续办没有重复执行。\n{completed.error or completed.output}\n正常输出：{completed.stdout}\n错误输出：{completed.stderr}"
        action = resume or Action(task_id=self.budget.task.id, kind=kind, target=target,
                        impact=impact, prepared_request=self.prepared_request() if self.prepared_request else self.budget.task.requests,
                        memory_changes=memory_changes, memory_project=memory_project, delegation_id=self.delegation_id, tool_call_id=tool_call_id)
        user_approval_required = kind in {"history_reuse", "memory", "memory_recall"}
        if resume is None:
            origin = tool_origin.get()
            if origin is not None:
                action.tool_name, action.tool_source, action.tool_schema_version = origin.name, origin.source, origin.schema_version
                action.tool_arguments = deepcopy(origin.arguments)
            action.approval_settings = (approval_settings or (self.current_approval_settings() if self.current_approval_settings else self.budget.task.approval)).model_copy(deep=True)
            if requires_approval and (user_approval_required or defer_approval):
                action.approval_settings.reviewer = "user"
                action.approval_settings.condition = "on_request"
            action.authorization_revision = action.approval_settings.revision
            action.prepared_parameters = parameters or {}
        def current_context() -> str:
            return approval_context(self.budget.task)
        action.approval_context_sha256 = current_context()
        action.prepared_action_sha256 = action.prepared_digest()
        if resume is None:
            self.actions.append(action)
        if requires_approval and defer_approval:
            action.status, action.approval_basis, action.approval_id = "awaiting_approval", "pending", uuid4().hex
            action.memory_deferred = True
            self.save()
            self.notify(action)
            return "记忆提议已保存，等待你批准；事实笔记尚未改变。待审记录可能含完整原文。"
        result = ""
        try:
            self.budget.check_action_start()
            if prepare_operation is not None:
                await prepare_operation(action)
                action.prepared_action_sha256 = action.prepared_digest()
            if requires_approval:
                action.status, action.approval_basis = "awaiting_approval", "pending"
                if resume is None:
                    action.approval_id = uuid4().hex
                self.save()
                self.notify(action)
                automatic = action.approval_settings is not None and action.approval_settings.reviewer == "assistant" and not user_approval_required
                if automatic:
                    approved = False
                    try:
                        approved = self.review is not None and await self.review(action)
                    finally:
                        if not approved and action.denial_source is None:
                            action.denial_source = "assistant" if action.review is not None and action.review.status == "denied" else "review_incomplete"
                else:
                    with self.budget.waiting():
                        approved = self.approve is not None and await self.approve(action.model_copy(deep=True))
                        if self.approve is not None:
                            self.budget.checkpoint(preparing_request=True)
                if not approved:
                    if action.denial_source == "user":
                        denied = "用户未批准本次具体动作；已拒绝，未执行所提议的动作。"
                    elif automatic:
                        denied = "独立审核未完成；未执行所提议的动作。"
                    elif self.approve is None:
                        denied = "尚未收到本人的具体决定；未执行所提议的动作。"
                    else:
                        action.denial_source = "user"
                        denied = "用户未批准本次具体动作；已拒绝，未执行所提议的动作。"
                    if kind == "delete" and action.before_sha256 is not None:
                        denied += "为核对删除版本已读取原件；未删除或改变文件，也未发送。"
                    raise PermissionError(action.review.reason if automatic and action.review and action.denial_source != "user" else denied)
                self.budget.checkpoint(applied_supplements=self.applied_supplements)
                if action.approval_context_sha256 != current_context() or action.prepared_action_sha256 != action.prepared_digest():
                    if action.review is not None and action.review.status == "allowed":
                        action.review.status, action.review.reason = "context_changed", "原动作或授权背景已变化，旧允许已失效。"
                    raise PermissionError("原动作或授权背景已变化，旧允许已失效；未开始执行，请按当前要求重新准备。")
                action.approval_basis = "assistant" if automatic and action.review is not None and not action.review.human_fallback else "user"
            self.budget.check_action_start()
            action.status = "started"
            action.effect = "unknown"
            self.save()
            self.notify(action)
            result = await operation(action)
            if not action.output:
                action.output = result
                if len(result) > 12000 and self.evidence_directory is not None:
                    self.evidence_directory.mkdir(parents=True, exist_ok=True)
                    evidence = self.evidence_directory / f"{action.id}.txt"
                    evidence.write_text(result, encoding="utf-8")
                    action.full_output_path = str(evidence)
                    action.output = result[:6000] + f"\n中间内容已省略；全文位置：{evidence}。\n" + result[-6000:]
            action.status, action.effect = "completed", "completed"
        except PermissionError as error:
            if action.denial_source is None and action.status != "awaiting_approval":
                action.denial_source = "scope"
            action.status, action.approval_basis, action.error, action.effect = "denied", "denied", str(error), "not_started"
        except ModelRetry as error:
            if action.review is not None and action.review.status == "allowed":
                action.review.status, action.review.reason = "context_changed", str(error)
            action.status, action.error, action.effect = "conflict", str(error), "not_started"
            raise
        except (RunCancelled, asyncio.CancelledError):
            stopped = f"指定检查「{action.target}」" if action.kind == "check" else "命令" if action.kind == "command" or action.process_id is not None else "工作"
            action.status, action.error = "cancelled", f"{stopped}已停止；已发生的影响和输出保留。"
            raise
        except UsageLimitExceeded as error:
            action.status, action.error = "cancelled", str(error)
            if self.budget.token.cancelled:
                raise
        except (OSError, ValueError, UnicodeError) as error:
            action.status, action.error = "failed", ("无法读取文字；请采用UTF-8（常见文字保存格式）。" if isinstance(error, UnicodeError) else str(error))
        finally:
            action.finished_at = datetime.now(timezone.utc).isoformat()
            self.save()
            self.notify(action)
        if action.error and (action.stdout or action.stderr):
            return f"{action.error}\n正常输出：\n{action.stdout}\n错误输出：\n{action.stderr}"
        return action.error or result
