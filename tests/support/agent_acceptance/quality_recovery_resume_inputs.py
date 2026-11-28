"""Recovery fixture facts, sources, and registered task projections."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from myrk.conversation import CheckCommand, TaskScope
from tests.support.agent_acceptance.core import Protocol
from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput, portable_task_path
from tests.support.agent_acceptance.replay import Reply


_CASE_REQUEST_CAP = 24
_SIDE_PATH = "side/source.txt"


class ResumeFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject: str
    target: str
    before: str
    after: str
    remaining: str


def facts(visible: QualityVisibleInput) -> ResumeFacts:
    if visible.adapter == "qg5-8" and "resume_facts" not in visible.parameters:
        raise ValueError("Delegated recovery requires frozen resume_facts.")
    if "resume_facts" in visible.parameters:
        item = ResumeFacts.model_validate(visible.parameters["resume_facts"])
        if visible.adapter == "qg5-8":
            if _SIDE_PATH not in visible.sources:
                raise ValueError(f"Delegated recovery requires its frozen {_SIDE_PATH} source.")
            if (not portable_task_path(item.target) or "/" in item.target
                    or item.target.casefold() in {"current-source.txt", "handoff.txt", "independent_check.py", "agents.md"}):
                raise ValueError("The delegated recovery target must be a distinct file in its own task folder.")
        return item
    source = "\n".join(item.text for item in visible.sources.values())
    if visible.adapter == "qg5-5":
        match = re.search(r"A台([0-9]+)盏.*?更正为([0-9]+)盏", source)
        if match:
            return ResumeFacts(subject="剧院灯光巡检", target="inspection.txt", before="A台" + match[1] + "盏\nB台交接说明：待核查\n",
                after="A台" + match[2] + "盏\nB台交接说明：待核查\n", remaining="只核查剩余B台交接说明；不重复A台更正或已完成的独立检查。")
    if visible.adapter == "qg5-6" and "档案清点" in source and "东库账页" in source:
        return ResumeFacts(subject="档案清点东库账页", target="east-ledger.txt", before="东库账页=未更正\n", after="东库账页=已改\n",
            remaining="先核查可靠保存、待提交原件和实际文件；没有本机归属凭据的导入副本不能重放未知编辑。")
    if visible.adapter == "qg5-9" and "展览印刷" in source and "7" in source:
        return ResumeFacts(subject="展览印刷任务", target="print-job.txt", before="展览印刷任务：当前原收件方固定；未获新的打印或发送授权。\n", after="展览印刷任务：当前原收件方固定；未获新的打印或发送授权。\n",
            remaining="保留真实用户停止状态和已用7次请求；打开/导入旧字段记录不自动续办，不扩大原收件方或访问范围。")
    raise ValueError("This recovery task lacks its actual source facts.")


def delegated_target_source(visible: QualityVisibleInput, item: ResumeFacts) -> QualitySource:
    source = visible.sources.get(item.target) or visible.sources.get("current-source.txt") or QualitySource(text=item.before)
    original = source.text.encode(source.encoding).decode("utf-8-sig")
    if re.split(r"\r\n|\r|\n", original) != re.split(r"\r\n|\r|\n", item.before):
        raise ValueError("The frozen target bytes contradict the declared original text.")
    return source


def supports(visible: QualityVisibleInput) -> bool:
    if visible.adapter not in {"qg5-5", "qg5-6", "qg5-8", "qg5-9"}: return False
    try:
        item = facts(visible)
        if visible.adapter == "qg5-8":
            delegated_target_source(visible, item)
        return True
    except ValueError:
        return False


def project_input(visible: QualityVisibleInput) -> QualityVisibleInput:
    if visible.adapter != "qg5-8" and not supports(visible):
        return visible
    item = facts(visible)
    parameters = {**visible.parameters, "resume_facts": item.model_dump(mode="json")}
    sources = {"current-source.txt": QualitySource(text=item.before)}
    if visible.adapter == "qg5-8":
        target_name = item.target if item.target in visible.sources else "current-source.txt"
        sources = {target_name: delegated_target_source(visible, item), _SIDE_PATH: visible.sources[_SIDE_PATH]}
    projected = visible.model_copy(update={"parameters": parameters, "sources": sources})
    return projected.model_copy(update={"goal": current_goal(projected)})


def current_goal(visible: QualityVisibleInput) -> str:
    item = facts(visible)
    if visible.adapter == "qg5-8":
        return f"{item.subject}：目前只读取{item.target}核对原文，等待用户明确补充。"
    if visible.adapter == "qg5-9":
        return f"{item.subject}：只读取{item.target}核对当前印刷事项，保留当前Task原接收方。未获新的外发或打印授权；实际用户停止后不继续。"
    return f"{item.subject}：读取{item.target}，按当前明确要求更正一次，原文\n{item.before}更正为\n{item.after}随后只执行本任务登记的检查。保留尚未完成的交接与原历史。"


def current_sources(visible: QualityVisibleInput) -> list[str]:
    return [facts(visible).target]


def allowed_paths(visible: QualityVisibleInput) -> list[str]:
    return [facts(visible).target, "handoff.txt", "independent_check.py", *([_SIDE_PATH] if visible.adapter == "qg5-8" else [])]


def model_prompts(visible: QualityVisibleInput) -> tuple[str, ...]:
    from tests.support.agent_acceptance.quality_runtime import model_prompt
    base = model_prompt(visible) + "\n\n" + current_goal(visible)
    return (base, "受委派工作：" + child_goal(visible)) if visible.adapter == "qg5-8" else (base,)


def required_outputs(visible: QualityVisibleInput) -> dict[str, int]:
    return {"main_answer": 1, **({"child_report": 1} if visible.adapter == "qg5-8" else {})}


def child_goal(visible: QualityVisibleInput) -> str:
    return f"{facts(visible).subject}：只读取子任务目录内的source.txt，按该原件报告尚未完成的事项。保留原接收方和只读范围。"


def supplement_goal(visible: QualityVisibleInput) -> str:
    item = facts(visible)
    return (f"我现在明确补充{item.subject}：只在{item.target}把以下原文更正一次。\n"
        f"原文：\n{item.before}\n新内容：\n{item.after}\n"
        f"随后委派子任务，只读{_SIDE_PATH}并报告未完成事项。保留原父子记录、接收方和各自用量。")


def output_standard(visible: QualityVisibleInput) -> dict[str, Any]:
    dimensions = {"source_facts": {"applicable": True, "description": "Use this source's distinct subject, values and remaining work."},
        "original_identity": {"applicable": True, "critical": True, "description": "Retain original Task, scope, approval, recipient, request counters and history through actual local reopening."},
        "effects_once": {"applicable": True, "critical": True, "description": "Never repeat a completed edit/check/child or revive an accepted stop; unknown effects require actual verification."},
        "failure_truth": {"applicable": True, "critical": True, "description": "Preserve reliable and pending originals and distinguish actual local save exit from supplier Mock failure; semantic/user acceptance stay pending."}}
    return {"main_answer": {"version": "quality-recovery-resume-v1", "dimensions": dimensions}}


def handoff_text(item: ResumeFacts) -> str:
    return f"{item.subject}：{item.remaining}用户接受仍待定。\n"


def setup(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> None:
    item = facts(visible)
    folder = root / "materials"
    target = folder / item.target
    if visible.adapter == "qg5-8":
        source = delegated_target_source(visible, item)
        expected = source.text.encode(source.encoding)
        if target.exists():
            if target.read_bytes() != expected:
                raise ValueError("The frozen target file differs from the authorized original bytes.")
        else:
            target.write_bytes(expected)
    else:
        target.write_text(item.before, encoding="utf-8")
    (folder / "handoff.txt").write_text(handoff_text(item), encoding="utf-8")
    body = "from pathlib import Path\np=Path(" + repr(item.target) + ")\nassert p.read_text(encoding='utf-8') == " + repr(item.after) + "\nprint('independent exact source change verified')\n"
    (folder / "independent_check.py").write_text(body, encoding="utf-8")


def manifest(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> dict[str, Any]:
    item = facts(visible)
    recipient: Literal["deepseek", "openai-compatible", "google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    folder = root / "materials"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit", "check"], provider=recipient, recipients=[recipient],
        checks=[CheckCommand(name="Independent recovery check", argv=[sys.executable, "-B", str(folder / "independent_check.py")])] if visible.adapter == "qg5-5" else [])
    return {"case_id": "quality." + visible.task_id.lower(), "root": str(root), "records": str(root / "records"), "materials": str(folder),
        "phases": ["work", "reopen"], "control_directories": [str(root / "initial"), str(root / "reopened")],
        "max_attempts": _CASE_REQUEST_CAP, "prompt": current_goal(visible), "scope": scope.model_dump(mode="json"), "approval_mode": "request_approval",
        "approval": None, "document": None, "receiver": None, "adoption": None, "quality_group": "QG5",
        "quality_recovery": {"workflow": "resume", "resume_kind": visible.adapter, "target": item.target, "remaining": item.remaining,
            "supplement": supplement_goal(visible) if visible.adapter == "qg5-8" else None,
            "child_goal": child_goal(visible) if visible.adapter == "qg5-8" else None}}


def replies(visible: QualityVisibleInput, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    item = facts(visible)
    if phase == "work":
        if visible.adapter == "qg5-8":
            request = manifest(visible, root, protocol)
            child_scope = {**request["scope"], "root": str(root / "materials/side"), "allowed_actions": ["read"], "checks": []}
            return [Reply(tool="read_text", arguments={"path": item.target}), Reply(tool="read_text", arguments={"path": item.target}),
                Reply(tool="edit_text", arguments={"path": item.target, "old_text": item.before, "new_text": item.after}),
                Reply(tool="delegate_work", arguments={"goal": child_goal(visible), "context": f"只回查{item.subject}的子任务原件。",
                    "evidence_requirements": "引用原件并保留实际子任务记录和未完成状态。", "expected_files": [_SIDE_PATH], "scope": child_scope}),
                Reply(tool="read_text", arguments={"path": "source.txt", "max_lines": 1000}),
                Reply(text=f"已只读子任务的source.txt。原文：\n{visible.sources[_SIDE_PATH].text.rstrip()}"),
                Reply(status=400)]
        if visible.adapter == "qg5-9":
            return [Reply(tool="read_text", arguments={"path": item.target}), Reply(text="The authorized source was read.")]
        if visible.adapter == "qg5-6":
            return [Reply(tool="read_text", arguments={"path": item.target}),
                Reply(tool="edit_text", arguments={"path": item.target, "old_text": item.before, "new_text": item.after})]
        return [Reply(tool="read_text", arguments={"path": item.target}),
            Reply(tool="edit_text", arguments={"path": item.target, "old_text": item.before, "new_text": item.after}),
            Reply(tool="run_check", arguments={"name": "Independent recovery check"}), Reply(status=400)]
    if visible.adapter == "qg5-6":
        return [Reply(tool="read_text", arguments={"path": item.target}), Reply(text=item.subject + "：本地保存替换前退出，原可靠与待提交记录分别保留；实际核查后沿原Task继续，没有重放已改文件。用户接受仍待定。")]
    if visible.adapter == "qg5-9":
        return []
    if visible.adapter == "qg5-8":
        return [Reply(tool="read_text", arguments={"path": "handoff.txt", "max_lines": 1000}),
            Reply(text=f"{item.subject}：{item.remaining} 本次只读取交接记录，保留原更正和子任务记录。用户接受仍待定。")]
    return [Reply(tool="read_text", arguments={"path": "handoff.txt"}),
        Reply(text=item.subject + "：重开阶段只核对交接记录，保留原更正和独立检查。用户接受仍待定。")]
