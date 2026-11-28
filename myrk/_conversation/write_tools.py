import re
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import RunContext

from myrk.actions import Action, folder_write_lock
from myrk.artifacts import FileChange
from myrk.executor import FileConflict
from myrk.permissions import requires_write_approval

if TYPE_CHECKING:
    from myrk._conversation.project_tools import ProjectToolContext


def register_write_tools(agent: Agent[None, str], tools: "ProjectToolContext") -> None:
    @agent.tool(prepare=lambda ctx, definition: definition if not tools.task.approval.access.legacy_scope else None)
    async def create_text(ctx: RunContext[None], path: str, content: str) -> str:
        """Create a new UTF-8 text file and necessary parents; an existing file is never replaced."""
        target = (tools.root / path).resolve()
        missing_parents = []
        parent = target.parent
        while not parent.exists():
            missing_parents.append(parent)
            parent = parent.parent
        async def create(action: Action) -> str:
            if tools.task.approval.access.legacy_scope and "edit" not in tools.scope.allowed_actions:
                raise PermissionError("本次任务未允许写入，未新建文件。")
            assert action.approval_settings is not None
            data = content.encode("utf-8")
            action.after_sha256 = sha256(data).hexdigest()
            tools.conversation.save()
            result = await tools.file_executor.create_file(target, data, tools.root, action.approval_settings.access, approved=action.approval_basis in {"user", "assistant"}, create_parents=missing_parents)
            action.sandbox_type = result["sandboxType"]
            tools.read_versions[target] = await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
            if sha256(tools.read_versions[target]).hexdigest() != action.after_sha256:
                raise ValueError("新建文件读回与写入内容不一致，未生成交付证据。")
            change = FileChange(path=str(target), change_kind='created', before_sha256=None,
                after_sha256=action.after_sha256, size_bytes=len(tools.read_versions[target]))
            action.file_changes.append(change)
            return f"已新建：{target}；已有同名文件不会覆盖。\n文件改动证据：{change.model_dump_json()}"
        async with folder_write_lock(tools.write_root):
            return await tools.executor.run("create", str(target), f"新建文件：{target}；必要目录：{[str(parent) for parent in missing_parents]}；创建所需条目的父目录：{parent}，仅在本次固定创建步骤中使用；完整内容：{content}", create,
                requires_approval=requires_write_approval(target, tools.task.approval.access, tools.owned_paths), tool_call_id=ctx.tool_call_id,
                replay_completed=False, parameters={"path": str(target), "content": content, "create_parents": [str(parent) for parent in missing_parents], "creation_parent": str(parent)})

    async def replace_read_file(action: Action, target: Path, transform: Callable[[bytes], bytes], *, operation_name: str, changed_message: Callable[[bytes], str], backup_suffix: str) -> Path:
        """Shared version check, original retention, locked replacement and readback."""
        if tools.task.approval.access.legacy_scope and "edit" not in tools.scope.allowed_actions:
            raise PermissionError("本次任务未允许改写，文件未改变。")
        if target not in tools.read_versions:
            raise ValueError(f"须先读取真实原文再{operation_name}；文件未改变。")
        assert action.approval_settings is not None
        original = await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
        if original != tools.read_versions[target]:
            tools.read_versions[target] = original
            raise ModelRetry(changed_message(original))
        replacement = transform(original)
        backup = tools.conversation.path.parent / f"{tools.conversation.path.stem}-evidence" / f"{action.id}-original.{backup_suffix}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)
        action.backup_path = str(backup)
        action.before_sha256, action.after_sha256 = sha256(original).hexdigest(), sha256(replacement).hexdigest()
        tools.conversation.save()
        try:
            result = await tools.file_executor.replace_existing(target, replacement, action.before_sha256, tools.root, action.approval_settings.access, approved=action.approval_basis in {"user", "assistant"})
        except FileConflict as error:
            raise ModelRetry(str(error)) from error
        action.sandbox_type = result["sandboxType"]
        tools.read_versions[target] = await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
        if sha256(tools.read_versions[target]).hexdigest() != action.after_sha256:
            raise ValueError("写入文件读回与预期内容不一致，未生成交付证据。")
        if original != tools.read_versions[target]:
            action.file_changes.append(FileChange(path=str(target), change_kind='modified',
                before_sha256=action.before_sha256, after_sha256=action.after_sha256,
                size_bytes=len(tools.read_versions[target]), backup_path=str(backup)))
        return backup

    @agent.tool
    async def edit_text(ctx: RunContext[None], path: str, old_text: str, new_text: str) -> str:
        """Replace one exact occurrence in a previously read text file, preserving its original."""
        target = (tools.root / path).resolve(strict=True)
        def precise_replacement(original: bytes) -> bytes:
            text = original.decode("utf-8-sig")
            matches = list(re.finditer(r"(?:\r\n|\r|\n)".join(re.escape(line) for line in re.split(r"\r\n|\r|\n", old_text)), text)) if old_text else []
            if len(matches) != 1:
                raise ValueError("原文片段须唯一匹配，未修改文件。")
            match = matches[0]
            newline = re.search(r"\r\n|\r|\n", match.group() or text) or re.search(r"\r\n|\r|\n", text)
            revised = re.sub(r"\r\n|\r|\n", lambda _: newline.group() if newline else "\n", new_text)
            return (text[:match.start()] + revised + text[match.end():]).encode("utf-8-sig" if original.startswith(b'\xef\xbb\xbf') else "utf-8")
        async def modify(action: Action) -> str:
            backup = await replace_read_file(action, target, precise_replacement, operation_name="精确修改",
                changed_message=lambda original: f"原文已变化，未覆盖。请根据重新读取的当前原文重新判断：\n{original.decode('utf-8-sig')}", backup_suffix="txt")
            return f"已精确修改：{target}；仅替换一处。原件位置：{backup}。" + (f"\n文件改动证据：{action.file_changes[0].model_dump_json()}" if action.file_changes else "\n内容未改变，没有新的文件改动证据。")
        async with folder_write_lock(tools.write_root):
            return await tools.executor.run("edit", str(target), f"在{target}把一处{old_text!r}替换为{new_text!r}，保留原件。", modify, requires_approval=requires_write_approval(target, tools.task.approval.access, tools.owned_paths), tool_call_id=ctx.tool_call_id,
                                      replay_completed=target not in tools.read_versions or old_text not in tools.read_versions[target].decode("utf-8-sig"),
                                      read_sha256=sha256(tools.read_versions[target]).hexdigest() if target in tools.read_versions else None,
                                      parameters={"path": str(target), "old_text": old_text, "new_text": new_text, "read_sha256": sha256(tools.read_versions[target]).hexdigest() if target in tools.read_versions else None})

    @agent.tool(prepare=lambda ctx, definition: definition if not tools.task.approval.access.legacy_scope else None)
    async def write_text(ctx: RunContext[None], path: str, content: str) -> str:
        """Replace a previously read whole text file after checking its exact version; preserve the original."""
        target = (tools.root / path).resolve()
        async def write(action: Action) -> str:
            backup = await replace_read_file(action, target, lambda _: content.encode("utf-8"), operation_name="整份写入",
                changed_message=lambda _: "原文已变化，未覆盖；请重新读取再决定。", backup_suffix="bin")
            return f"已整份写入：{target}；原件位置：{backup}。" + (f"\n文件改动证据：{action.file_changes[0].model_dump_json()}" if action.file_changes else "\n内容未改变，没有新的文件改动证据。")
        async with folder_write_lock(tools.write_root):
            return await tools.executor.run("write", str(target), f"整份写入：{target}；完整内容：{content}；保留原件。", write,
                requires_approval=requires_write_approval(target, tools.task.approval.access, tools.owned_paths), tool_call_id=ctx.tool_call_id,
                replay_completed=False, parameters={"path": str(target), "content": content, "read_sha256": sha256(tools.read_versions[target]).hexdigest() if target in tools.read_versions else None})

    @agent.tool
    async def delete_file(ctx: RunContext[None], path: str) -> str:
        """Delete one concrete version of a file; keep its original and request approval only for expanded access."""
        target = (tools.root / path).resolve()
        prepared_original: bytes | None = None
        async def prepare_delete(action: Action) -> None:
            nonlocal prepared_original
            assert action.approval_settings is not None
            if action.approval_settings.access.legacy_scope:
                if "edit" not in tools.scope.allowed_actions:
                    raise PermissionError("本次旧授权未允许改写或删除，未读取文件内容。")
                if "read" not in tools.scope.allowed_actions:
                    raise PermissionError("本次旧授权未允许读取，不能预读文件内容准备删除。")
                if not target.is_relative_to(tools.root):
                    if target not in tools.read_versions:
                        raise PermissionError("旧记录的任务文件夹外资料须先单独读取并获准；未为删除预读内容。")
                    current = tools.read_versions[target]
                else:
                    current = await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
            else:
                current = await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
            if target in tools.read_versions and current != tools.read_versions[target]:
                tools.read_versions[target] = current
                raise ModelRetry("原文已变化，未删除；请重新读取后再决定。")
            prepared_original = current
            action.before_sha256 = sha256(current).hexdigest()
            action.prepared_parameters["read_sha256"] = action.before_sha256
            action.impact += f" 预备原件的内容指纹（由内容算出的核对编号）：{action.before_sha256}；大小：{len(current)}字节。"
        async def remove(action: Action) -> str:
            assert action.approval_settings is not None
            metadata = await tools.file_executor.metadata(target, tools.root, action.approval_settings.access)
            if not metadata["isFile"]:
                raise ValueError("只能删除本次展示的单个文件，未删除文件夹。")
            original = await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
            if original != prepared_original:
                tools.read_versions[target] = original
                raise ModelRetry("原文已变化，未删除；请重新读取后再决定。")
            backup = tools.conversation.path.parent / f"{tools.conversation.path.stem}-evidence" / f"{action.id}-original.bin"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(original)
            action.backup_path = str(backup)
            action.before_sha256 = sha256(original).hexdigest()
            tools.conversation.save()
            try:
                result = await tools.file_executor.delete_existing(target, action.before_sha256, tools.root, action.approval_settings.access, approved=action.approval_basis in {"user", "assistant"})
            except FileConflict as error:
                raise ModelRetry(str(error)) from error
            action.sandbox_type = result["sandboxType"]
            return f"已删除：{target}；原件恢复位置：{backup}。"
        async with folder_write_lock(tools.write_root):
            return await tools.executor.run("delete", str(target), f"删除单个文件：{target}；保留原件，不删除旧聊天、输出或其他副本。", remove, requires_approval=tools.task.approval.access.legacy_scope or requires_write_approval(target, tools.task.approval.access, tools.owned_paths), tool_call_id=ctx.tool_call_id, replay_completed=not target.exists(), parameters={"path": str(target), "read_sha256": sha256(tools.read_versions[target]).hexdigest() if target in tools.read_versions else None}, prepare_operation=prepare_delete)
