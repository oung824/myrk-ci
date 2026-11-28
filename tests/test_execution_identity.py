"""Execution facts identify the actual command/check and the source of refusal."""

import asyncio
import json
from pathlib import Path
import sys

import pytest
from pydantic_ai.models.function import FunctionModel

from myrk.actions import Action, ActionExecutor, approval_context
from myrk.checks import CheckReceipt, execute_check, prepare_check_runtime
from myrk.conversation import Task
from myrk.permissions import AccessSettings, ApprovalSettings
from myrk.review import review_action
from myrk.task_control import TaskBudget


@pytest.mark.parametrize("kind", ["command", "send", "check"])
def test_execution_reports_its_actual_kind_and_preserves_receipts_and_streams(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_check_runtime()
    application_variables = (
        "MYRK_PROCESS_OWNER_FILE",
        "MYRK_PROCESS_RECEIPT_DIRECTORY",
        "MYRK_TEST_NORMAL_EXIT_FLAG",
        "MYRK_EXIT_CASE_DIRECTORY",
        "MYRK_CHECK_ENVIRONMENT_TEST_SENTINEL",
    )
    for name in application_variables:
        monkeypatch.setenv(name, "synthetic-application-value")
    script = """
import json, os, sys
from pathlib import Path

environment = {name: os.environ[name] for name in sys.argv[1:] if name in os.environ}
Path('environment.json').write_text(json.dumps(environment), encoding='utf-8')
print('actual-output')
print('actual-error', file=sys.stderr)
"""
    actions: list[Action] = []
    executor = ActionExecutor(actions=actions, budget=TaskBudget(Task(goal="运行普通命令", scope=None, request_limit=10)), save=lambda: None, notify=lambda action: None)

    async def command(action: Action) -> str:
        argv = [sys.executable, "-c", script, "PYTHONIOENCODING", "PYTHONPATH", *application_variables]
        return await execute_check(argv, tmp_path, 5, action,
                                   access=AccessSettings(writable_roots=[str(tmp_path)]), save=lambda: None,
                                   evidence_directory=tmp_path / "records" / "checks", protected_paths=[])

    result = asyncio.run(executor.run(kind, "金额核对", "运行普通命令", command))
    action = actions[0]
    assert ("指定检查「金额核对」通过" if kind == "check" else "命令完成") in result
    assert kind == "check" or "指定检查" not in result
    assert "actual-output" in result and "actual-error" in result
    assert action.status == "completed" and action.exit_code == 0 and action.cleanup_complete
    assert action.stdout == "actual-output\n" and action.stderr == "actual-error\n"
    assert action.receipt_path and action.stdout_path and action.stderr_path
    receipt = CheckReceipt.model_validate_json(Path(action.receipt_path).read_text(encoding="utf-8"))
    assert receipt.action_id == action.id and receipt.exit_code == 0 and receipt.sandbox_type == "windowsMxc"
    assert Path(action.stdout_path).read_text(encoding="utf-8") == "actual-output\n"
    assert Path(action.stderr_path).read_text(encoding="utf-8") == "actual-error\n"
    environment = json.loads((tmp_path / "environment.json").read_text(encoding="utf-8"))
    assert environment == {"PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}


@pytest.mark.parametrize("kind", ["command", "send", "check"])
@pytest.mark.parametrize("timeout", [False, True])
def test_failed_execution_names_its_actual_kind_and_preserves_outputs(tmp_path: Path, kind: str, timeout: bool) -> None:
    prepare_check_runtime()
    actions: list[Action] = []
    executor = ActionExecutor(actions=actions, budget=TaskBudget(Task(goal="保留程序实际结果", scope=None, request_limit=10)), save=lambda: None, notify=lambda action: None)
    script = "import sys,time;print('failure-output',flush=True);print('failure-error',file=sys.stderr,flush=True);" + ("time.sleep(30)" if timeout else "sys.exit(7)")

    async def operation(action: Action) -> str:
        return await execute_check([sys.executable, "-c", script], tmp_path, .4 if timeout else 5, action,
                                   access=AccessSettings(writable_roots=[str(tmp_path)]), save=lambda: None,
                                   evidence_directory=tmp_path / "records" / "checks", protected_paths=[])

    result = asyncio.run(executor.run(kind, "金额核对", "保留真实失败", operation))
    action = actions[0]
    assert ("指定检查「金额核对」" if kind == "check" else "命令") in result
    assert kind == "check" or "指定检查" not in result
    assert "failure-output" in result and "failure-error" in result
    assert action.status == "failed" and action.timed_out is timeout and action.cleanup_complete
    assert action.exit_code is not None and (timeout or action.exit_code == 7)


@pytest.mark.parametrize("kind", ["command", "send", "check"])
def test_stopped_execution_keeps_its_identity_output_and_cleanup(tmp_path: Path, kind: str) -> None:
    prepare_check_runtime()
    actions: list[Action] = []
    executor = ActionExecutor(actions=actions, budget=TaskBudget(Task(goal="停止正在运行的程序", scope=None, request_limit=10)), save=lambda: None, notify=lambda action: None)

    async def operation(action: Action) -> str:
        return await execute_check([sys.executable, "-c", "import sys,time;print('before-stop-output',flush=True);print('before-stop-error',file=sys.stderr,flush=True);time.sleep(30)"], tmp_path, 10, action,
                                   access=AccessSettings(writable_roots=[str(tmp_path)]), save=lambda: None,
                                   evidence_directory=tmp_path / "records" / "checks", protected_paths=[])

    async def run() -> None:
        running = asyncio.create_task(executor.run(kind, "持续核对", "保留停止事实", operation))
        try:
            async with asyncio.timeout(15):
                while not actions or not actions[0].stderr_path or not Path(actions[0].stderr_path).exists():
                    await asyncio.sleep(.01)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(run())
    action = actions[0]
    assert action.error and ("指定检查「持续核对」已停止" if kind == "check" else "命令已停止") in action.error
    assert action.status == "cancelled" and not action.timed_out and action.cleanup_complete
    assert "before-stop-output" in action.stdout and "before-stop-error" in action.stderr


def test_user_refusal_is_recorded_and_the_proposed_action_never_runs(tmp_path: Path) -> None:
    actions: list[Action] = []

    async def decline(action: Action) -> bool:
        return False

    async def operation(action: Action) -> str:
        (tmp_path / "must-not-exist.txt").write_text("not permitted", encoding="utf-8")
        return "unexpected execution"

    executor = ActionExecutor(actions=actions, budget=TaskBudget(Task(goal="由本人决定", scope=None, request_limit=10)), save=lambda: None, notify=lambda action: None, approve=decline)
    asyncio.run(executor.run("create", "must-not-exist.txt", "本次具体提议", operation, requires_approval=True))
    action = actions[0]
    assert action.denial_source == "user" and action.status == "denied" and action.effect == "not_started"
    assert not (tmp_path / "must-not-exist.txt").exists()


@pytest.mark.parametrize("automatic", [False, True])
def test_missing_decision_channel_cannot_be_reported_as_a_human_refusal(tmp_path: Path, automatic: bool) -> None:
    actions: list[Action] = []
    task = Task(goal="只在取得实际决定后执行", scope=None, request_limit=10,
                approval=ApprovalSettings(mode="auto_review" if automatic else "request_approval"))

    async def operation(action: Action) -> str:
        (tmp_path / "must-not-exist.txt").write_text("not permitted", encoding="utf-8")
        return "unexpected execution"

    executor = ActionExecutor(actions=actions, budget=TaskBudget(task), save=lambda: None, notify=lambda action: None)
    result = asyncio.run(executor.run("create", "must-not-exist.txt", "缺少决定通道", operation, requires_approval=True))
    action = actions[0]
    assert action.denial_source == ("review_incomplete" if automatic else None)
    assert "用户未批准" not in result
    assert action.status == "denied" and action.effect == "not_started" and not (tmp_path / "must-not-exist.txt").exists()


def test_scope_validation_refusal_is_distinct_from_a_human_decision(tmp_path: Path) -> None:
    actions: list[Action] = []

    async def validate(action: Action) -> None:
        raise PermissionError("具体路径不在本次允许范围内。")

    async def operation(action: Action) -> str:
        (tmp_path / "must-not-exist.txt").write_text("not permitted", encoding="utf-8")
        return "unexpected execution"

    executor = ActionExecutor(actions=actions, budget=TaskBudget(Task(goal="保留范围限制依据", scope=None, request_limit=10)), save=lambda: None, notify=lambda action: None)
    asyncio.run(executor.run("create", "must-not-exist.txt", "验证具体位置", operation, prepare_operation=validate))
    action = actions[0]
    assert action.denial_source == "scope" and action.status == "denied" and action.effect == "not_started"
    assert not (tmp_path / "must-not-exist.txt").exists()


def test_rejecting_a_persisted_proposal_records_the_user_as_its_source() -> None:
    task = Task(goal="本人决定已有提议", scope=None, request_limit=10)
    action = Action(task_id=task.id, kind="memory", target="existing-proposal", status="awaiting_approval")
    executor = ActionExecutor(actions=[action], budget=TaskBudget(task), save=lambda: None, notify=lambda action: None)
    executor.reject(action)
    assert action.denial_source == "user" and action.status == "denied" and action.effect == "not_started"


@pytest.mark.parametrize("old_error", ["用户拒绝本次具体提议", "独立审核拒绝", "检查范围不符"])
def test_old_refusals_keep_the_source_unknown_instead_of_guessing_from_text(old_error: str) -> None:
    action = Action.model_validate({"task_id": "old-task", "kind": "command", "target": "old-target", "status": "denied", "error": old_error})
    assert action.denial_source is None
    assert Action.model_validate_json(action.model_dump_json()).denial_source is None


@pytest.mark.parametrize("outcome", ["denied", "failed", "timeout"])
def test_independent_review_records_denial_or_incompletion_without_executing(tmp_path: Path, outcome: str) -> None:
    actions: list[Action] = []
    task = Task(goal="先独立审核再决定", scope=None, request_limit=10, approval=ApprovalSettings(mode="auto_review"))
    budget = TaskBudget(task)

    async def response(messages, info):
        if outcome == "timeout":
            await asyncio.sleep(10)
        elif outcome == "failed":
            raise OSError("controlled-review-service-failure")
        yield json.dumps({"decision": "deny", "risk": "high", "authorization": "low", "rule": "general",
                          "reason": "本次确切动作的具体风险尚未取得用户授权。", "same_denied_outcome": False})

    async def review(action: Action) -> bool:
        return await review_action(action, model=FunctionModel(stream_function=response), budget=budget, directory=tmp_path / "review",
                                   messages=[], denials=[], save=lambda: None, context=lambda: approval_context(task),
                                   timeout_seconds=.1 if outcome == "timeout" else 5)

    async def operation(action: Action) -> str:
        (tmp_path / "must-not-exist.txt").write_text("not permitted", encoding="utf-8")
        return "unexpected execution"

    executor = ActionExecutor(actions=actions, budget=budget, save=lambda: None, notify=lambda action: None, review=review)
    asyncio.run(executor.run("create", "must-not-exist.txt", "审核实际动作", operation, requires_approval=True))
    action = actions[0]
    assert action.review and action.review.status == outcome
    assert action.denial_source == ("assistant" if outcome == "denied" else "review_incomplete")
    assert action.status == ("denied" if outcome == "denied" else "failed") and action.effect == "not_started"
    assert not (tmp_path / "must-not-exist.txt").exists()
