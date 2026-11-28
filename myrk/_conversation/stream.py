from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent, AgentRunResult, AgentRunResultEvent, FunctionToolCallEvent
from pydantic_ai.agent.abstract import AgentRunEvents
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta
from pydantic_ai.usage import RunUsage, UsageLimits

from myrk._conversation.records import DocumentReadError, Task, TaskScope, Turn, Update
from myrk.actions import Action, ActionExecutor
from myrk.provider import IncompleteGoogleResponse
from myrk.task_control import TaskBudget

if TYPE_CHECKING:
    from myrk.conversation import Conversation


@dataclass
class RunStreamProgress:
    handle: AgentRunEvents[str] | None = None
    started: bool = False


async def approve_history_reuse(
    conversation: Conversation,
    *,
    task: Task,
    turn: Turn,
    scope: TaskScope | None,
    project: str | None,
    executor: ActionExecutor,
) -> None:
    if conversation._record.messages:
        previous_projects = {str(Path(previous.task.scope.root).resolve()) for previous in conversation._record.turns[:-1] if previous.task is not None and previous.task.scope is not None} if scope is not None else set()
        previous_projects.discard(project or "")
        previous_receivers = {"接收服务：" + json.dumps(previous.task.provider_config.identity, ensure_ascii=False)
            for previous in conversation.turns[:-1] if previous.task is not None and previous.task.provider_config.identity != task.provider_config.identity}
        history_sources = previous_projects | previous_receivers
        history_key = json.dumps([project, scope.provider if scope else None, scope.recipients if scope else None, task.provider_config.identity], ensure_ascii=False)
        permitted = set(conversation._record.history_uses.get(history_key, []))
        if history_sources - permitted:
            if previous_receivers:
                source_task = next(previous.task for previous in reversed(conversation.turns[:-1]) if previous.task is not None and previous.task.provider_config.identity != task.provider_config.identity)
                compatible, reason = conversation._provider_history_compatibility(task.provider_config, source_task.provider_config.model)
                if not compatible:
                    raise DocumentReadError(reason)
            async def allow_history(_action: Action) -> str:
                conversation._record.history_uses[history_key] = sorted(history_sources | permitted)
                return "已批准本聊天既有历史用于当前项目和所选服务；不扩大新文件范围、动作或其他项目笔记的自动取用。"
            await executor.run("history_reuse", project or "", f"沿用本聊天完整既有历史（可能含旧项目资料），用于当前工作并提供给当前所选{task.provider_config.name}（理解与回答服务），地址{task.provider_config.base_url}，型号{task.provider_config.model}。旧资料来源：" + ", ".join(sorted(history_sources)) + "；当前项目：" + (project or "无任务文件夹") + "。只覆盖这组聊天历史的当前用途，不扩大新文件访问、其他项目笔记或其他会话权限；拒绝后不发新请求，可新建聊天。", allow_history, requires_approval=True)
            if turn.actions[-1].status != "completed":
                raise DocumentReadError("未批准本聊天历史用于当前项目及具体接收方，未发送新请求；原始历史保留。你可以新建聊天，或重新确认这次历史用途。")


