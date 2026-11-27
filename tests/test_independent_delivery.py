import asyncio
from collections.abc import AsyncIterator
from hashlib import sha256
import json
from pathlib import Path
import sys

from pydantic_ai.messages import ModelMessage, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from myrk.checks import CheckReceipt
from myrk.conversation import CheckCommand, Conversation, Task, TaskScope


def test_two_independent_children_deliver_real_files_for_parent_verification_and_final_check(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    invoice = 'from decimal import Decimal\n\ndef total(amounts):\n    return sum((Decimal(str(amount)) for amount in amounts[:-1]), Decimal("0"))\n'
    corrected_invoice = invoice.replace("amounts[:-1]", "amounts")
    source_csv = "item,amount\nnotebook,0.10\npencil,0.20\nfolder,0.45\n"
    original_report = "来源：待补\n笔数：待补\n合计：待补\n"
    expected_report = "来源：amounts.csv\n笔数：3\n合计：0.75\n"
    checker = '''import csv, hashlib, json, runpy
from decimal import Decimal
from pathlib import Path
total = runpy.run_path("invoice.py")["total"]
cases = [([], "0"), (["0.10"], "0.10"), (["0.10", "0.20", "0.45"], "0.75"), (["-1.10", "0.20"], "-0.90"), (["0.00000001", "0.20"], "0.20000001")]
assert all(total(values) == Decimal(expected) for values, expected in cases)
rows = list(csv.DictReader(Path("amounts.csv").read_text(encoding="utf-8").splitlines()))
assert len(rows) == 3 and sum(Decimal(row["amount"]) for row in rows) == Decimal("0.75")
assert Path("report.txt").read_text(encoding="utf-8") == "来源：amounts.csv\\n笔数：3\\n合计：0.75\\n"
print(json.dumps({"total": "0.75", "cases": 5, "hashes": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in ["invoice.py", "report.txt"]}}), flush=True)
'''
    originals = {"invoice.py": invoice, "amounts.csv": source_csv, "report.txt": original_report, "check_delivery.py": checker}
    for name, content in originals.items():
        (folder / name).write_text(content, encoding="utf-8", newline="\n")
    counts = {"main": 0, "invoice": 0, "report": 0}

    def tool(name: str, arguments: dict[str, object], identity: str) -> DeltaToolCall:
        return DeltaToolCall(name=name, json_args=json.dumps(arguments, ensure_ascii=False), tool_call_id=identity)

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        role = ("invoice" if "职责=invoice" in prompts[0] else "report") if prompts[0].startswith("受委派工作：") else "main"
        counts[role] += 1
        step = counts[role]
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if role == "invoice":
            if step == 1:
                yield {0: tool("read_text", {"path": "invoice.py"}, "invoice-read")}
            elif step == 2:
                yield {0: tool("edit_text", {"path": "invoice.py", "old_text": "amounts[:-1]", "new_text": "amounts"}, "invoice-edit")}
            else:
                yield "金额程序已修改，供主助理核验。"
        elif role == "report":
            if step == 1:
                yield {0: tool("read_text", {"path": "amounts.csv"}, "amounts-read"), 1: tool("read_text", {"path": "report.txt"}, "report-read")}
            elif step == 2:
                yield {0: tool("edit_text", {"path": "report.txt", "old_text": original_report, "new_text": expected_report}, "report-edit")}
            else:
                yield "结果文件已修改，供主助理核验。"
        elif step == 1:
            yield {0: tool("update_plan", {"steps": ["分派金额程序", "主助理核验", "分派结果文件", "主助理核验并检查"]}, "plan"),
                   1: tool("delegate_work", {"goal": "修正金额程序", "context": "职责=invoice；只改invoice.py。", "evidence_requirements": "保留原件，交回真实文件。", "expected_files": ["invoice.py"]}, "delegate-invoice")}
        elif step in {2, 3}:
            work = json.loads(str(next(part.content for part in reversed(returns) if part.tool_name == "delegate_work")))
            if step == 2:
                yield {0: tool("verify_delegation", {"delegation_id": work["id"], "path": "invoice.py", "expected_text": corrected_invoice}, "verify-invoice"),
                       1: tool("delegate_work", {"goal": "独立填写结果文件", "context": "职责=report；读amounts.csv，只改report.txt。", "evidence_requirements": "交回真实结果文件。", "expected_files": ["report.txt"]}, "delegate-report")}
            else:
                yield {0: tool("verify_delegation", {"delegation_id": work["id"], "path": "report.txt", "expected_text": expected_report}, "verify-report"),
                       1: tool("run_check", {"name": "交付检查"}, "final-check")}
        else:
            yield "两份成果交付，等待用户验收。"

    argv = [sys.executable, "-B", str(folder / "check_delivery.py")]
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"],
                      checks=[CheckCommand(name="交付检查", argv=argv)])
    task = Task(goal="依次请两位子助理修改金额程序和结果文件，主助理分别核验后运行交付检查。", scope=scope,
                request_limit=None, max_turns=None, delegation_max_turns=250)
    conversation = Conversation(tmp_path / "records" / "session.json")
    turn = asyncio.run(conversation.ask(task.goal, document=None, scope=scope, task=task, model=FunctionModel(stream_function=respond)))

    assert turn.status == "completed", turn.error
    assert turn.plan and turn.checks_passed is True and turn.delivery_status == "awaiting_acceptance"
    assert counts == {"main": 4, "invoice": 3, "report": 3}
    assert task.request_limit is None and task.max_turns is None
    assert task.requests == task.turns_used == 4 and task.total_requests == 10
    assert len(turn.delegations) == len({work.id for work in turn.delegations}) == 2
    assert {tuple(work.expected_files) for work in turn.delegations} == {("invoice.py",), ("report.txt",)}
    assert (folder / "invoice.py").read_text(encoding="utf-8") == corrected_invoice
    assert (folder / "report.txt").read_text(encoding="utf-8") == expected_report
    for name in ("amounts.csv", "check_delivery.py"):
        assert (folder / name).read_bytes() == originals[name].encode("utf-8")

    attempts = list(turn.request_attempts)
    assert [attempt.own_number for attempt in attempts] == [1, 2, 3, 4]
    assert all(attempt.actor == "main" and attempt.delegation_id is None for attempt in attempts)
    for work in turn.delegations:
        assert work.status == "completed" and work.verification_status == "passed"
        assert work.verified_files == work.expected_files
        assert work.request_limit is None and work.max_turns == 250
        assert work.requests == work.turns_used == 3
        child = Conversation.open(Path(work.record_path)).turns[-1]
        assert child.task.id == task.id and child.request_owner_id == work.id
        assert child.request_account.requests == child.request_account.turns_used == 3
        assert [attempt.own_number for attempt in child.request_attempts] == [1, 2, 3]
        assert all(attempt.actor == "delegation" and attempt.delegation_id == work.id for attempt in child.request_attempts)
        attempts.extend(child.request_attempts)
    assert sorted(attempt.number for attempt in attempts) == list(range(1, 11))
    assert all(attempt.status == "completed" for attempt in attempts)

    verify = [action for action in turn.actions if action.kind == "verify"]
    assert len(verify) == 2 and all(action.status == "completed" and action.delegation_id is None for action in verify)
    checks = [action for action in turn.actions if action.kind == "check"]
    assert len(checks) == 1
    check = checks[0]
    assert check.delegation_id is None and check.status == "completed" and check.exit_code == 0
    assert check.cleanup_complete is True and check.sandbox_type == "windowsMxc" and not check.timed_out
    assert check.prepared_parameters["argv"] == argv and check.cwd == str(folder)
    assert all(action.finished_at <= check.started_at for action in turn.actions if action.kind in {"edit", "verify"} and action.finished_at)
    receipt = CheckReceipt.model_validate_json(Path(check.receipt_path).read_text(encoding="utf-8"))
    assert receipt.action_id == check.id and receipt.exit_code == 0 and receipt.sandbox_type == "windowsMxc"
    payload = json.loads(check.stdout)
    assert payload["total"] == "0.75" and payload["cases"] == 5
    assert payload["hashes"] == {name: sha256((folder / name).read_bytes()).hexdigest() for name in ("invoice.py", "report.txt")}
    saved = Conversation.open(conversation.path).turns[-1]
    assert saved.task.requests == saved.task.turns_used == 4 and saved.task.total_requests == 10
    assert [(work.requests, work.turns_used) for work in saved.delegations] == [(3, 3), (3, 3)]
    messages = json.loads(conversation.path.read_text(encoding="utf-8"))["messages"]
    assert [part["part_kind"] for part in messages[-1]["parts"]] == ["text"]
