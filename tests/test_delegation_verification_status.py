import asyncio
from collections.abc import AsyncIterator
import json
from pathlib import Path
import sys

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from myrk.conversation import Conversation, TaskScope


def test_completed_ordinary_checks_keep_pending_registration_and_an_accurate_notice(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    target = folder / "notes.txt"
    target.write_bytes("苹果 5\n".encode("utf-8"))
    check = folder / "check_notes.py"
    check.write_text(
        "from pathlib import Path\n"
        "assert Path('notes.txt').read_bytes() == '苹果 5\\n'.encode('utf-8')\n"
        "print('independent artifact check passed')\n",
        encoding="utf-8",
    )
    read_compared = False
    check_compared = False

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal read_compared, check_compared
        prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if prompts[0].startswith("受委派工作："):
            if not results:
                yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="child-read")}
            else:
                yield "notes.txt 中苹果为5，请主助理核对真实文件。"
            return
        if len(results) >= 2:
            read_compared = str(results[1].content) == "[notes.txt:1] 苹果 5"
        if len(results) >= 3:
            check_compared = "independent artifact check passed" in str(results[2].content)
            yield "主助理已比较真实原文并运行独立检查；成果待用户验收。"
            return
        steps = [
            ("delegate_work", {"goal": "读取苹果数量", "context": "只读notes.txt", "evidence_requirements": "真实原文，主助理自行核对", "expected_files": ["notes.txt"]}),
            ("read_text", {"path": "notes.txt"}),
            ("run_check", {"name": "核对真实文件全文"}),
        ]
        name, arguments = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"main-{len(results)}")}

    scope = TaskScope(
        root=str(folder), allowed_actions=["read", "check"], provider="deepseek", recipients=["deepseek"],
        checks=[{"name": "核对真实文件全文", "argv": [sys.executable, str(check)]}],
    )
    conversation = Conversation(tmp_path / "records" / "verification.json")
    turn = asyncio.run(conversation.ask("委派读取后比较全文并运行指定检查", document=None, scope=scope, model=FunctionModel(stream_function=respond)))
    assert turn.status == "completed", turn.error
    assert read_compared and check_compared and turn.checks_passed
    assert all(action.status == "completed" for action in turn.actions)
    assert turn.delegations[0].verification_status == "pending"
    assert turn.delegations[0].verified_files == []
    assert "专用核验尚未登记" in turn.answer
    assert "未核对或未通过" not in turn.answer
    assert "专用核验尚未登记：1；核验记录有缺口：0；已登记通过：0" in turn.execution_facts
    assert "主助理指定检查记录：1；其中当时退出0：1" in turn.execution_facts
    reopened = Conversation.open(conversation.path).turns[-1]
    assert reopened.answer == turn.answer
    assert reopened.delegations[0] == turn.delegations[0]
    assert reopened.checks_passed is True


@pytest.mark.parametrize("outcomes,expected_states,expected_facts,pending_notice,gaps_notice", [
    (
        [("pending.txt", None)], ["pending"],
        "分工记录：1；专用核验尚未登记：1；核验记录有缺口：0；已登记通过：0", True, False,
    ),
    (
        [("passed.txt", "独立预期原文\n")], ["passed"],
        "分工记录：1；专用核验尚未登记：0；核验记录有缺口：0；已登记通过：1", False, False,
    ),
    (
        [("gaps.txt", "要求的另一份原文\n")], ["gaps"],
        "分工记录：1；专用核验尚未登记：0；核验记录有缺口：1；已登记通过：0", False, True,
    ),
    (
        [("pending.txt", None), ("gaps.txt", "要求的另一份原文\n"), ("passed.txt", "独立预期原文\n")],
        ["pending", "gaps", "passed"],
        "分工记录：3；专用核验尚未登记：1；核验记录有缺口：1；已登记通过：1", True, True,
    ),
], ids=["no-main-verification", "registered-pass", "registered-gap", "mixed-registration"])
def test_verification_notices_distinguish_registration_from_recorded_gaps(
    tmp_path: Path, outcomes: list[tuple[str, str | None]], expected_states: list[str],
    expected_facts: str, pending_notice: bool, gaps_notice: bool,
) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    for name, _expected in outcomes:
        (folder / name).write_bytes("独立预期原文\n".encode("utf-8"))
    steps = []
    for name, expected in outcomes:
        steps.append(("delegate_work", {
            "goal": f"交回{name}", "context": f"仅处理{name}", "evidence_requirements": "交回声明文件，仍须主助理核验。",
            "expected_files": [name],
        }))
        if expected is not None:
            steps.append(("verify_delegation", {"path": name, "expected_text": expected}))

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        if prompts[0].startswith("受委派工作："):
            yield "所列真实文件已交回；报告仍须主助理独立核验。"
            return
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == len(steps):
            yield "本轮处理结束，请查看各项分工记录并验收。"
            return
        name, arguments = steps[len(results)]
        if name == "verify_delegation":
            work = json.loads(str(results[-1].content))
            arguments = {**arguments, "delegation_id": work["id"]}
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"registration-{len(results)}")}

    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    conversation = Conversation(tmp_path / "records" / "registration.json")
    turn = asyncio.run(conversation.ask("逐项交回文件并登记明确核验结果", document=None, scope=scope, model=FunctionModel(stream_function=respond)))
    assert turn.status == "completed", turn.error
    assert [work.status for work in turn.delegations] == ["completed"] * len(outcomes)
    assert [work.verification_status for work in turn.delegations] == expected_states
    assert expected_facts in turn.execution_facts
    assert ("专用核验尚未登记" in turn.answer) is pending_notice
    assert ("核验记录有缺口" in turn.answer) is gaps_notice
    assert "未核对或未通过" not in turn.answer
    if gaps_notice:
        assert any(action.kind == "verify" and action.status == "failed" for action in turn.actions)
    if expected_states == ["pending"]:
        assert [action.kind for action in turn.actions] == ["delegate"]
        assert turn.checks_passed is None
        assert "已有读取和检查证据" not in turn.answer
    for name, _expected in outcomes:
        assert (folder / name).read_bytes() == "独立预期原文\n".encode("utf-8")
    reopened = Conversation.open(conversation.path).turns[-1]
    assert reopened.delegations == turn.delegations
    assert reopened.answer == turn.answer
    assert reopened.execution_facts == turn.execution_facts
