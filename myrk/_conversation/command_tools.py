import json
from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import RunContext

from myrk.actions import Action, folder_write_lock
from myrk.artifacts import FileChange
from myrk.checks import execute_check
from myrk.executor import ExecutorRequestError
from myrk.extensions import ExtensionCatalog
from myrk.permissions import requires_write_approval

if TYPE_CHECKING:
    from myrk._conversation.project_tools import ProjectToolContext


def register_command_tools(
    agent: Agent[None, str],
    extensions: ExtensionCatalog,
    tools: "ProjectToolContext",
    *,
    network_decision: Callable[[Action, dict[str, Any]], Awaitable[bool]],
) -> None:
    async def execute_command(ctx: RunContext[None], program: str, arguments: list[str], cwd: str, timeout_seconds: float,
                              additional_write_paths: list[str] | None, output_paths: list[str] | None = None) -> str:
        directory = (tools.root / cwd).resolve()
        extra = sorted({str((tools.root / path).resolve()) for path in additional_write_paths or []})
        targets = list(dict.fromkeys((directory / path).resolve() for path in output_paths or []))
        originals: dict[Path, bytes | None] = {}
        backups: dict[Path, str] = {}
        async def observe(target: Path, action: Action) -> bytes | None:
            assert action.approval_settings is not None
            try:
                metadata = await tools.file_executor.metadata(target, tools.root, action.approval_settings.access)
            except ExecutorRequestError:
                return None
            if not metadata['isFile']:
                raise ValueError("声明的命令产物不是普通文件，未生成交付证据。")
            return await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
        async def prepare_outputs(action: Action) -> None:
            if output_paths is not None and not targets:
                raise ValueError("请在执行前声明至少一个确切产物文件。")
            for target in targets:
                if any(target.is_relative_to(path.resolve()) for path in [*tools.owned_paths, *tools.file_executor.runtime_roots, tools.file_executor.directory]):
                    raise PermissionError("应用的记录、备份、日志和临时资料不能声明为交付产物。")
                original = await observe(target, action)
                originals[target] = original
                if original is not None:
                    backup = tools.conversation.path.parent / f'{tools.conversation.path.stem}-evidence' / f'{action.id}-{len(backups)}-original.bin'
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(original)
                    backups[target] = str(backup)
            if targets:
                action.prepared_parameters['output_before'] = {str(target): sha256(raw).hexdigest() if raw is not None else None
                    for target, raw in originals.items()}
                action.impact += ' 声明产物执行前内容指纹：' + json.dumps(action.prepared_parameters['output_before'], ensure_ascii=False)
        async def command(action: Action) -> str:
            if tools.task.approval.access.legacy_scope and "check" not in tools.scope.allowed_actions:
                raise PermissionError("本次任务未允许运行程序，未启动。")
            if not program.strip() or not 0 < timeout_seconds <= 60:
                raise ValueError("请提供确切程序，时限须大于零且不超过60秒。")
            if not directory.is_dir():
                raise ValueError("程序工作位置不存在或不是文件夹。")
            assert action.approval_settings is not None
            access = action.approval_settings.access.model_copy(deep=True)
            for target, original in originals.items():
                if await observe(target, action) != original:
                    raise ModelRetry("声明产物在批准或启动前已变化，旧决定已失效；未启动命令，请重新准备。")
            output = await execute_check([program, *arguments], directory, timeout_seconds, action, access=access, protected_paths=tools.owned_paths, extra_write_paths=extra,
                save=tools.conversation.save, evidence_directory=tools.conversation.path.parent / f"{tools.conversation.path.stem}-checks", network_decision=lambda request: network_decision(action, request))
            changes = []
            for target, original in originals.items():
                current = await observe(target, action)
                if current is not None and current != original:
                    changes.append(FileChange(path=str(target), change_kind='created' if original is None else 'modified',
                        before_sha256=sha256(original).hexdigest() if original is not None else None,
                        after_sha256=sha256(current).hexdigest(), size_bytes=len(current), backup_path=backups.get(target)))
            tools.budget.checkpoint(applied_supplements=tools.seen_supplements)
            action.file_changes.extend(changes)
            return output + ('\n文件改动证据：' + json.dumps([change.model_dump() for change in changes], ensure_ascii=False) if output_paths is not None else '')
        async with folder_write_lock(tools.write_root):
            parameters: dict[str, object] = {"program": program, "arguments": arguments, "cwd": str(directory), "timeout_seconds": timeout_seconds, "additional_write_paths": extra}
            if output_paths is not None:
                parameters['output_paths'] = [str(target) for target in targets]
            return await tools.executor.run("command", program, f"运行程序：{program}；参数：{json.dumps(arguments, ensure_ascii=False)}；工作位置：{directory}；时限：{timeout_seconds}秒；额外可写位置：{extra}。" + (f' 执行前声明产物：{[str(target) for target in targets]}。声明不增加可写范围。' if output_paths is not None else ''), command,
                requires_approval=any(requires_write_approval(Path(path), tools.task.approval.access, tools.owned_paths) for path in extra),
                tool_call_id=ctx.tool_call_id, parameters=parameters, prepare_operation=prepare_outputs if output_paths is not None else None,
                replay_completed=output_paths is None)

    @agent.tool(prepare=lambda ctx, definition: definition if not tools.task.approval.access.legacy_scope else None)
    async def run_command(ctx: RunContext[None], program: str, arguments: list[str], cwd: str = ".", timeout_seconds: float = 60,
                          additional_write_paths: list[str] | None = None) -> str:
        """Run a concrete program and arguments with OS limits. Request exact extra writable paths when necessary; never bypass denied reads."""
        return await execute_command(ctx, program, arguments, cwd, timeout_seconds, additional_write_paths)

    @extensions.tool(prepare=lambda ctx, definition: definition if not tools.task.approval.access.legacy_scope else None,
        metadata={'search_keywords': ['命令', '产物', '文件', '生成']})
    async def run_command_with_outputs(ctx: RunContext[None], program: str, arguments: list[str], output_paths: list[str],
                                       cwd: str = '.', timeout_seconds: float = 60,
                                       additional_write_paths: list[str] | None = None) -> str:
        """Run a command with exact output files declared relative to cwd before execution. Observe real changes without granting writes; then explicitly deliver their IDs."""
        return await execute_command(ctx, program, arguments, cwd, timeout_seconds, additional_write_paths, output_paths)

    @agent.tool
    async def run_check(ctx: RunContext[None], name: str) -> str:
        """Run the exact command preselected by the user; never accept generated shell commands."""
        selected = next((check for check in tools.scope.checks if check.name == name), None)
        async def check(action: Action) -> str:
            if tools.task.approval.access.legacy_scope and "check" not in tools.scope.allowed_actions or selected is None:
                raise PermissionError("本次任务未允许这项指定检查，未启动程序。")
            cwd = (tools.root / selected.cwd).resolve(strict=True)
            assert action.approval_settings is not None
            if not cwd.is_relative_to(tools.root) and action.approval_settings.access.sandbox != "full_access":
                raise PermissionError("检查工作位置在任务文件夹之外，未启动程序。")
            return await execute_check(selected.argv, cwd, selected.timeout_seconds, action, access=action.approval_settings.access, protected_paths=tools.owned_paths, save=tools.conversation.save,
                                       evidence_directory=tools.conversation.path.parent / f"{tools.conversation.path.stem}-checks", network_decision=lambda request: network_decision(action, request))
        async with folder_write_lock(tools.write_root):
            return await tools.executor.run("check", name, f"运行用户指定检查：{name}；命令与参数：{selected.argv if selected else '未获准'}；按当前系统访问范围执行。", check, tool_call_id=ctx.tool_call_id,
                                      parameters={"argv": selected.argv, "cwd": selected.cwd, "timeout_seconds": selected.timeout_seconds} if selected else {})
