import json
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_core import to_json

from myrk._conversation.records import ConversationRecord, Supplement, Turn, _linked_supplement
from myrk.compaction import part_hash, source_fingerprint
from myrk.context import ContextPreparationError


def compaction_status(turns: tuple[Turn, ...]) -> list[dict[str, Any]]:
    return [record.model_dump(mode="json", include={"id", "version", "status", "trigger", "task_id", "model", "layout_version", "replaced_id",
                "source", "retained", "covered", "summary", "before_tokens", "after_tokens", "input_budget_tokens",
                "measurement_source", "target_reached", "request_numbers", "error"})
            for turn in turns for record in turn.compactions]


def read_compaction_source(turns: tuple[Turn, ...], messages: list[ModelMessage], compaction_id: str, message_index: int, part_index: int, start: int = 0, length: int = 8000) -> str:
    record = next((record for turn in turns for record in turn.compactions if record.id == compaction_id), None)
    ref = next((ref for ref in record.source if (ref.message_index, ref.part_index) == (message_index, part_index)), None) if record else None
    if ref is None or start < 0 or length < 1 or length > 8000:
        raise ValueError("只能回查本会话摘要登记的原文部件；起点须非负，每次1至8000个字符。")
    try:
        part = messages[message_index].parts[part_index]
    except IndexError:
        raise ContextPreparationError("摘要来源位置缺失，请恢复可靠记录后继续。") from None
    if part_hash(part) != ref.sha256:
        raise ContextPreparationError("摘要来源原文指纹不符，已停止回查。")
    return to_json(part).decode()[start:start + length]


def _restore_legacy_supplement_selection(record: ConversationRecord, turns: tuple[Turn, ...]) -> None:
    legacy_owners: dict[str, str] = {}
    journal: dict[tuple[str, str], Supplement] = {}
    for turn in turns:
        if turn.task is not None:
            for item in turn.task.supplements:
                legacy_owners.setdefault(item.id, turn.task.id)
                journal[turn.task.id, item.id] = item
    pending = {key: item for key, item in journal.items()
               if not item.selected and item.applied_request is None and item.revoked_at is None}
    if not pending:
        return
    records = [record for turn in turns for record in turn.compactions]
    records.extend(layout.compaction for layout in record.request_layouts.values() if layout.compaction is not None)
    selected_sources = {(record.task_id, ref.message_index, ref.part_index, ref.sha256)
                        for record in records if source_fingerprint(record.source) == record.source_fingerprint
                        for ref in record.source}
    if not selected_sources:
        return
    for message_index, message in enumerate(record.messages):
        if not isinstance(message, ModelRequest):
            continue
        key = _linked_supplement(message, legacy_owners)
        if key is None or key not in pending:
            continue
        row = pending[key]
        if any(isinstance(part, UserPromptPart) and part.content == row.text and
               (key[0], message_index, part_index, part_hash(part)) in selected_sources
               for part_index, part in enumerate(message.parts)):
            row.selected = True


def _settle_interrupted_compactions(turns: tuple[Turn, ...], live_turns: list[Turn]) -> None:
    for turn in turns:
        if any(live is turn for live in live_turns):
            continue
        interrupted = {number for record in turn.compactions if record.status == "preparing" for number in record.request_numbers}
        for record in turn.compactions:
            if record.status == "preparing":
                record.status = "failed"
                record.error = "前次整理已中断，未采用候选摘要；原始记录与旧有效摘要保留，未自动重试。"
        for attempt in turn.request_attempts:
            if attempt.purpose == "compaction" and attempt.status == "sent" and attempt.number in interrupted:
                attempt.status = "failed"
                attempt.error = "前次摘要请求已中断，未完整收到服务回应。"
                attempt.decision = "已知用量保留，缺失用量保持未知；未自动重试或重置额度。"


