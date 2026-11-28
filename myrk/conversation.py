import asyncio
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic_ai import CancellationToken
from pydantic_ai.models import Model

from myrk._conversation import memory, persistence, recipient, recovery
from myrk._conversation.ask import ask_conversation
from myrk._conversation.records import (
    CheckCommand as CheckCommand,
    ConversationRecord as ConversationRecord,
    Delegation as Delegation,
    DocumentReadError as DocumentReadError,
    HISTORICAL_TASKS_HEADER as HISTORICAL_TASKS_HEADER,
    ProviderChange as ProviderChange,
    QueuedTask as QueuedTask,
    SkillDescription as SkillDescription,
    Supplement as Supplement,
    Task as Task,
    TaskScope as TaskScope,
    Turn as Turn,
    Update as Update,
    without_pending_supplement_journal as without_pending_supplement_journal,
)
from myrk.actions import Action as Action
from myrk.context import CORE_TOOL_CONTRACT_VERSION
from myrk.extensions import ExtensionCatalog
from myrk.memory import MemoryChange
from myrk.model_controls import ModelSelection, connection_identity
from myrk.permissions import ApprovalMode, ApprovalSettings
from myrk.provider import ProviderConfig
from myrk.recovery import ActionEvidence, RecoveryOwnership, RecoveryReport
from myrk.request_observation import observe_request
from myrk.task_control import RequestAttempt as RequestAttempt, TaskBudget, WorkStep


