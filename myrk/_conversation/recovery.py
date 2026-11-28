from __future__ import annotations

import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart

from myrk._conversation.records import Task, Turn
from myrk.actions import approval_context
from myrk.checks import CheckReceipt, job_active
from myrk.executor import OfficialExecutor
from myrk.recovery import RecoveryCheck, RecoveryOwnership, RecoveryReport

if TYPE_CHECKING:
    from myrk.conversation import Conversation


def _current_recovery_turn(self: Conversation) -> Turn | None:
    latest_task = self._record.turns[-1].task if self._record.turns else None
    if latest_task is None:
        return None
    for turn in reversed(self._record.turns):
        if turn.task is None or turn.task.id != latest_task.id:
            break
        if not turn.blocked_attachment_resume:
            return turn if turn.status == "running" or turn.service_failure else None
    return None


def _earlier_recovery_turns(self: Conversation) -> Iterator[Turn]:
    current = self._current_recovery_turn()
    seen: set[str] = set()
    for turn in reversed(self._record.turns):
        if turn.task is None or turn.task.id in seen or turn.blocked_attachment_resume:
            continue
        if turn.status == "failed" and not turn.service_failure and not turn.requests and not turn.request_attempts:
            if (len(turn.actions) == 1 and turn.actions[0].task_id == turn.task.id
                    and turn.actions[0].kind == "history_reuse" and turn.actions[0].status == "denied"):
                continue
        seen.add(turn.task.id)
        if turn is not current and turn.status == "failed" and turn.service_failure and turn.request_owner_id is None:
            yield turn


def recovery_turn(self: Conversation, task_id: str) -> Turn | None:
    current = self._current_recovery_turn()
    if current is not None and current.task is not None and current.task.id == task_id:
        return current
    return next((turn for turn in self._earlier_recovery_turns() if turn.task is not None and turn.task.id == task_id), None)


def _recovery_report(path: Path, recovery_checks: list[RecoveryCheck], previous: Turn) -> RecoveryReport:
    assert previous.task is not None
    checked = {item.action_id: item for item in recovery_checks}
    ownership = RecoveryOwnership(path)
    actions = [(checked.get(action.id) if ownership.owns(action) else None) or RecoveryCheck(
        action_id=action.id, effect=action.effect,
        observation="这是最后可靠保存的动作状态；影响未知时必须核查真实文件和程序，不能重跑。",
    ) for action in previous.actions]
    return RecoveryReport(task_id=previous.task.id, required=True, actions=actions,
                          can_resume=all(item.effect != "unknown" for item in actions))


def authorize_denied_retry(self: Conversation, task_id: str, action_id: str) -> Task:
    previous = self.turns[-1] if self.turns else None
    if previous is None or previous.task is None or previous.task.id != task_id or previous.status in {"running", "cancelled"}:
        raise ValueError("只能选择当前已结束工作的确切拒绝；停止的旧允许不能恢复。")
    task = previous.task
    denials = [item for turn in self.turns if turn.task is not None and turn.task.id == task_id
               for item in turn.actions if item.review is not None and item.review.status == "denied"][-10:]
    action = next((item for item in denials if item.id == action_id), None)
    if action is None or action.review is None or action.review.override_used or task.denied_retry is not None:
        raise ValueError("该动作不在最近10项可再审拒绝中，或这次单次授权已使用。")
    if action.approval_context_sha256 != approval_context(task) or action.prepared_action_sha256 is None:
        raise ValueError("原动作授权背景已改变，不能把旧拒绝转为当前授权。")
    action.review.override_used = True
    task.denied_retry = {"action": action.prepared_action_sha256, "context": action.approval_context_sha256,
                         "action_id": action.id, "parameters": json.dumps(action.prepared_parameters, ensure_ascii=False), "kind": action.kind}
    self.save()
    return task


