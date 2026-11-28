from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic_ai.exceptions import ModelAPIError, ModelContextWindowExceededError, ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage

from myrk._conversation.records import DocumentReadError, Task, Turn
from myrk._conversation.stream import RunStreamProgress
from myrk.context import ContextLimitExceeded, ContextPreparationError
from myrk.executor import OfficialExecutor
from myrk.provider import IncompleteGoogleResponse
from myrk.task_control import TaskBudget

if TYPE_CHECKING:
    from myrk.conversation import Conversation


def record_model_failure(
    error: Exception,
    *,
    turn: Turn,
    budget: TaskBudget,
    progress: RunStreamProgress,
    append_run_messages: Callable[[list[ModelMessage]], None],
    usage: RunUsage | None,
    usage_complete: bool,
) -> tuple[RunUsage | None, bool]:
    if isinstance(error, DocumentReadError):
        turn.status = "failed"
        turn.error = str(error)
    elif isinstance(error, ContextLimitExceeded):
        turn.status = "limited"
        turn.error = str(error)
    elif isinstance(error, ContextPreparationError):
        turn.status = "failed"
        turn.error = str(error)
    elif isinstance(error, UsageLimitExceeded):
        turn.status = "cancelled" if budget.token.cancelled and budget.reason is None else "limited"
        turn.error = None if turn.status == "cancelled" else budget.reason or str(error)
    else:
        turn.status = "failed"
        turn.error = str(error) if isinstance(error, IncompleteGoogleResponse) else f"本次处理失败，已保留收到的内容。程序报告的错误种类：{type(error).__name__}（供排查问题用的错误名称）。"
        if isinstance(error, ModelContextWindowExceededError) and turn.request_attempts:
            turn.error = turn.request_attempts[-1].decision + " " + turn.error
        if isinstance(error, ModelAPIError):
            turn.service_failure = True
            turn.error += (f"HTTP（网络回应状态）：{error.status_code}。" if isinstance(error, ModelHTTPError) else "") + "已做与未做见动作记录，部分回答保留；等待你手动继续或更换服务，不会自动转交。"
            if turn.request_attempts and turn.request_attempts[-1].status == "sent":
                attempt = turn.request_attempts[-1]
                attempt.status = "failed"
                attempt.http_status = error.status_code if isinstance(error, ModelHTTPError) else None
                attempt.error = turn.error
                attempt.decision = "服务在输出途中失败；已有内容不可倒回，没有自动重试。"
    handle = progress.handle
    if handle is not None and progress.started:
        append_run_messages(handle.new_messages())
        usage = handle.usage
        usage_complete = (
            isinstance(error, (UsageLimitExceeded, IncompleteGoogleResponse)) and not usage.unmeasured_requests
        )
    return usage, usage_complete


async def finish_turn(
    conversation: Conversation,
    *,
    budget: TaskBudget,
    file_executor: OfficialExecutor,
    timer: asyncio.Task[None],
    turn: Turn,
    task: Task,
    before_requests: int,
    resuming: bool,
    attachment_read_failed: bool,
    usage: RunUsage | None,
    usage_complete: bool,
    delegation_id: str | None,
) -> None:
    if delegation_id is None:
        budget.set_supplement_admission(False)
    timer.cancel()
    await asyncio.gather(timer, return_exceptions=True)
    await file_executor.close()
    budget.finish()
    turn.requests = budget.requests - before_requests
    turn.resume_attachment_read_failed = (resuming and attachment_read_failed and not budget.token.cancelled
        and not turn.requests and not turn.request_attempts and turn.only_local_history_consent)
    step = budget.counter.work_step
    if step is not None and step.request_pending and not step.completed and not turn.resume_attachment_read_failed:
        step.capacity_recovery_used = True
    if task.provider_config.protocol == "google" and turn.status == "cancelled":
        for attempt in turn.request_attempts:
            if attempt.status == "sent":
                attempt.status = "failed"
                attempt.error = "请求已停止，未完整收到服务回应。"
                attempt.decision = "本轮已停止发送；部分回答与已知用量保留，没有自动重试。"
    if any(action.cleanup_complete is False for action in turn.actions):
        turn.status = "failed"
        turn.error = "本次拥有的程序收尾未能确认，尚不能称为已停止；请核对动作记录和程序影响。"
    turn.execution_facts = conversation.execution_facts(task.id)
    if usage is not None:
        if usage_complete:
            attempts = turn.request_attempts
            if any(attempt.observation is not None and attempt.observation.uses_native_usage for attempt in attempts):
                inputs = [attempt.input_tokens for attempt in attempts]
                outputs = [attempt.output_tokens for attempt in attempts]
            else:
                inputs = [usage.input_tokens, *(attempt.input_tokens for attempt in attempts if attempt.purpose == "compaction")]
                outputs = [usage.output_tokens, *(attempt.output_tokens for attempt in attempts if attempt.purpose == "compaction")]
            inputs.extend(work.input_tokens for work in turn.delegations)
            outputs.extend(work.output_tokens for work in turn.delegations)
            if all(value is not None for value in inputs):
                turn.input_tokens = sum(value for value in inputs if value is not None)
            if all(value is not None for value in outputs):
                turn.output_tokens = sum(value for value in outputs if value is not None)
    turn.task = task.model_copy(deep=True)
    try:
        conversation.save()
    except OSError:
        turn.status = "failed"
        turn.error = f"{turn.error + ' ' if turn.error else ''}本次结果未能保存；已有内容保留在当前窗口。{conversation.save_failures[-1]}"
        raise
    finally:
        conversation._live_turns = [live for live in conversation._live_turns if live is not turn]
