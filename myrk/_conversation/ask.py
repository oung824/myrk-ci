import asyncio
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic_ai import Agent, CancellationToken, RunCancelled
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import ToolReturnPart, repair_messages
from pydantic_ai.models import Model
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from myrk._conversation.context_tools import register_input_tools, register_memory_tools
from myrk._conversation.layout import prepare_layout
from myrk._conversation.progress import TurnProgress
from myrk._conversation.project_tools import register_project_tools
from myrk._conversation.records import DocumentReadError, Task, TaskScope, Turn, Update
from myrk._conversation.requests import ObserveRequest, TurnRequests
from myrk._conversation.settlement import finish_turn, record_model_failure
from myrk._conversation.setup import prepare_actions, start_turn, task_instructions
from myrk._conversation.stream import RunStreamProgress, approve_history_reuse, run_memory_review, run_model_stream
from myrk._conversation.supplements import SupplementDelivery
from myrk.actions import Action
from myrk.context import ContextPreparationError, CurrentContext, LayoutChange, RequestLayout
from myrk.executor import POWERSHELL
from myrk.extensions import ExtensionCatalog
from myrk.memory import MemoryStore
from myrk.model_controls import reasoning_settings
from myrk.permissions import ApprovalSettings
from myrk.project import project_guidance
from myrk.task_control import TaskBudget, persist_supplement_selection

if TYPE_CHECKING:
    from myrk.conversation import Conversation


