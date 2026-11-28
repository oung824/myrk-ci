from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext

from myrk._conversation.records import Task, TaskScope, Turn, Update
from myrk.compaction import SourcePart, part_hash
from myrk.context import ContextPreparationError, RecoveryControlPrompt
from myrk.provider import IncompleteGoogleResponse, google_response_error
from myrk.request_observation import record_response_usage
from myrk.task_control import TaskBudget

if TYPE_CHECKING:
    from myrk.conversation import Conversation


class TurnProgress:
    def __init__(self, conversation: "Conversation", turn: Turn, task: Task, budget: TaskBudget,
                 scope: TaskScope | None, on_update: Callable[[Update], None] | None,
                 prompt_origin: Literal["user", "recovery_control"]) -> None:
        self.conversation = conversation
        self.turn = turn
        self.task = task
        self.budget = budget
        self.scope = scope
        self.on_update = on_update
        self.prompt_origin = prompt_origin
        self.saved_run_messages = 0
        self.history_length = 0
        self.review_history_length = 0

    def safe_failure_descriptions(self, messages: list[ModelMessage]) -> None:
        failures = [attempt for previous in self.conversation.turns for attempt in previous.request_attempts if attempt.status == "failed" and attempt.error]
        for message in messages:
            if isinstance(message, ModelResponse):
                if message.failed_attempts:
                    message.failed_attempts = [replace(failed, error=next((attempt.error for attempt in failures if attempt.model == failed.model_name and failed.error and (failed.error == attempt.error or failed.error.startswith((attempt.error or "").split("（", 1)[0] + ":") and (attempt.http_status is None or f"status_code: {attempt.http_status}" in failed.error))),
                                            "此前服务请求失败；安全错误种类和网络状态见本工作逐次请求记录，服务错误原文不用于续办。")) if failed.error else failed for failed in message.failed_attempts]

    def append_run_messages(self, messages: list[ModelMessage]) -> None:
        self.safe_failure_descriptions(messages)
        added = deepcopy(messages[self.saved_run_messages:])
        if self.prompt_origin == "recovery_control" and self.saved_run_messages == 0 and added:
            initial = added[0]
            prompts = [index for index, part in enumerate(initial.parts) if isinstance(part, UserPromptPart)]
            if not isinstance(initial, ModelRequest) or len(prompts) != 1:
                raise ContextPreparationError("恢复控制提示缺少唯一原始部件，未发送；请核对保存记录。")
            part_index = prompts[0]
            origin = RecoveryControlPrompt(task_id=self.task.id, source=SourcePart(message_index=len(self.conversation._record.messages),
                part_index=part_index, sha256=part_hash(initial.parts[part_index])))
            initial.metadata = {**(initial.metadata or {}), "myrk_recovery_control": origin.model_dump()}
        self.conversation._record.messages.extend(added)
        self.saved_run_messages = len(messages)

    def save_request_progress(self) -> None:
        on_update = self.on_update
        try:
            self.conversation.save()
        except OSError:
            if on_update is not None:
                on_update(Update("status", self.conversation.save_failures[-1]))
            if self.scope is not None or self.prompt_origin == "recovery_control" or self.conversation._starting_queued_task is not None:
                raise

    def progress_tick(self) -> None:
        on_update = self.on_update
        try:
            self.save_request_progress()
        except OSError:
            self.budget.reason = self.conversation.save_failures[-1]
            self.budget.token.cancel()
        if on_update is not None:
            on_update(Update("task", task=self.task.model_copy(deep=True)))

    async def received(self, ctx: RunContext[None], *, request_context: ModelRequestContext, response: ModelResponse) -> ModelResponse:
        turn = self.turn
        attempt = turn.request_attempts[-1]
        assert self.budget.counter.work_step is not None
        self.budget.counter.work_step.request_pending = False
        incomplete = google_response_error(response) if self.task.provider_config.protocol == "google" else None
        attempt.status = "failed" if incomplete and response.finish_reason != "length" else "completed"
        attempt.error = incomplete if attempt.status == "failed" else None
        attempt.decision = incomplete or "服务回应已收到。"
        record_response_usage(attempt, response.usage)
        self.append_run_messages([*ctx.messages[self.history_length:], response])
        self.save_request_progress()
        if not incomplete:
            assert self.budget.counter.work_step is not None
            self.budget.counter.work_step.completed = True
            self.save_request_progress()
        if incomplete:
            if response.finish_reason == "length":
                raise UsageLimitExceeded(incomplete)
            raise IncompleteGoogleResponse(request_context.model.model_name, incomplete)
        return response

    async def received_review(self, ctx: RunContext[None], *, request_context: ModelRequestContext, response: ModelResponse) -> ModelResponse:
        turn = self.turn
        attempt = turn.request_attempts[-1]
        assert self.budget.counter.work_step is not None
        self.budget.counter.work_step.request_pending = False
        incomplete = google_response_error(response) if self.task.provider_config.protocol == "google" else None
        attempt.status = "failed" if incomplete else "completed"
        attempt.error = incomplete
        attempt.decision = incomplete or "复盘服务回应已收到。"
        record_response_usage(attempt, response.usage)
        self.safe_failure_descriptions([*ctx.messages, response])
        turn.memory_review_messages = deepcopy([*ctx.messages[self.review_history_length:], response])
        self.save_request_progress()
        if not incomplete:
            assert self.budget.counter.work_step is not None
            self.budget.counter.work_step.completed = True
            self.save_request_progress()
        if incomplete:
            raise IncompleteGoogleResponse(request_context.model.model_name, incomplete)
        return response
