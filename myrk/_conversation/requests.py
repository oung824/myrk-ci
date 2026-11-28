from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Protocol

from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.exceptions import ModelContextWindowExceededError, ModelHTTPError, RetryModelRequest, ToolFailed, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_core import to_json

from myrk._conversation.progress import TurnProgress
from myrk._conversation.records import Update
from myrk._conversation.supplements import SupplementDelivery
from myrk.context import ContextPreparationError, CurrentContext, RequestLayout
from myrk.provider import is_temporary_model_error
from myrk.task_control import RequestAttempt, WorkStep


class ObserveRequest(Protocol):
    def __call__(self, model: Model, attempt: RequestAttempt, *, layout_version: int | None = None,
                 change_reasons: list[str] | None = None) -> Model: ...


class TurnRequests:
    def __init__(self, progress: TurnProgress, supplements: SupplementDelivery, *,
                 current_context: Callable[[], CurrentContext], layout: Callable[[], RequestLayout],
                 is_unattended: Callable[[], bool], observe: ObserveRequest, core_tool_contract_version: int,
                 delegation_id: str | None) -> None:
        self.progress = progress
        self.supplements = supplements
        self.current_context = current_context
        self.layout = layout
        self.is_unattended = is_unattended
        self.observe = observe
        self.core_tool_contract_version = core_tool_contract_version
        self.delegation_id = delegation_id
        self.prepared_request = 0
        self.supplement_snapshot: dict[str, str] = {}

    async def before_request(self, ctx: RunContext[None], request: ModelRequestContext) -> ModelRequestContext:
        layout = self.layout()
        request = self.supplements.adopt_supplements(ctx, request)
        request = await self.current_context().before_model_request(ctx, request)
        rewrite = [part for part in (request.model_request_parameters.instruction_parts or [])
                   if part.on_change == "rewrite" and str(part.id) != "agent"]
        if layout.tools_initialized and layout.rewrite_instructions != rewrite:
            layout.record_change(["instructions_changed"])
        layout.rewrite_instructions = deepcopy(rewrite)
        return request

    async def prepare(self, ctx: RunContext[None], request: ModelRequestContext) -> ModelRequestContext:
        budget = self.progress.budget
        turn = self.progress.turn
        task = self.progress.task
        on_update = self.progress.on_update
        layout = self.layout()
        assert budget.counter.work_step is not None
        turn_key = budget.counter.work_step.id
        budget.check_turn(turn_key)
        self.prepared_request = budget.reserve_request()
        budget.reserve_turn(turn_key)
        if budget.max_turns is not None and budget.turns_used == budget.max_turns:
            request.model_request_parameters = replace(request.model_request_parameters, function_tools=[])
        parameters = request.model.customize_request_parameters(request.model_request_parameters)
        tools = [*parameters.function_tools, *parameters.output_tools]
        tools_changed = to_json(layout.tools) != to_json(tools)
        if layout.tools_initialized and (tools_changed or layout.core_contract_version != self.core_tool_contract_version):
            final_turn = not tools and budget.max_turns is not None and budget.turns_used == budget.max_turns
            layout.record_change(["final_turn_tools_removed" if final_turn else "core_tools_changed"])
            if not final_turn:
                previous = {tool.name: tool for tool in layout.tools}
                current = {tool.name: tool for tool in tools}
                changed = list(dict.fromkeys(tool.name for tool in [*layout.tools, *tools]
                                             if to_json(previous.get(tool.name)) != to_json(current.get(tool.name))))
                if not changed and tools_changed:
                    changed = [tool.name for index, tool in enumerate(tools) if layout.tools[index].name != tool.name]
                change = layout.changes[-1]
                change.previous_core_version, change.core_version = layout.core_contract_version, self.core_tool_contract_version
                change.changed_tools = changed
        layout.core_contract_version = self.core_tool_contract_version
        layout.tools = deepcopy(tools)
        layout.tools_initialized = True
        turn.request_attempts.append(RequestAttempt(number=self.prepared_request, own_number=budget.requests, step_id=budget.counter.work_step.id, model=request.model.model_name, provider=request.model.base_url or request.model.system, delegation_id=self.delegation_id, actor="memory_review" if self.is_unattended() else "delegation" if self.delegation_id else "main"))
        request.model = self.observe(request.model, turn.request_attempts[-1], layout_version=layout.version,
            change_reasons=list(dict.fromkeys(reason for change in layout.changes if change.version > layout.observed_version for reason in change.reasons)))
        layout.observed_version = layout.version
        self.current_context().prepared[-1].task_id = task.id
        self.current_context().prepared[-1].request_attempt = self.prepared_request
        if self.is_unattended():
            turn.memory_review_messages = deepcopy(ctx.messages[self.progress.review_history_length:])
        else:
            self.progress.append_run_messages(ctx.messages[self.progress.history_length:])
        budget.counter.work_step.request_pending = True
        applied_supplements = [item for item in task.active_supplements
                               if item.id in self.supplements.seen_supplements and item.applied_request is None]
        for item in applied_supplements:
            item.applied_request = self.prepared_request
        try:
            if applied_supplements:
                self.supplements.save_supplement_state()
            else:
                self.progress.conversation.save()
        except Exception:
            for item in applied_supplements:
                item.applied_request = None
            raise
        if on_update is not None:
            on_update(Update("task", task=task.model_copy(deep=True)))
            on_update(Update("status", "正在等待回答服务"))
        return request

    async def prepare_context(self, ctx: RunContext[None], request: ModelRequestContext) -> ModelRequestContext:
        budget = self.progress.budget
        task = self.progress.task
        step = budget.counter.work_step
        if step is None or step.completed:
            step = WorkStep()
            budget.counter.work_step = step
        next_turn = budget.check_turn(step.id)
        request = self.supplements.adopt_supplements(ctx, request)
        self.supplement_snapshot = {item.id: item.text for item in task.active_supplements}
        self.progress.safe_failure_descriptions(request.messages)
        if not self.is_unattended():
            self.progress.append_run_messages(ctx.messages[self.progress.history_length:])
            self.progress.conversation.save()
        final_turn = budget.max_turns is not None and next_turn == budget.max_turns
        if final_turn:
            request.model_request_parameters = replace(request.model_request_parameters, function_tools=[])
        request = await self.current_context().prepare_model_request(ctx, request)
        if any(item.id not in self.supplements.seen_supplements for item in task.active_supplements):
            request.messages = list(ctx.messages)
            self.refresh_after_summary(ctx, request)
            request = await self.current_context().prepare_model_request(ctx, request)
        return await self.prepare(ctx, request)

    def refresh_after_summary(self, ctx: RunContext[None], request: ModelRequestContext) -> None:
        task = self.progress.task
        self.supplements.adopt_supplements(ctx, request)
        if not self.is_unattended():
            self.progress.append_run_messages(ctx.messages[self.progress.history_length:])
            self.progress.conversation.save()
        current = {item.id: item.text for item in task.active_supplements}
        if any(current.get(identifier) != text for identifier, text in self.supplement_snapshot.items()):
            raise ContextPreparationError("整理期间已有补充被修改或撤回，旧摘要未提交；原始记录保留，请按当前要求继续。")

    async def request_error(self, ctx: RunContext[None], *, request_context: ModelRequestContext, error: Exception) -> ModelResponse:
        turn = self.progress.turn
        budget = self.progress.budget
        task = self.progress.task
        on_update = self.progress.on_update
        attempt = turn.request_attempts[-1]
        attempt.status = "failed"
        attempt.http_status = error.status_code if isinstance(error, ModelHTTPError) else None
        attempt.error = f"{type(error).__name__}（服务报告的错误种类）" + (f"；HTTP（网络回应状态）{attempt.http_status}" if attempt.http_status is not None else "")
        step = budget.counter.work_step
        if step is not None:
            step.request_pending = False
        if isinstance(error, ModelContextWindowExceededError) and task.provider_config.auto_compaction and step is not None and not step.capacity_recovery_used and not budget.token.cancelled:
            step.capacity_recovery_used = True
            self.current_context().force_compaction = True
            attempt.decision = "服务确认输入窗口超限；在原办事步骤整理一次后重试，不重放已完成动作。"
            self.progress.conversation.save()
            raise RetryModelRequest()
        if isinstance(error, ModelContextWindowExceededError) and step is not None and step.capacity_recovery_used:
            attempt.decision = "本步骤已用一次容量超限自动恢复机会，或缺少可靠记录证明机会仍可用；已暂停，保留原错误和未完成工作，不再次整理或自动发送。"
            turn.context_notes.append(attempt.decision)
            self.progress.save_request_progress()
            raise error
        temporary = is_temporary_model_error(error)
        if temporary and request_context.attempt < 3 and not budget.token.cancelled and (budget.request_limit is None or budget.requests < budget.request_limit):
            attempt.decision = "暂时服务错误；在原工作剩余额度内再尝试一次，最多额外2次。"
            self.progress.save_request_progress()
            if on_update:
                on_update(Update("status", attempt.error + "。" + attempt.decision))
            raise RetryModelRequest()
        attempt.decision = "已停止发送，保留已做、未做与部分回答，等待你手动继续或更换服务。" if temporary else "参数、权限或其他不可确认暂时性的错误；未原样反复发送，等待你处理。"
        self.progress.save_request_progress()
        raise error

    async def before_tool_execute(self, ctx: RunContext[None], *, call: ToolCallPart, tool_def: ToolDefinition,
                                  args: ValidatedToolArgs) -> ValidatedToolArgs:
        budget = self.progress.budget
        try:
            budget.check_action_start()
        except UsageLimitExceeded as error:
            if budget.token.cancelled:
                raise
            raise ToolFailed(str(error)) from error
        return args