async def ask_conversation(
    self: "Conversation",
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
    extension_catalog: type[ExtensionCatalog],
    observe: ObserveRequest,
    core_tool_contract_version: int,
    child_factory: type["Conversation"],
) -> Turn:
    task, scope, document, reasoning, thinking, resuming, budget = start_turn(
        self, prompt, document=document, model=model, cancellation_token=cancellation_token, thinking=thinking,
        reasoning=reasoning, request_limit=request_limit, scope=scope, task=task, new_task=new_task,
        budget=budget, active_seconds_limit=active_seconds_limit, delegation_id=_delegation_id,
        review_model=review_model, review_model_resolver=review_model_resolver, prompt_origin=_prompt_origin)

    unattended = False
    supplements = SupplementDelivery(self, task, budget, on_update, _delegation_id, _save_supplement_owner)
    instructions, selected_path = task_instructions(task, scope, document, resuming)
    authorized_text = ""

    def read_authorized_text() -> str:
        """Read the single text document authorized for this turn."""
        return authorized_text

    turn = Turn(prompt=prompt, answer="", thinking=thinking, reasoning=reasoning,
                request_owner_id=_delegation_id, request_account=budget.counter if _delegation_id else None,
                execution_scope=scope.model_copy(deep=True) if scope is not None else None,
                project_trusted=scope is not None and project_trusted,
                approval_settings=(_approval_settings or task.approval).model_copy(deep=True),
                task=task, delegations=[work.model_copy(deep=True) for work in {
                    work.id: work for previous in self.turns if previous.task is not None and previous.task.id == task.id
                    for work in previous.delegations
                }.values()])
    self._record.turns.append(turn)
    self._live_turns.append(turn)
    progress = TurnProgress(self, turn, task, budget, scope, on_update, _prompt_origin)
    requests = TurnRequests(progress, supplements, current_context=lambda: current_context, layout=lambda: layout,
        is_unattended=lambda: unattended, observe=observe, core_tool_contract_version=core_tool_contract_version,
        delegation_id=_delegation_id)
    executor, current_context, owned_paths, file_executor, network_decision = prepare_actions(
        self, task=task, turn=turn, scope=scope, budget=budget, model=model, review_model=review_model,
        review_model_resolver=review_model_resolver, approve_action=approve_action, on_update=on_update,
        delegation_id=_delegation_id, supplements=supplements, prepared_request=lambda: requests.prepared_request,
        refresh_after_summary=requests.refresh_after_summary)
    memories = MemoryStore(self.path.parent / "memories")
    project = str(Path(scope.root).resolve()) if scope is not None else None
    layout, boundary, refresh_memory = prepare_layout(
        self, task=task, turn=turn, scope=scope, model=model, reasoning=reasoning, thinking=thinking,
        selected_path=selected_path, delegation_id=_delegation_id, memories=memories, project=project,
        instructions=instructions, current_context=current_context,
        legacy_pending_sources=supplements.legacy_pending_sources, current_layout=lambda: layout,
        core_tool_contract_version=core_tool_contract_version)
    agent = Agent[None, str](model, model_settings=reasoning_settings(task.provider_config, reasoning, legacy_thinking=thinking), instructions=layout.instructions,
                            capabilities=[Hooks[None](before_model_request=requests.before_request, prepare_model_request=requests.prepare_context,
                                after_model_request=progress.received, model_request_error=requests.request_error, before_tool_execute=requests.before_tool_execute,
                                after_node_run=supplements.schedule_late_supplements)])
    extensions = extension_catalog(on_schema_refresh_required=lambda: layout.record_change(['extension_schema_refresh_required']))
    extensions.install(agent)

    @agent.instructions(name="execution-state", on_change="append")
    def actual_execution_state(_ctx: RunContext[None]) -> str:
        settings = turn.approval_settings or task.approval
        approval = json.dumps({"condition": settings.condition, "reviewer": settings.reviewer,
            "sandbox": settings.access.sandbox, "read_only": settings.access.read_only,
            "review_mandatory": settings.review_mandatory}, ensure_ascii=False, sort_keys=True)
        return (self.execution_facts(task.id)
            + '\n当前动作批准设置（程序事实，只适用于后续新动作，不增加任务授权）：\n'
            + '<harness_data origin="myrk_action_approval">\n' + approval + '\n</harness_data>')

    @agent.instructions(name="action-approval-protocol", on_change="append")
    def action_approval_protocol() -> str:
        return (
            '执行已授权的任务时，先调用相应已注册的文件或发送工具，由程序准备确切动作并按当前设置处理批准。'
            '当前动作批准设置见程序提供的myrk_action_approval数据；reviewer=user表示由本人决定，'
            'reviewer=assistant表示由程序调用只读的独立动作审核。独立动作审核针对这项已准备动作作决定，'
            'delegate_work用于办理子任务，不能代替动作批准，也不需要为批准另建报告文件。'
            '文字询问不等于已提交待批动作或取得批准。condition=never表示当前配置免普通逐项批准，'
            '只读上限、明确禁止和工具返回的限制仍有效；已准备动作和待批候选继续使用其原设置。'
            '需要发送已读文件且已注册send_text时，调用send_text(path,url)，程序绑定该已读版本的原字节、'
            '内容指纹和确切地址，并在需要时捕获对具体内容与地址的批准，无需为准备发送额外运行命令；'
            '拒绝或失败后按原规则处理。'
            f'当前程序执行环境为Windows；PowerShell 7可执行文件为{POWERSHELL}。'
            '需要运行PowerShell命令时使用该完整程序路径，并分别传递参数；程序按实际提交的程序和参数执行，不替换已准备的程序。')
    if scope is not None:
        @agent.instructions(name='artifact-delivery', on_change='append')
        def artifact_delivery_protocol() -> str:
            return (
                '需要交付命令生成或修改的文件时，先用search_extensions查找run_command_with_outputs，'
                '再用describe_extension查看定义，通过call_extension调用，并在执行前声明确切产物文件；声明不增加写入权限。'
                '原生写入或此命令返回真实文件改动证据后，用同样扩展入口发现、描述并调用deliver_files，'
                '把证据编号作为change_ids明确交付；写入成功或回答中的文件路径不等于已登记成果。'
                '只交付用户实际需要的文件，不交付读入资料、备份、临时文件和日志。'
                '普通run_command未逐文件观察，不能事后补造改动证据或登记产物；失败、停止和未变化的文件不能冒充成功交付。'
            )
    loaded_project_guidance = ""
    @agent.instructions
    def applicable_project_guidance() -> str:
        return loaded_project_guidance

    memory = register_memory_tools(agent, conversation=self, executor=executor, memories=memories, project=project,
        is_unattended=lambda: unattended, turn=turn, delegation_id=_delegation_id, budget=budget,
        seen_supplements=supplements.seen_supplements, task=task)
    register_input_tools(agent, read_authorized_text=read_authorized_text if document is not None else None,
        budget=budget, seen_supplements=supplements.seen_supplements, current_context=lambda: current_context)
    discover_skills = register_project_tools(agent, extensions, conversation=self, child_factory=child_factory,
        scope=scope, task=task, turn=turn, budget=budget, seen_supplements=supplements.seen_supplements,
        executor=executor, file_executor=file_executor, owned_paths=owned_paths, network_decision=network_decision,
        on_update=on_update, delegation_id=_delegation_id, model=model, thinking=thinking, reasoning=reasoning,
        approve_action=approve_action, review_model=review_model,
        review_model_resolver=review_model_resolver) if scope is not None else None

    stream_progress = RunStreamProgress()
    usage: RunUsage | None = None
    usage_complete = False
    timer = asyncio.create_task(budget.watch(progress.progress_tick))
    before_requests = budget.requests
    attachment_read_failed = False
    try:
        if _delegation_id is None:
            budget.set_supplement_admission(True)
        if scope is not None and not task.approval.access.legacy_scope:
            loaded_project_guidance = await project_guidance(Path(scope.root).resolve(), trusted=turn.project_trusted,
                executor=file_executor, access=turn.approval_settings.access if turn.approval_settings else task.approval.access,
                notes=turn.context_notes)
        if scope is not None and (not task.approval.access.legacy_scope or "read" in scope.allowed_actions):
            assert discover_skills is not None
            await discover_skills()
        await approve_history_reuse(self, task=task, turn=turn, scope=scope, project=project, executor=executor)
        if selected_path is not None:
            try:
                authorized_text = (await file_executor.read_bytes(selected_path, selected_path.parent, task.approval.access)).decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            except (UnicodeError, OSError) as error:
                attachment_read_failed = True
                raise DocumentReadError(
                    "无法读取所选资料；请确认文件可读，并另存为 UTF-8（常见文字保存格式）后重试。"
                ) from error
        pending_history = [row for owner, row in supplements.legacy_pending_sources.values()
                           if owner == task.id and row.revoked_at is None]
        if persist_supplement_selection(pending_history, supplements.save_supplement_state):
            supplements.publish_task()
        history = repair_messages(deepcopy(supplements.material_history()))
        progress.history_length = len(history)
        retained_parts = [part for message in history for part in message.parts]
        if any(isinstance(part, ToolReturnPart) and part not in retained_parts
               for message in self._record.messages for part in message.parts):
            raise ContextPreparationError(
                "原始工具结果缺少可靠的请求配对，现有服务无法接收；已停止且未发送新请求。"
                "原始证据和未完成事项仍保留，请先核对对应请求，不能靠删掉结果继续。"
            )
        usage = await run_model_stream(self, stream_progress, agent=agent, prompt=prompt, history=history,
            budget=budget, turn=turn, task=task, scope=scope, on_update=on_update, append_run_messages=progress.append_run_messages)
        usage_complete = not usage.unmeasured_requests
        usage_complete = usage_complete and not any(item.status == "failed" for item in turn.request_attempts)
        if _delegation_id is None and task.memory_review_requested and not task.memory_reviews:
            task.memory_reviews = 1
            if (budget.request_limit is not None and budget.requests >= budget.request_limit) or (budget.max_turns is not None and budget.turns_used >= budget.max_turns) or budget.token.cancelled:
                turn.context_notes.append("没有剩余请求额度或工作已停止，跳过交付后有限复盘；没有额外请求。")
            else:
                unattended = True
                review_history = supplements.material_history()
                progress.review_history_length = len(review_history)
                review_instructions = layout.instructions + "\n交付后有限复盘：只整理本任务已获准资料中的精简事实。新增可自动保存；修改或删除已有条目只形成待审提议。无需新笔记时直接结束，不再安排复盘。"
                previous_review = self._record.request_layouts.get("memory_review")
                version = previous_review.version + 1 if previous_review else 1
                layout = RequestLayout(task_id=task.id, version=version, instructions=review_instructions, core_contract_version=core_tool_contract_version,
                    boundary=boundary, memory_block=layout.memory_block, observed_version=version - 1,
                    changes=[*(previous_review.changes if previous_review else []), LayoutChange(version=version, reasons=["new_task"])])
                self._record.request_layouts["memory_review"] = layout
                current_context = CurrentContext(self.path, len(self._record.turns) - 1, self._record.prepared_contexts,
                    turn.context_notes, lambda text: on_update(Update("status", text)) if on_update is not None else None,
                    refresh_instructions=refresh_memory,
                    source_task_ids=lambda: {item.task.id for item in self._record.turns if item.task is not None})
                current_context.layout = layout
                review_agent = Agent[None, str](model, model_settings=reasoning_settings(task.provider_config, reasoning, legacy_thinking=thinking),
                    instructions=review_instructions,
                    tools=[memory], capabilities=[Hooks[None](before_model_request=requests.before_request, prepare_model_request=requests.prepare_context, after_model_request=progress.received_review, model_request_error=requests.request_error, before_tool_execute=requests.before_tool_execute)])
                review_agent.instructions(name="execution-state", on_change="append")(actual_execution_state)
                usage_complete = await run_memory_review(review_agent=review_agent, review_history=review_history,
                    budget=budget, turn=turn, usage=usage, usage_complete=usage_complete, on_update=on_update,
                    save=self.save, safe_failure_descriptions=progress.safe_failure_descriptions)
            if turn.actions:
                turn.delivery_status = "awaiting_acceptance"
        usage_complete = usage_complete and not any(item.status == "failed" for item in turn.request_attempts)
    except asyncio.CancelledError:
        turn.status = "limited" if budget.reason else "cancelled"
        turn.error = budget.reason
        if stream_progress.handle is not None and stream_progress.started:
            self._record.messages.extend(stream_progress.handle.new_messages())
        raise
    except RunCancelled as cancelled:
        cancelled_messages = cancelled.new_messages()
        if cancelled_messages:
            progress.append_run_messages(cancelled_messages)
        turn.status = "limited" if budget.reason else "cancelled"
        turn.error = budget.reason
        usage = cancelled.usage
    except Exception as error:
        usage, usage_complete = record_model_failure(error, turn=turn, budget=budget, progress=stream_progress,
            append_run_messages=progress.append_run_messages, usage=usage, usage_complete=usage_complete)
    finally:
        await finish_turn(self, budget=budget, file_executor=file_executor, timer=timer, turn=turn, task=task,
            before_requests=before_requests, resuming=resuming, attachment_read_failed=attachment_read_failed,
            usage=usage, usage_complete=usage_complete, delegation_id=_delegation_id)
    return turn