class Conversation:
    def __init__(self, path: Path, *, approval_settings: ApprovalSettings | None = None) -> None:
        self.path = path
        self._record = ConversationRecord(version=10, messages=[], turns=[], approval_settings=approval_settings.model_copy(deep=True) if approval_settings is not None else None)
        self._resume_authorized: str | None = None
        self._starting_queued_task: tuple[str, int] | None = None
        self._live_turns: list[Turn] = []

    @property
    def approval_settings(self) -> ApprovalSettings:
        if self._record.approval_settings is None:
            previous = next((turn.task.approval for turn in reversed(self.turns) if turn.task is not None), ApprovalSettings())
            self._record.approval_settings = previous.model_copy(deep=True)
        return self._record.approval_settings

    def set_approval_mode(self, mode: ApprovalMode) -> ApprovalSettings:
        previous = self._record.approval_settings
        self._record.approval_settings = ApprovalSettings(mode=mode)
        try:
            self.save()
        except OSError:
            self._record.approval_settings = previous
            raise
        return self.approval_settings.model_copy(deep=True)

    def set_turn_reviewer(self, task_id: str, reviewer: Literal["user", "assistant"]) -> ApprovalSettings:
        turn = self.turns[-1] if self.turns else None
        if turn is None or turn.status != "running" or turn.task is None or turn.task.id != task_id or turn.approval_settings is None:
            raise ValueError("只能为当前仍在运行的工作修改后续新步骤的审核者。")
        if turn.approval_settings.condition == "never" or turn.approval_settings.access.sandbox == "full_access":
            raise ValueError("完全访问权限免普通逐项批准，不能为当前轮新增审核者；下一轮可改用其他批准方式。")
        previous = turn.approval_settings.reviewer
        turn.approval_settings.reviewer = reviewer
        try:
            self.save()
        except OSError:
            turn.approval_settings.reviewer = previous
            raise
        return turn.approval_settings.model_copy(deep=True)

    @property
    def turns(self) -> tuple[Turn, ...]:
        return tuple(self._record.turns)

    def compaction_status(self) -> list[dict[str, Any]]:
        """Return summary progress and bounded source references for the desktop."""
        return persistence.compaction_status(self.turns)

    def read_compaction_source(self, compaction_id: str, message_index: int, part_index: int, start: int = 0, length: int = 8000) -> str:
        """Read one verified original part; the caller never supplies a file path."""
        return persistence.read_compaction_source(
            self.turns, self._record.messages, compaction_id, message_index, part_index, start, length,
        )

    @property
    def model_selection(self) -> ModelSelection | None:
        return self._record.model_selection

    def set_model_selection(self, selection: ModelSelection | None, *, persist: bool = True) -> None:
        previous = self._record.model_selection
        self._record.model_selection = selection
        if not persist:
            return
        try:
            self.save()
        except OSError:
            self._record.model_selection = previous
            raise

    def check_model_history(self, target: ProviderConfig) -> None:
        previous = self.turns[-1] if self.turns else None
        if previous is None or previous.task is None or previous.task.provider_config == target:
            return
        if connection_identity(previous.task.provider_config) != connection_identity(target):
            raise ValueError("原会话历史尚未批准提供给该服务地址；请先审阅更换服务，或新建空白会话。没有发出请求。")
        compatible, reason = self._provider_history_compatibility(target, previous.task.provider_config.model)
        if not compatible:
            raise ValueError(reason)

    @property
    def queue(self) -> list[QueuedTask]:
        return self._record.queue

    @property
    def waiting_queue(self) -> list[QueuedTask]:
        return self.queue[1:] if self._starting_queued_task is not None else self.queue

    def prepare_next_queued(self, verified_recovery: RecoveryReport | None = None) -> QueuedTask:
        item = self.queue[0]
        previous = self._current_recovery_turn()
        if previous is not None:
            recovery = self._recovery_report(previous)
            if (verified_recovery is None or verified_recovery.task_id != recovery.task_id
                    or not verified_recovery.can_resume or not recovery.can_resume
                    or previous.status != "failed" or not previous.service_failure
                    or any(turn.task is not None and turn.task.id == item.task.id for turn in self.turns)):
                raise ValueError("原工作仍待核查，排队工作已保留。请先确认原工作的真实影响。")
            message_count = len(self._record.messages)
            self._close_interrupted_tool_calls(previous, verified_recovery)
            try:
                self.check_model_history(item.task.provider_config)
            except ValueError:
                del self._record.messages[message_count:]
                raise
            self._resume_authorized = item.task.id
        self._starting_queued_task = (item.task.id, len(self.turns))
        return item

    def release_queued_start(self, task_id: str) -> bool:
        restored = self._starting_queued_task is not None and self._starting_queued_task[0] == task_id
        if restored:
            self._starting_queued_task = None
        if self._resume_authorized == task_id:
            self._resume_authorized = None
        return restored

    @property
    def save_failures(self) -> tuple[str, ...]:
        return tuple(self._record.save_failures)

    @property
    def last_saved_at(self) -> str | None:
        return self._record.last_saved_at

    @property
    def recovery(self) -> RecoveryReport | None:
        current = self._current_recovery_turn()
        return self._recovery_report(current) if current is not None else None

    def _current_recovery_turn(self) -> Turn | None:
        return recovery._current_recovery_turn(self)

    def _earlier_recovery_turns(self) -> Iterator[Turn]:
        return recovery._earlier_recovery_turns(self)

    @property
    def earlier_recoveries(self) -> list[RecoveryReport]:
        return [self._recovery_report(turn) for turn in self._earlier_recovery_turns()]

    def recovery_turn(self, task_id: str) -> Turn | None:
        return recovery.recovery_turn(self, task_id)

    def _recovery_report(self, previous: Turn) -> RecoveryReport:
        return recovery._recovery_report(self.path, self._record.recovery_checks, previous)

    def authorize_denied_retry(self, task_id: str, action_id: str) -> Task:
        """One visible denial can be resubmitted once; this is never an execution allow."""
        return recovery.authorize_denied_retry(self, task_id, action_id)

    def verify_recovery(self, task_id: str) -> RecoveryReport:
        return asyncio.run(self.verify_recovery_async(task_id))

    async def verify_recovery_async(self, task_id: str) -> RecoveryReport:
        return await recovery.verify_recovery_async(self, task_id)

    def _latest_task(self, task_id: str) -> Task | None:
        return next((turn.task for turn in reversed(self._record.turns)
                     if turn.task is not None and turn.task.id == task_id), None)

    def prepare_resume(self, task_id: str, *, request_limit: int | None = None) -> Task:
        return recovery.prepare_resume(self, task_id, request_limit=request_limit)

    def _close_interrupted_tool_calls(self, previous: Turn, report: RecoveryReport) -> None:
        recovery._close_interrupted_tool_calls(self, previous, report)

    def preview_provider_change(self, task_id: str, target: ProviderConfig) -> ProviderChange:
        return recipient.preview_provider_change(self, task_id, target)

    def _provider_history_compatibility(self, target: ProviderConfig, source_model: str) -> tuple[bool, str]:
        return recipient._provider_history_compatibility(self._record, target, source_model)

    def _provider_materials(self, task: Task) -> str:
        return recipient._provider_materials(self._record, self.turns, task)

    def decide_provider_change(self, change_id: str, approved: bool) -> Task | None:
        return recipient.decide_provider_change(self, change_id, approved)

    async def resume_task(self, task_id: str, *, model: Model, on_update: Callable[[Update], None] | None = None,
                          approve_action: Callable[[Action], Awaitable[bool]] | None = None) -> Turn:
        task = self.prepare_resume(task_id)
        return await self.ask("用户明确选择继续原工作；只做尚未完成的事项。中断核查记录是实际依据，不重放旧请求，不重复已完成动作；新敏感动作重新批准。",
                              document=Path(task.document) if task.document else None, model=model, task=task, scope=task.scope,
                              on_update=on_update, approve_action=approve_action, _prompt_origin="recovery_control")

    @classmethod
    def open(cls, path: Path) -> "Conversation":
        conversation = cls(path)
        conversation._record = ConversationRecord.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        conversation._settle_interrupted_compactions()
        conversation._restore_legacy_supplement_selection()
        for turn in conversation.turns:
            account = turn.request_account or turn.task
            if account is not None and account.work_step is None and turn.status != "completed":
                account.work_step = WorkStep(capacity_recovery_used=True, turn_number=account.turns_used or None)
            if account is not None and account.work_step is not None and account.work_step.request_pending and not account.work_step.completed:
                account.work_step.capacity_recovery_used = True
        return conversation

    def _restore_legacy_supplement_selection(self) -> None:
        """Derive old selections from the saved summary preparation evidence."""
        persistence._restore_legacy_supplement_selection(self._record, self.turns)

    def _settle_interrupted_compactions(self) -> None:
        """Settle abandoned candidates in memory, leaving committed views intact."""
        persistence._settle_interrupted_compactions(self.turns, self._live_turns)

    def record_failure(self, prompt: str, *, thinking: bool, error: str) -> Turn:
        turn = Turn(prompt=prompt, answer="", status="failed", thinking=thinking, error=error)
        self._record.turns.append(turn)
        return turn

    def execution_facts(self, task_id: str) -> str:
        return persistence.execution_facts(self.turns, task_id)

    def save(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        reliable = self._record.last_saved_at
        latest_task = self.turns[-1].task if self.turns else None
        started = bool(self._starting_queued_task and latest_task and len(self.turns) > self._starting_queued_task[1]
                       and latest_task.id == self._starting_queued_task[0])
        queued_item = self.queue.pop(0) if started else None
        try:
            self._save(now)
        except OSError:
            if queued_item is not None:
                self.queue.insert(0, queued_item)
            self._record.last_saved_at = reliable
            self._record.save_failures.append(f"{now}：进展未能保存；最后可靠保存位置：{self.path if reliable is not None else '尚无可靠保存'}；时间：{reliable or '未知'}。")
            raise
        if queued_item is not None:
            self._starting_queued_task = None

    def _save(self, now: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._record.version < 10:
            if self.path.exists():
                original = self.path.read_bytes()
                backup = self.path.parent / "backups" / f"{self.path.stem}-v{self._record.version}-{uuid4().hex}.json"
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(original)
                if backup.read_bytes() != original:
                    raise OSError("旧记录备份未能核对，未覆盖原记录。")
                Conversation.open(backup)
            self._record.version = 10
        latest = {item.action.id: item.action for item in self._record.action_evidence}
        for turn in self.turns:
            for action in turn.actions:
                if latest.get(action.id) != action:
                    phase: Literal["prepared", "started", "checkpoint", "result"] = "checkpoint"
                    if action.finished_at is not None:
                        phase = "result"
                    elif action.id not in latest:
                        phase = "prepared" if action.effect == "not_started" else "started"
                    elif action.effect == "unknown" and latest[action.id].effect != "unknown":
                        phase = "started"
                    self._record.action_evidence.append(ActionEvidence(id=uuid4().hex, recorded_at=now, phase=phase, action=action.model_copy(deep=True)))
        self._record.last_saved_at = now
        ownership = RecoveryOwnership(self.path)
        for live_turn in self._live_turns:
            for live_action in live_turn.actions:
                ownership.remember(live_action)
        pending_path = self.path.with_suffix(self.path.suffix + ".tmp")
        pending_path.write_text(self._record.model_dump_json(indent=2), encoding="utf-8")
        pending_path.replace(self.path)

    async def change_memory(self, changes: list[MemoryChange], *, project: str | None = None,
                            task: Task, budget: TaskBudget,
                            approve_action: Callable[[Action], Awaitable[bool]],
                            on_update: Callable[[Update], None]) -> Turn:
        """User-requested note management uses the same persisted real-action boundary."""
        return await memory.change_memory(
            self, changes, project=project, task=task, budget=budget, approve_action=approve_action, on_update=on_update,
        )

    async def approve_memory_proposal(self, *, task_id: str, action_id: str, approval_id: str, approved: bool,
                                      on_update: Callable[[Update], None], active_seconds_limit: float | None = None) -> None:
        await memory.approve_memory_proposal(
            self, task_id=task_id, action_id=action_id, approval_id=approval_id, approved=approved, on_update=on_update, active_seconds_limit=active_seconds_limit,
        )

    async def ask(
        self,
        prompt: str,
        *,
        document: Path | None,
        model: Model,
        on_update: Callable[[Update], None] | None = None,
        cancellation_token: CancellationToken | None = None,
        thinking: bool = False,
        reasoning: str | None = None,
        request_limit: int | None = None,
        scope: TaskScope | None = None,
        task: Task | None = None,
        new_task: bool = False,
        budget: TaskBudget | None = None,
        active_seconds_limit: float | None = None,
        approve_action: Callable[[Action], Awaitable[bool]] | None = None,
        _delegation_id: str | None = None,
        review_model: Model | None = None,
        review_model_resolver: Callable[[], Awaitable[Model]] | None = None,
        _approval_settings: ApprovalSettings | None = None,
        _save_supplement_owner: Callable[[], None] | None = None,
        project_trusted: bool = False,
        _prompt_origin: Literal["user", "recovery_control"] = "user",
    ) -> Turn:
        return await ask_conversation(
            self,
            prompt,
            document=document,
            model=model,
            on_update=on_update,
            cancellation_token=cancellation_token,
            thinking=thinking,
            reasoning=reasoning,
            request_limit=request_limit,
            scope=scope,
            task=task,
            new_task=new_task,
            budget=budget,
            active_seconds_limit=active_seconds_limit,
            approve_action=approve_action,
            _delegation_id=_delegation_id,
            review_model=review_model,
            review_model_resolver=review_model_resolver,
            _approval_settings=_approval_settings,
            _save_supplement_owner=_save_supplement_owner,
            project_trusted=project_trusted,
            _prompt_origin=_prompt_origin,
            extension_catalog=ExtensionCatalog,
            observe=observe_request,
            core_tool_contract_version=CORE_TOOL_CONTRACT_VERSION,
            child_factory=Conversation,
        )
