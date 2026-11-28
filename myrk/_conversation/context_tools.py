import json
from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRetry

from myrk._conversation.records import Task, Turn
from myrk.actions import Action, ActionExecutor, folder_write_lock
from myrk.context import CurrentContext
from myrk.memory import MemoryChange, MemoryStore
from myrk.task_control import TaskBudget

if TYPE_CHECKING:
    from myrk.conversation import Conversation


def register_memory_tools(
    agent: Agent[None, str],
    *,
    conversation: "Conversation",
    executor: ActionExecutor,
    memories: MemoryStore,
    project: str | None,
    is_unattended: Callable[[], bool],
    turn: Turn,
    delegation_id: str | None,
    budget: TaskBudget,
    seen_supplements: set[str],
    task: Task,
) -> Callable[..., Awaitable[str]]:
    @agent.tool_plain(prepare=lambda ctx, definition: definition if delegation_id is None else None)
    async def memory(action: Literal["add", "replace", "patch", "remove", "batch", "list"], text: str = "", old_text: str = "", replacement: str = "", scope: Literal["personal", "project"] = "personal", changes: list[MemoryChange] | None = None) -> str:
        """Proactively save concise useful personal preferences or current-project experience; notes never grant authority."""
        selected = changes or []
        if action != "batch" and action != "list":
            selected = [MemoryChange(action=action, text=text, old_text=old_text, replacement=replacement, scope=scope)]
        async def change(_action: Action) -> str:
            if action == "list":
                return memories.background(project, turn.context_notes)
            if not selected:
                raise ValueError("请提供至少一项事实笔记动作。")
            async with folder_write_lock(str(memories.directory)):
                return memories.change(selected, project)
        return await executor.run("memory", scope, "事实笔记动作：" + json.dumps([item.model_dump() for item in selected], ensure_ascii=False) + "；待审提议可能含完整原文。不会改变本任务允许范围。", change,
                                  requires_approval=action != "list" and (memories.read().approve_writes or any(item.action == "remove" or is_unattended() and item.action != "add" for item in selected)), memory_changes=selected or None, memory_project=project, defer_approval=is_unattended())

    @agent.tool_plain(prepare=lambda ctx, definition: definition if delegation_id is None else None)
    async def queue_memory_review() -> str:
        """Request at most one post-delivery review of useful unsaved experience, using this task's remaining request/time/stop budget."""
        budget.checkpoint(applied_supplements=seen_supplements)
        if task.memory_reviews:
            return "这项工作已经进行过一次有限整理，不再安排。"
        task.memory_review_requested = True
        conversation.save()
        return "已安排交付后按需进行一次有限整理，使用本任务剩余额度，无额度则跳过。"

    @agent.tool_plain(prepare=lambda ctx, definition: definition if delegation_id is None else None)
    async def recall_project_memory(project: str) -> str:
        """Use another project's saved notes only after confirmation of this exact source and recipient; never read arbitrary project files."""
        source = str(Path(project).resolve())
        provider = task.provider_config
        recipient_project = str(Path(task.scope.root).resolve()) if task.scope else None
        empty_result = "该项目没有可取用的笔记。"
        prepared_content = ""

        async def prepare(action: Action) -> None:
            nonlocal prepared_content
            prepared_content = memories.background(source, turn.context_notes)
            action.approval_preview = prepared_content or empty_result
            action.prepared_parameters = {
                "source_project": source,
                "recipient_project": recipient_project,
                "recipient": {"name": provider.name, "protocol": provider.protocol, "base_url": provider.base_url, "model": provider.model},
                "content_sha256": sha256(action.approval_preview.encode("utf-8")).hexdigest(),
            }

        async def recall(_action: Action) -> str:
            if (task.provider_config.identity != provider.identity
                    or str(Path(project).resolve()) != source
                    or memories.background(source) != prepared_content):
                raise ModelRetry("待取用的笔记、来源或接收方已变化，请重新申请批准。")
            return prepared_content or empty_result

        return await executor.run(
            "memory_recall", source,
            f"跨项目取用笔记：{source}。接收服务为{provider.name}，协议{provider.protocol}，地址{provider.base_url}，型号{provider.model}。",
            recall, requires_approval=True, prepare_operation=prepare,
        )
    return memory


def register_input_tools(
    agent: Agent[None, str],
    *,
    read_authorized_text: Callable[[], str] | None,
    budget: TaskBudget,
    seen_supplements: set[str],
    current_context: Callable[[], CurrentContext],
) -> None:
    if read_authorized_text is not None:
        agent.tool_plain(read_authorized_text)

    @agent.tool_plain
    async def read_context_evidence(evidence_id: str, start: int = 0, length: int = 4000, query: str = "") -> str:
        """Read omitted original text by evidence ID and character range; optionally find literal query from start. Arbitrary paths are refused."""
        budget.checkpoint(applied_supplements=seen_supplements)
        return current_context().read_evidence(evidence_id, start, length, query)