def execution_facts(turns: tuple[Turn, ...], task_id: str) -> str:
    relevant = [turn for turn in turns if turn.task is not None and turn.task.id == task_id]
    if not relevant:
        return ""
    current = relevant[-1]
    task = current.task
    assert task is not None
    account = current.request_account or task
    actions = {action.id: action for turn in relevant for action in turn.actions}
    denied = [action for action in actions.values() if action.status == "denied"]
    sources = {source: sum(action.denial_source == source for action in denied)
               for source in ("user", "assistant", "scope", "review_incomplete", None)}
    work = {item.id: item for turn in relevant for item in turn.delegations}
    checks = [action for action in actions.values() if action.kind == "check" and action.delegation_id is None]
    unfinished_reviews = sum(action.review is not None and action.review.status in {"failed", "timeout", "context_changed"} for action in actions.values())
    quota = (f"旧记录父子共用真实请求：{task.requests}/{task.request_limit}次。" if task.request_policy == "shared" else
             f"本助理已办{account.turns_used}轮；轮数上限：{account.max_turns if account.max_turns is not None else '未设置'}；本助理真实请求：{account.requests}次。")
    action_records = list(actions.values())
    own_actions = sum(action.delegation_id == current.request_owner_id for action in action_records)
    kind_labels = {"list": "列举", "read": "普通读取", "search": "搜索", "verify": "产物全文核验",
                   "command": "运行程序", "check": "指定检查", "create": "新建", "write": "写入",
                   "edit": "修改", "delete": "删除", "calculate": "计算", "delegate": "委派",
                   "skill": "读取技能资料", "send": "外发", "network": "联网", "memory": "记忆"}
    status_labels = {"started": "已进入动作处理，不证明目标程序已运行", "awaiting_approval": "等待批准",
                     "completed": "已完成", "denied": "已拒绝", "failed": "失败", "conflict": "发生冲突", "cancelled": "已停止"}
    effect_labels = {"not_started": "所提议动作未执行，不排除准备读取", "completed": "已完成", "unknown": "未知"}
    action_lines = []
    for action in action_records[-8:]:
        actor = "主助理" if action.delegation_id is None else f"分工 {json.dumps(action.delegation_id[:64], ensure_ascii=False)}"
        if action.delegation_id is not None and len(action.delegation_id) > 64:
            actor += "（分工编号截短）"
        actor += "（本人）" if action.delegation_id == current.request_owner_id else "（其他助理）"
        target = json.dumps(action.target[:120], ensure_ascii=False) + ("（目标截短）" if len(action.target) > 120 else "")
        identity = json.dumps(action.id[:64], ensure_ascii=False) + ("（编号截短）" if len(action.id) > 64 else "")
        observation = "仅说明保存的动作记录，不能据此推断未记录的访问。"
        if action.kind in {"command", "check"}:
            observation = f"退出码：{action.exit_code if action.exit_code is not None else '未知'}｜内部逐文件影响未完整观测。"
        elif action.kind == "list" and action.status == "completed":
            observation = "本次列举文件名和元信息，未读取正文。"
        elif action.kind in {"read", "search", "verify", "skill"}:
            observation = ("已取得读取结果；不证明其他动作未读取。" if action.status == "completed" else
                           "读取范围未知；失败、停止或未执行提议不能断言未读取。")
            if action.kind == "verify" and action.status == "completed":
                observation = "已读回声明产物并比较全文，不验证所有过程自述。"
        elif action.kind == "delete" and action.before_sha256 is not None:
            observation = "为版本核对已读取原件；影响记录只针对所提议的删除。"
        kind = kind_labels.get(action.kind, json.dumps(action.kind[:32], ensure_ascii=False))
        action_lines.append(f"动作 {identity}｜{actor}｜{kind}：{target}｜"
            f"状态：{status_labels[action.status]}｜影响记录：{effect_labels[action.effect]}｜{observation}")
    return (
        "程序事实（直接取自保存的记录；不代表用户已验收）：\n"
        + quota + f" 整项累计真实请求：{task.total_requests}次；没有整项自动时间截止。\n"
        + f"拒绝或未执行记录：{len(denied)}；规则或范围拒绝：{sources['scope']}；本人明确拒绝：{sources['user']}；独立审核明确拒绝：{sources['assistant']}；拒绝来源未保存：{sources[None]}。\n"
        + f"审核未完成记录：{unfinished_reviews}；不能当成独立审核明确拒绝。\n"
        + f"分工记录：{len(work)}；专用核验尚未登记：{sum(item.verification_status == 'pending' for item in work.values())}；核验记录有缺口：{sum(item.verification_status == 'gaps' for item in work.values())}；已登记通过：{sum(item.verification_status == 'passed' for item in work.values())}。\n"
        + f"主助理指定检查记录：{len(checks)}；其中当时退出0：{sum(action.exit_code == 0 for action in checks)}；普通命令不能替代指定检查。\n"
        + f"动作摘要：本助理{own_actions}条，其他助理{len(action_records) - own_actions}条；按记录顺序列最后{len(action_lines)}条，省略{max(0, len(action_records) - len(action_lines))}条。完整编号和目标见原动作记录，不是完整文件访问审计。\n"
        + "\n".join(action_lines)
        + "\n汇报依据：哈希计算属于读取字节，不等于把正文交给模型或运行脚本。不得仅凭没有显式读取工具调用，就断言完全没读。"
        + "指定检查通过只证明该检查结果，不能证明所有过程自述属实；无法解释的错误输出保留未知，不编造环境原因。\n"
        + "用户明确禁止程序或命令时，使用允许的文件工具；无法完成的核对说明缺证，不为补齐报告自行启动命令。"
    )
