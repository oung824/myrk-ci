from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import (
    InstructionBaselinePart,
    InstructionDeltaPart,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from myrk._conversation.records import ConversationRecord, ProviderChange, Task, Turn
from myrk.model_controls import chat_models
from myrk.provider import ProviderConfig

if TYPE_CHECKING:
    from myrk.conversation import Conversation


def preview_provider_change(self: Conversation, task_id: str, target: ProviderConfig) -> ProviderChange:
    previous = self.turns[-1] if self.turns else None
    if previous is not None and previous.request_owner_id is not None:
        raise ValueError("这是子助理执行记录，只供回查；请回到原主聊天继续，或明确新建工作，不能变成不受限的主助理。")
    if previous is None or previous.task is None or previous.task.id != task_id or not (previous.service_failure or previous.status == "running"):
        raise ValueError("请先选择已停止且需要续办的原工作。")
    report = self.recovery
    returned = {part.tool_call_id for message in self._record.messages for part in message.parts if isinstance(part, ToolReturnPart)}
    unfinished = [part for message in self._record.messages for part in message.parts if isinstance(part, ToolCallPart) and part.tool_call_id not in returned]
    known_calls = {action.tool_call_id for action in previous.actions if action.tool_call_id is not None}
    if unfinished and report is not None and report.can_resume and all(part.tool_call_id in known_calls for part in unfinished):
        self.prepare_resume(task_id)
    compatible, reason = self._provider_history_compatibility(target, previous.task.provider_config.model)
    target_model = next(model for model in chat_models(target) if model.id == target.model)
    supported = {option.id for option in target_model.reasoning_options}
    target_reasoning: str | None = previous.task.reasoning if previous.task.reasoning in supported else target_model.default_reasoning
    if target_reasoning != previous.task.reasoning:
        reason += " 原推理档位不能用于目标型号，将使用目标型号的初始推理设置。"
    if report is not None and not report.can_resume:
        compatible, reason = False, "原工作仍有影响未知的真实动作；请先核查，不能借更换服务盲目重跑。"
    if target_model.tools_supported is False:
        compatible = False
        reason += " " + (target_model.unavailable_reason or "目标型号不支持当前办事流程的工具调用。")
        target_reasoning = None
    materials = self._provider_materials(previous.task)
    change = ProviderChange(task_id=task_id, previous=previous.task.provider_config, target=target, materials=materials, compatible=compatible, reason=reason, target_reasoning=target_reasoning)
    self._record.provider_changes.append(change)
    self.save()
    return change


def _provider_history_compatibility(record: ConversationRecord, target: ProviderConfig, source_model: str) -> tuple[bool, str]:
    reason = "只支持已列普通文字与完整动作配对；这项格式核对不证明新服务实际可用或回答质量。"
    compatible = True
    pending: dict[str, str] = {}
    for message in record.messages:
        if isinstance(message, ModelResponse) and (message.state != "complete" or message.provider_details and set(message.provider_details) - {"finish_reason", "timestamp"}):
            compatible, reason = False, "历史含未验证可迁移的服务状态或附带信息；不能丢字段续办。"
        for part in message.parts:
            if isinstance(part, (ToolCallPart, TextPart)) and (part.id or part.provider_details):
                compatible, reason = False, "历史含必要动作或文字的服务附带编号、信息；新型号尚未证明可保留，已停止。"
            if isinstance(part, ToolCallPart):
                if not part.tool_call_id or part.tool_call_id in pending:
                    compatible, reason = False, "动作请求编号缺失或重复，不能可靠续办。"
                pending[part.tool_call_id] = part.tool_name
            elif isinstance(part, ToolReturnPart):
                if pending.pop(part.tool_call_id, None) != part.tool_name:
                    compatible, reason = False, "动作结果缺少同编号、同动作的请求配对，不能删除字段强行继续。"
            elif isinstance(part, ThinkingPart):
                if target.protocol != "deepseek" or target.model != source_model or part.signature or part.id or part.provider_details or part.provider_name not in {None, "deepseek"}:
                    compatible, reason = False, "历史含新型号不能可靠接收的额外思考或附带编号、签名；已保留原文并停止，不删字段续办。"
            elif isinstance(part, (InstructionBaselinePart, InstructionDeltaPart)):
                continue
            elif not isinstance(part, (TextPart, UserPromptPart, SystemPromptPart)) or not isinstance(part.content, str):
                compatible, reason = False, "历史含尚未验证可传递的必要内容格式；已停止，不静默转换或丢字段。"
    if pending:
        compatible, reason = False, "尚有未配对动作；请先按原恢复过程核查影响，再准备更换服务。"
    return compatible, reason


def _provider_materials(record: ConversationRecord, turns: tuple[Turn, ...], task: Task) -> str:
    return json.dumps({"task": task.model_dump(), "messages": record.model_dump(mode="json")["messages"],
                            "progress": [turn.model_dump(mode="json") for turn in turns if turn.task and turn.task.id == task.id],
                            "applicable_notes": record.memory_contexts.get(str(Path(task.scope.root).resolve()) if task.scope else "personal", ""),
                            "single_document": task.document,
                            "impact": "以上必要历史、当前适用事实笔记、原允许范围内按需读取的资料及后续动作结果将发给所列服务地址；之后按原规则重读的适用笔记也在此允许用途内。不扩大文件范围，不重做已完成动作，次数及主动时间延用。"}, ensure_ascii=False, indent=2)


def decide_provider_change(self: Conversation, change_id: str, approved: bool) -> Task | None:
    change = next((item for item in self._record.provider_changes if item.id == change_id), None)
    previous = self.turns[-1] if self.turns else None
    if change is None or change.approved is not None or previous is None or previous.task is None or previous.task.id != change.task_id or previous.task.provider_config != change.previous:
        raise ValueError("这份批准不对应当前原工作或已使用，未更换服务。")
    if approved and self._provider_materials(previous.task) != change.materials:
        raise ValueError("必要资料或进展已在审阅后变化；请重新审阅，未向新接收方发送。")
    if approved and not change.compatible:
        raise ValueError(change.reason)
    report = self.recovery
    if approved and report is not None and not report.can_resume:
        raise ValueError("原工作仍有影响未知的动作；请先核查，没有更换接收方。")
    change.approved = approved
    if approved:
        previous.task.provider_config = change.target
        if change.target_reasoning is not None:
            previous.task.reasoning = change.target_reasoning
            previous.task.legacy_thinking = None
        if previous.task.scope is not None:
            scope = previous.task.scope
            project = str(Path(scope.root).resolve())
            sources = {str(Path(turn.task.scope.root).resolve()) for turn in self.turns if turn.task and turn.task.scope}
            sources.discard(project)
            history_key = json.dumps([project, scope.provider, scope.recipients, change.target.identity], ensure_ascii=False)
            self._record.history_uses[history_key] = sorted(sources)
    self.save()
    return previous.task if approved else None
