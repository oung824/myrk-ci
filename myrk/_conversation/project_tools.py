from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.models import Model

from myrk._conversation.command_tools import register_command_tools
from myrk._conversation.delegation_tools import register_delegation_tools
from myrk._conversation.delivery_tools import register_delivery_tools
from myrk._conversation.read_tools import register_read_tools
from myrk._conversation.records import Task, TaskScope, Turn, Update
from myrk._conversation.write_tools import register_write_tools
from myrk.actions import Action, ActionExecutor
from myrk.executor import OfficialExecutor
from myrk.extensions import ExtensionCatalog
from myrk.task_control import TaskBudget

if TYPE_CHECKING:
    from myrk.conversation import Conversation


@dataclass(frozen=True)
class ProjectToolContext:
    conversation: "Conversation"
    scope: TaskScope
    task: Task
    turn: Turn
    root: Path
    write_root: str
    budget: TaskBudget
    seen_supplements: set[str]
    executor: ActionExecutor
    file_executor: OfficialExecutor
    owned_paths: list[Path]
    on_update: Callable[[Update], None] | None
    read_versions: dict[Path, bytes] = field(default_factory=dict)


def register_project_tools(
    agent: Agent[None, str],
    extensions: ExtensionCatalog,
    *,
    conversation: "Conversation",
    child_factory: "type[Conversation]",
    scope: TaskScope,
    task: Task,
    turn: Turn,
    budget: TaskBudget,
    seen_supplements: set[str],
    executor: ActionExecutor,
    file_executor: OfficialExecutor,
    owned_paths: list[Path],
    network_decision: Callable[[Action, dict[str, Any]], Awaitable[bool]],
    on_update: Callable[[Update], None] | None,
    delegation_id: str | None,
    model: Model,
    thinking: bool,
    reasoning: str | None,
    approve_action: Callable[[Action], Awaitable[bool]] | None,
    review_model: Model | None,
    review_model_resolver: Callable[[], Awaitable[Model]] | None,
) -> Callable[[], Awaitable[None]] | None:
    root = Path(scope.root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("任务文件夹不存在或不是文件夹。")
    write_root = str(Path(task.scope.root).resolve(strict=True)) if task.scope else str(root)
    tools = ProjectToolContext(
        conversation=conversation,
        scope=scope,
        task=task,
        turn=turn,
        root=root,
        write_root=write_root,
        budget=budget,
        seen_supplements=seen_supplements,
        executor=executor,
        file_executor=file_executor,
        owned_paths=owned_paths,
        on_update=on_update,
    )
    discover_skills = register_read_tools(agent, tools)
    register_write_tools(agent, tools)
    register_delivery_tools(agent, extensions, tools, delegation_id=delegation_id)
    register_command_tools(agent, extensions, tools, network_decision=network_decision)
    if delegation_id is None:
        register_delegation_tools(
            agent,
            tools,
            child_factory=child_factory,
            model=model,
            thinking=thinking,
            reasoning=reasoning,
            approve_action=approve_action,
            review_model=review_model,
            review_model_resolver=review_model_resolver,
        )
    return discover_skills