async def run_model_stream(
    conversation: Conversation,
    progress: RunStreamProgress,
    *,
    agent: Agent[None, str],
    prompt: str,
    history: list[ModelMessage],
    budget: TaskBudget,
    task: Task,
    turn: Turn,
    scope: TaskScope | None,
    on_update: Callable[[Update], None] | None,
    append_run_messages: Callable[[list[ModelMessage]], None],
) -> RunUsage:
    result: AgentRunResult[str] | None = None
    async with agent.run_stream_events(
        prompt,
        message_history=history,
        cancellation_token=budget.token,
        usage_limits=UsageLimits(request_limit=None),
    ) as events:
        progress.handle = events
        async for event in events:
            progress.started = True
            chunk = ""
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                chunk = event.part.content
            elif isinstance(event, PartDeltaEvent) and isinstance(
                event.delta, TextPartDelta
            ):
                chunk = event.delta.content_delta
            elif isinstance(event, FunctionToolCallEvent) and on_update is not None:
                on_update(Update("status", "正在核对并执行资料动作" if scope else "正在读取授权文本"))
            elif isinstance(event, AgentRunResultEvent):
                result = event.result
            if chunk:
                turn.answer += chunk
                if on_update is not None:
                    on_update(Update("text", chunk))
    if result is None:
        raise RuntimeError("模型运行未完整结束，不能标为成功。")
    append_run_messages(result.new_messages())
    turn.answer = result.output
    if any(work.verification_status == "pending" for work in turn.delegations):
        turn.answer += "\n分工专用核验尚未登记；请查看分工与动作记录中的读取和检查情况，完成核验登记。"
    if any(work.verification_status == "gaps" for work in turn.delegations):
        turn.answer += "\n分工核验记录有缺口；报告本身不代表真实成果合格，请查看分工与动作证据，继续核对未通过或未完成的部分。"
    turn.status = "completed"
    checks = [action for item in conversation.turns if item.task is not None and item.task.id == task.id for action in item.actions if action.kind == "check"]
    verified_checks = {item.action_id: item for item in conversation._record.recovery_checks}
    exit_codes = [verified_checks[action.id].exit_code if action.id in verified_checks else action.exit_code for action in checks]
    turn.checks_passed = all(code == 0 for code in exit_codes) if exit_codes and all(code is not None for code in exit_codes) else None
    if turn.actions:
        turn.delivery_status = "awaiting_acceptance"
    return events.usage


async def run_memory_review(
    *,
    review_agent: Agent[None, str],
    review_history: list[ModelMessage],
    budget: TaskBudget,
    turn: Turn,
    usage: RunUsage,
    usage_complete: bool,
    on_update: Callable[[Update], None] | None,
    save: Callable[[], None],
    safe_failure_descriptions: Callable[[list[ModelMessage]], None],
) -> bool:
    review_events: AgentRunEvents[str] | None = None
    try:
        budget.checkpoint()
        if on_update is not None:
            on_update(Update("status", "正在按剩余额度整理一次有用经验"))
        save()
        async with review_agent.run_stream_events("按本任务资料进行一次有限复盘。", message_history=deepcopy(review_history), cancellation_token=budget.token, usage_limits=UsageLimits(request_limit=None)) as review_stream:
            review_events = review_stream
            async for review_event in review_stream:
                if isinstance(review_event, AgentRunResultEvent):
                    turn.memory_review_messages = review_event.result.new_messages()
        usage.incr(review_stream.usage)
        usage_complete = usage_complete and not review_stream.usage.unmeasured_requests
        turn.context_notes.append("交付后有限复盘已结束；与原任务共享请求、主动时间和停止信号。改删提议仍等待你批准。")
    except Exception as error:
        if review_events is not None:
            if not isinstance(error, IncompleteGoogleResponse):
                turn.memory_review_messages = review_events.new_messages()
            safe_failure_descriptions(turn.memory_review_messages)
            usage.incr(review_events.usage)
        usage_complete = False
        if turn.request_attempts and turn.request_attempts[-1].status == "sent":
            attempt = turn.request_attempts[-1]
            attempt.status = "failed"
            attempt.http_status = error.status_code if isinstance(error, ModelHTTPError) else None
            attempt.error = f"{type(error).__name__}（复盘中服务报告的错误种类）"
            attempt.decision = "有限复盘未完成；主要交付、部分内容与实际错误保留，未倒回重发。"
        turn.context_notes.append("交付后有限复盘未完成，主要交付与已有笔记保留；" + (budget.reason or ("工作已停止。" if budget.token.cancelled else f"错误名称：{type(error).__name__}（供排查原因用）。")))
    return usage_complete
