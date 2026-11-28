import base64
import csv
import io
import json
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import AnyHttpUrl
from pydantic_ai import Agent
from pydantic_ai.tools import RunContext

from myrk._conversation.records import Update
from myrk.actions import Action, folder_write_lock
from myrk.artifacts import DeliveredArtifact
from myrk.checks import execute_check
from myrk.executor import NetworkPolicyRequest, POWERSHELL
from myrk.extensions import ExtensionCatalog

if TYPE_CHECKING:
    from myrk._conversation.project_tools import ProjectToolContext


def register_delivery_tools(
    agent: Agent[None, str],
    extensions: ExtensionCatalog,
    tools: "ProjectToolContext",
    *,
    delegation_id: str | None,
) -> None:
    @agent.tool
    async def send_text(ctx: RunContext[None], path: str, url: AnyHttpUrl) -> str:
        """Send exact previously read text to this recipient under captured approval settings. Redirects are disabled."""
        target = (tools.root / path).resolve()
        payload = tools.read_versions.get(target)
        async def send(action: Action) -> str:
            if payload is None:
                raise ValueError("须先读取准备外发的具体资料，未发送。")
            assert action.approval_settings is not None
            parsed = url
            if parsed.username or parsed.password:
                raise ValueError("接收地址不能包含账号或密码，未发送。")
            async def approved_connection(params: dict[str, Any]) -> bool:
                request = NetworkPolicyRequest.model_validate(params).request
                tools.budget.checkpoint(applied_supplements=tools.seen_supplements)
                return request.host == parsed.host and request.port == parsed.port and request.protocol == ("http" if parsed.scheme == "http" else "https_connect")
            directory = tools.conversation.path.parent / f"{tools.conversation.path.stem}-checks"
            directory.mkdir(parents=True, exist_ok=True)
            staged = directory / f"{action.id}.payload"
            staged.write_bytes(payload)
            encoded_url = base64.b64encode(str(url).encode("utf-8")).decode("ascii")
            encoded_path = base64.b64encode(str(staged).encode("utf-8")).decode("ascii")
            script = f"""$ErrorActionPreference='Stop'
$handler=[Net.Http.HttpClientHandler]::new()
$handler.AllowAutoRedirect=$false
if ($env:HTTP_PROXY) {{ $handler.Proxy=[Net.WebProxy]::new($env:HTTP_PROXY); $handler.UseProxy=$true }} else {{ $handler.UseProxy=$false }}
$client=[Net.Http.HttpClient]::new($handler)
try {{
 $url=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_url}'))
 $path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
 $body=[Net.Http.ByteArrayContent]::new([IO.File]::ReadAllBytes($path))
 $body.Headers.ContentType=[Net.Http.Headers.MediaTypeHeaderValue]::new('text/plain')
 $body.Headers.ContentType.CharSet='utf-8'
 $response=$client.PostAsync($url,$body).GetAwaiter().GetResult()
 [Console]::WriteLine('HTTP（网络回应编号）：'+[int]$response.StatusCode)
 $response.EnsureSuccessStatusCode() | Out-Null
}} finally {{ $client.Dispose(); $handler.Dispose() }}
"""
            encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            try:
                await execute_check([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script], tools.root, 60, action, access=action.approval_settings.access,
                    protected_paths=tools.owned_paths, save=tools.conversation.save, evidence_directory=directory, network_decision=approved_connection)
            finally:
                staged.unlink(missing_ok=True)
            return "已向本次确切接收地址发送已读版本的文字；" + action.stdout
        return await tools.executor.run("send", str(url), f"新增资料接收方：{url}；发送文件：{target}；内容：{payload.decode('utf-8') if payload is not None else '尚未读取，不能发送'}。", send, requires_approval=tools.task.approval.condition != "never", tool_call_id=ctx.tool_call_id,
            parameters={"path": str(target), "url": str(url), "payload_sha256": sha256(payload).hexdigest() if payload is not None else None})

    @extensions.tool(metadata={'search_keywords': ['表格', '求和', '列合计']})
    async def calculate_table(ctx: RunContext[None], path: str, column: str) -> str:
        """Sum one numeric CSV/TSV column using exact decimal arithmetic and show source rows."""
        target = (tools.root / path).resolve()
        async def calculate(action: Action) -> str:
            if tools.task.approval.access.legacy_scope and not {"read", "calculate"}.issubset(tools.scope.allowed_actions):
                raise PermissionError("本次任务未允许读取和计算，未读取资料。")
            if target.suffix.lower() not in {".csv", ".tsv"}:
                raise ValueError("只计算.csv或.tsv（普通文字表格）。")
            assert action.approval_settings is not None
            raw = await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
            action.sandbox_type = tools.file_executor.last_sandbox_type
            tools.read_versions[target] = raw
            table = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter="\t" if target.suffix.lower() == ".tsv" else ",")
            if table.fieldnames is None or column not in table.fieldnames:
                raise ValueError("表格没有指定列，未进行估算。")
            total = Decimal(0)
            rows = []
            for row in table:
                value = row.get(column)
                if value is None:
                    raise ValueError(f"第{table.line_num}行缺少数值，未进行估算。")
                try:
                    amount = Decimal(value.strip())
                except InvalidOperation as error:
                    raise ValueError(f"第{table.line_num}行不是有效数值，未进行估算。") from error
                if not amount.is_finite():
                    raise ValueError("不接受无穷或未知数值，未进行估算。")
                total += amount
                rows.append(f"[{path}:{table.line_num}] {column}={value}")
            return f"按{column}列逐行相加，共{len(rows)}行，合计：{total}。\n" + "\n".join(rows)
        return await tools.executor.run("calculate", path, f"计算文件：{path}；实际位置：{target}；逐行合计{column}列，内容提供给已选理解服务。", calculate, requires_approval=tools.task.approval.access.legacy_scope and not target.is_relative_to(tools.root), tool_call_id=ctx.tool_call_id)

    @extensions.tool(metadata={'search_keywords': ['交付', '成果', '文件']})
    async def deliver_files(ctx: RunContext[None], change_ids: list[str]) -> str:
        """Explicitly deliver observed file changes by their evidence IDs, after verifying the real current bytes. Paths and answers are not evidence."""
        pending: list[DeliveredArtifact] = []
        async def deliver(action: Action) -> str:
            if not change_ids:
                raise ValueError("请提供真实文件改动证据编号。")
            assert action.approval_settings is not None
            existing = {item.file_change_id: item for previous in tools.conversation.turns for item in previous.artifacts}
            sources = {change.id: (source, change) for previous in tools.conversation.turns
                if previous.task is not None and previous.task.id == tools.task.id
                for source in previous.actions if source.task_id == tools.task.id
                for change in source.file_changes}
            selected = []
            for change_id in dict.fromkeys(change_ids):
                if change_id not in sources:
                    raise ValueError("没有本任务对应的真实文件改动证据，未登记成果。")
                source, change = sources[change_id]
                if source.status != 'completed' or source.effect != 'completed':
                    raise ValueError("来源动作尚未可靠完成，未登记成果。")
                target = Path(change.path)
                if any(target.is_relative_to(path.resolve()) for path in [*tools.owned_paths, *tools.file_executor.runtime_roots, tools.file_executor.directory]):
                    raise PermissionError("应用的记录、备份、日志和临时资料不能登记为交付成果。")
                record_path = str(tools.conversation.path)
                if source.delegation_id != delegation_id:
                    work = next((item for previous in tools.conversation.turns for item in previous.delegations
                        if item.id == source.delegation_id), None)
                    if delegation_id is not None or work is None or not target.is_relative_to(Path(work.scope.root).resolve()):
                        raise PermissionError("成果不属于本次分工范围，未登记。")
                    record_path = work.record_path
                metadata = await tools.file_executor.metadata(target, tools.root, action.approval_settings.access)
                if not metadata['isFile']:
                    raise ValueError("交付目标不是普通文件，未登记成果。")
                raw = await tools.file_executor.read_bytes(target, tools.root, action.approval_settings.access)
                if sha256(raw).hexdigest() != change.after_sha256:
                    raise ValueError("文件在来源动作后已变化，未把当前内容登记为该动作的成果。")
                artifact = existing.get(change_id)
                if artifact is None:
                    artifact = DeliveredArtifact(task_id=tools.task.id, source_action_id=source.id,
                        file_change_id=change.id, delegation_id=source.delegation_id,
                        source_record_path=record_path, name=target.name, path=str(target),
                        change_kind=change.change_kind, delivered_sha256=change.after_sha256,
                        size_bytes=len(raw))
                    pending.append(artifact)
                selected.append(artifact)
            tools.budget.checkpoint(applied_supplements=tools.seen_supplements)
            tools.turn.artifacts.extend(pending)
            return json.dumps({'status': 'delivered', 'artifacts': [item.model_dump() for item in selected]}, ensure_ascii=False)
        async with folder_write_lock(tools.write_root):
            try:
                result = await tools.executor.run('deliver', ','.join(change_ids), '明确交付真实文件改动：' + ', '.join(change_ids), deliver,
                    tool_call_id=ctx.tool_call_id, parameters={'change_ids': change_ids})
            except OSError:
                tools.turn.artifacts[:] = [item for item in tools.turn.artifacts if item not in pending]
                raise
            if pending and all(item in tools.turn.artifacts for item in pending) and tools.on_update:
                tools.on_update(Update('artifacts', artifacts=[item.model_copy(deep=True) for item in tools.turn.artifacts]))
            return result