async def verify_recovery_async(self: Conversation, task_id: str) -> RecoveryReport:
    previous = self.recovery_turn(task_id)
    if previous is None:
        raise ValueError("请先选择确实意外中断的原工作，核查不会启动任何旧动作。")
    assert previous.task is not None
    ownership = RecoveryOwnership(self.path)
    file_executor = OfficialExecutor(self.path.parent / "executor", protected_paths=[self.path.parent])
    try:
        for action in previous.actions:
            effect = action.effect
            exit_code = action.exit_code
            observation = "最后可靠保存的动作状态已保留；本次没有执行原动作。"
            if effect == "unknown":
                observation = "证据不足，实际影响仍未知；请人工核查，本次没有重写、重发或重跑。"
                if not ownership.owns(action):
                    observation = "缺少可核对的本机动作归属凭据；记录自报路径不能证明文件或输出属于本次工作，未读取这些路径，实际影响保持未知。导入的原记录与部分回答保留，没有重跑。"
                elif action.kind in {"create", "write", "edit", "delete"} and previous.task.scope is not None and action.approval_settings is not None:
                    root = Path(previous.task.scope.root).resolve()
                    target = Path(action.target).resolve()
                    evidence_root = (self.path.parent / f"{self.path.stem}-evidence").resolve()
                    suffix = "txt" if action.kind == "edit" else "bin"
                    backup = evidence_root / f"{action.id}-original.{suffix}"
                    if action.kind == "create" or (action.backup_path == str(backup) and backup.resolve().parent == evidence_root):
                        try:
                            original_hash = sha256(backup.read_bytes()).hexdigest() if action.kind != "create" else None
                            entries = await file_executor.list_directory(target.parent, root, action.approval_settings.access)
                            present = any(entry["fileName"].casefold() == target.name.casefold() for entry in entries)
                            actual_hash = sha256(await file_executor.read_bytes(target, root, action.approval_settings.access)).hexdigest() if present else None
                        except OSError:
                            observation = "实际文件或原件证据无法读取，影响仍未知；请人工核查。"
                        else:
                            if action.kind == "create" and action.after_sha256 and actual_hash == action.after_sha256:
                                effect, observation = "completed", "已只读核对新建文件与原动作预期内容指纹（由内容算出的核对编号），新建影响已存在；不会重复创建。"
                            elif action.kind in {"write", "edit"} and original_hash == action.before_sha256 and actual_hash == action.after_sha256:
                                effect, observation = "completed", f"已只读核对真实文件{target}与保存的预期内容指纹（由内容算出的核对编号），改写影响已存在；不会重复改写。"
                            elif action.kind == "delete" and original_hash == action.before_sha256 and not present:
                                effect, observation = "completed", "已核对保留原件的内容指纹（由内容算出的核对编号）及真实文件已不存在，删除影响已存在；不会重复删除。"
                            elif action.kind != "create" and original_hash == action.before_sha256 and actual_hash == action.before_sha256:
                                effect, observation = "not_started", "真实文件与保存的改写前原件一致，预期改写影响未出现；旧动作不会重放，新动作须重新准备。"
                elif action.kind in {"check", "command", "send"} and action.job_name == f"Local\\Myrk-{action.id}" and action.receipt_path is not None:
                    evidence_root = (self.path.parent / f"{self.path.stem}-checks").resolve()
                    paths = [evidence_root / f"{action.id}{suffix}" for suffix in (".json", ".stdout", ".stderr")]
                    if [str(path) for path in paths] == [action.receipt_path, action.stdout_path, action.stderr_path] and all(path.resolve().parent == evidence_root for path in paths):
                        try:
                            active = job_active(action.job_name)
                            stdout = paths[1].read_text(encoding="utf-8", errors="replace") if paths[1].exists() else ""
                            stderr = paths[2].read_text(encoding="utf-8", errors="replace") if paths[2].exists() else ""
                            observation = f"只读核查本次程序组仍有{active}个程序；已有正常输出：{stdout}；已有错误输出：{stderr}。没有重跑；缺少可靠退出依据时仍未知。"
                            if active == 0 and paths[0].exists():
                                receipt = CheckReceipt.model_validate_json(paths[0].read_text(encoding="utf-8"))
                                if receipt.action_id == action.id:
                                    exit_code = receipt.exit_code
                                    actual_sandbox = "未采用本地隔离（电脑系统设置的访问限制）" if receipt.sandbox_type == "none" else "微软原生隔离（电脑系统实际限制程序访问）" if receipt.sandbox_type == "windowsMxc" else "旧凭据未记录"
                                    effect, observation = "completed", f"已核对本次程序组全部退出及独立退出凭据，实际限制方式：{actual_sandbox}；退出情况：{receipt.exit_code}；正常输出：{stdout}；错误输出：{stderr}。这不证明命令产生的所有外部效果，未重跑。"
                        except (OSError, ValueError):
                            observation = "本次程序或独立退出凭据不能可靠读取；影响仍未知，请人工核查，没有重跑。"
            self._record.recovery_checks.append(RecoveryCheck(action_id=action.id, effect=effect, observation=observation, exit_code=exit_code))
    finally:
        await file_executor.close()
    self.save()
    return self._recovery_report(previous)


