from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from myrk._conversation.records import Task, Turn, Update
from myrk.actions import Action, ActionExecutor, folder_write_lock
from myrk.memory import MemoryChange, MemoryStore
from myrk.task_control import TaskBudget

if TYPE_CHECKING:
    from myrk.conversation import Conversation


async def change_memory(self: Conversation, changes: list[MemoryChange], *, project: str | None = None,
                        task: Task, budget: TaskBudget,
                        approve_action: Callable[[Action], Awaitable[bool]],
                        on_update: Callable[[Update], None]) -> Turn:
    if self.recovery is not None:
        raise ValueError("原工作仍待核查，不能用本地管理新轮次盖住；请先核查原工作，或新建聊天管理事实。")
    turn = Turn(prompt=task.goal, answer="", task=task)
    self._record.turns.append(turn)
    self._live_turns.append(turn)
    memories = MemoryStore(self.path.parent / "memories")
    executor = ActionExecutor(actions=turn.actions, budget=budget, save=self.save,
                              notify=lambda action: on_update(Update("action", action=action.model_copy(deep=True))), approve=approve_action)
    budget.start()
    try:
        async def apply(_action: Action) -> str:
            async with folder_write_lock(str(memories.directory)):
                return memories.change(changes, project)
        turn.answer = await executor.run("memory", project or "个人笔记", "纠正或删除所选事实：" + json.dumps([change.model_dump() for change in changes], ensure_ascii=False) + "。删除只移除唯一匹配的整条当前事实；旧聊天、摘要、备份、导出和其他待审提议保留。当前聊天背景不立即更新。", apply,
                                         requires_approval=any(change.action == "remove" for change in changes), memory_changes=changes, memory_project=project)
        turn.status = "completed"
    except UsageLimitExceeded as error:
        turn.status = "cancelled" if budget.token.cancelled else "limited"
        turn.error = str(error)
    finally:
        budget.finish()
        self._record.messages.extend([ModelRequest(parts=[UserPromptPart(content=task.goal)]), ModelResponse(parts=[TextPart(content="已记录本地事实管理结果；具体笔记原文仅供本地管理查看，不作为聊天资料提供。")])])
        try:
            self.save()
        finally:
            self._live_turns = [live for live in self._live_turns if live is not turn]
    return turn


async def approve_memory_proposal(self: Conversation, *, task_id: str, action_id: str, approval_id: str, approved: bool,
                                  on_update: Callable[[Update], None], active_seconds_limit: float | None = None) -> None:
    owner = next((turn for turn in self._record.turns if turn.task is not None and turn.task.id == task_id and any(action.id == action_id for action in turn.actions)), None)
    action = next((action for action in owner.actions if action.id == action_id), None) if owner else None
    if owner is None or owner.task is None or action is None or action.status != "awaiting_approval" or action.approval_id != approval_id or not action.memory_changes or not action.memory_deferred:
        raise ValueError("这份答复不对应当前待审记忆动作，未执行。")
    changes = action.memory_changes
    memories = MemoryStore(self.path.parent / "memories")
    budget = TaskBudget(owner.task)
    executor = ActionExecutor(actions=owner.actions, budget=budget, save=self.save,
                              notify=lambda event: on_update(Update("action", action=event.model_copy(deep=True))))
    if not approved:
        executor.reject(action)
        return
    owner.task.active_seconds_limit = None
    async def decide(_action: Action) -> bool:
        return approved
    executor.approve = decide
    async def apply(_action: Action) -> str:
        async with folder_write_lock(str(memories.directory)):
            return memories.change(changes, action.memory_project)
    budget.start()
    try:
        await executor.run(action.kind, action.target, action.impact, apply, requires_approval=True, resume=action)
    finally:
        budget.finish()
        self.save()
        on_update(Update("task", task=owner.task.model_copy(deep=True)))