def prepare_resume(self: Conversation, task_id: str, *, request_limit: int | None = None) -> Task:
    latest = self.turns[-1] if self.turns else None
    current = self._current_recovery_turn()
    if ((latest is not None and latest.request_owner_id is not None)
            or (current is not None and current.request_owner_id is not None)):
        raise ValueError("这是子助理执行记录，只供回查；请回到原主聊天续办，不能改用主助理额度。")
    previous = self.recovery_turn(task_id)
    report = self._recovery_report(previous) if previous is not None else None
    if previous is None or report is None or not report.can_resume:
        raise ValueError("不能续办：请先核查本项中断工作；仍有影响未知的动作时须由你人工判断，不能盲目重跑。")
    assert previous.task is not None
    account = self._latest_task(task_id)
    assert account is not None
    if request_limit is not None:
        account.validate_continuation_request_limit(request_limit)
    elif account.request_limit is not None and account.requests >= account.request_limit:
        raise ValueError("原工作已到请求次数上限；请明确增加额度后再继续，已用额度不会清零。")
    if previous is current:
        self._close_interrupted_tool_calls(previous, report)
    elif current is not None:
        raise ValueError("最近一项工作仍待核查；请先处理该项，再选择续办较早的工作。")
    old_limit = account.request_limit
    if request_limit is not None:
        account.request_limit = request_limit
    try:
        self.save()
    except (OSError, ValueError):
        account.request_limit = old_limit
        raise
    self._resume_authorized = task_id
    return previous.task.model_copy(deep=True, update={"request_limit": account.request_limit})


def _close_interrupted_tool_calls(self: Conversation, previous: Turn, report: RecoveryReport) -> None:
    outcomes = {item.action_id: item for item in report.actions}
    returned = {part.tool_call_id for message in self._record.messages for part in message.parts if isinstance(part, ToolReturnPart)}
    pending = [part for message in self._record.messages for part in message.parts if isinstance(part, ToolCallPart) and part.tool_call_id not in returned]
    actions = {action.tool_call_id: action for action in previous.actions if action.tool_call_id is not None}
    if pending:
        parts = []
        for call in pending:
            action = actions.get(call.tool_call_id)
            checked = outcomes.get(action.id) if action is not None else None
            content = (f"中断后核查记录：{checked.observation} 原动作编号：{action.id}。" if checked is not None and action is not None else "中断时没有可靠执行结果；本条仅关闭原请求，没有重新执行或宣称成功。")
            parts.append(ToolReturnPart(tool_name=call.tool_name, tool_call_id=call.tool_call_id, content=content,
                                        outcome="success" if checked is not None and checked.effect == "completed" else "failed"))
        self._record.messages.append(ModelRequest(parts=parts))
