import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.function import DeltaToolCall

from myrk.conversation import Conversation, TaskScope


def test_old_task_deadline_does_not_stop_service_but_explicit_user_stop_does(tmp_path: Path, monkeypatch) -> None:
    from myrk.conversation import Task
    from myrk.task_control import TaskBudget
    now = 100.0
    monkeypatch.setattr("myrk.task_control.monotonic", lambda: now)
    task = Task(goal="超过旧时间字段后继续等待本人停止", scope=None, active_seconds_limit=0.08)
    budget = TaskBudget(task)
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        nonlocal now
        with budget.waiting():
            now += 10
            await asyncio.sleep(0)
        assert task.active_seconds == 0
        yield "用户等待已结束"
        now += .2
        budget.account()
        assert task.active_seconds == pytest.approx(.2)
        assert not budget.token.cancelled
        yield "超过旧时间仍在继续"
        await asyncio.Event().wait()
        yield "停止后不应到达"
    def progress(update):
        if update.kind == "text" and "超过旧时间仍在继续" in update.text:
            budget.token.cancel()
    path = tmp_path / "time.json"
    async def run():
        return await asyncio.wait_for(Conversation(path).ask(task.goal, document=None,
            model=FunctionModel(stream_function=stream), task=task, budget=budget, on_update=progress), timeout=10)
    turn = asyncio.run(run())
    assert turn.status == "cancelled" and turn.error is None
    assert turn.answer == "用户等待已结束超过旧时间仍在继续"
    assert "停止后不应到达" not in turn.answer
    assert task.active_seconds == pytest.approx(.2)
    reopened = Conversation.open(path).turns[-1]
    assert reopened.status == "cancelled" and reopened.answer == turn.answer
    assert reopened.task.requests == reopened.task.total_requests == 1


def test_a_not_started_second_action_waits_for_a_new_supplement_to_be_adopted(tmp_path: Path) -> None:
    from myrk.conversation import Supplement, Task
    from myrk.task_control import TaskBudget
    for name in ["first.txt", "obsolete.txt", "revised.txt"]:
        (tmp_path / name).write_text(name, encoding="utf-8")
    scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="读取两份", scope=scope, request_limit=5)
    budget = TaskBudget(task)
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"first.txt"}', tool_call_id="first"),
                   1: DeltaToolCall(name="read_text", json_args='{"path":"obsolete.txt"}', tool_call_id="obsolete")}
        elif not any("revised.txt:1" in str(part.content) for part in results):
            assert "改为读取revised.txt" in str(messages)
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"revised.txt"}', tool_call_id="revised")}
        else:
            yield "按补充完成"
    def update(event):
        if event.kind == "action" and event.action.status == "started" and not task.supplements:
            task.supplements.append(Supplement(text="改为读取revised.txt"))
    turn = asyncio.run(Conversation(tmp_path / "two-actions.json").ask("读取两份", document=None, scope=scope, task=task, budget=budget, model=FunctionModel(stream_function=stream), on_update=update))
    assert turn.status == "completed"
    assert [action.target for action in turn.actions] == ["first.txt", "revised.txt"]
    assert task.supplements[0].applied_request == 2


@pytest.mark.parametrize("exit_code", [0, 7])
def test_user_specified_check_runs_real_program_and_records_both_streams(tmp_path: Path, exit_code: int) -> None:
    script = tmp_path / "check.py"
    script.write_text(f"import pathlib,sys\nprint(pathlib.Path.cwd())\nprint('actual-stdout')\nprint('actual-stderr', file=sys.stderr)\nsys.exit({exit_code})\n", encoding="utf-8")
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"检查材料"}', tool_call_id="real-check")}
        else:
            yield "本次处理结束"
    turn = asyncio.run(Conversation(tmp_path / "records" / "check.json").ask("运行指定检查", document=None, scope=TaskScope(root=str(tmp_path), allowed_actions=["check"], provider="deepseek", recipients=["deepseek"], checks=[{"name": "检查材料", "argv": [sys.executable, str(script)]}]), model=FunctionModel(stream_function=stream)))
    action = turn.actions[0]
    assert action.exit_code == exit_code and action.cwd == str(tmp_path)
    assert "actual-stdout" in action.stdout and "actual-stderr" in action.stderr
    assert str(tmp_path) in action.stdout and action.process_id
    assert action.status == ("completed" if exit_code == 0 else "failed")
    assert turn.checks_passed is (exit_code == 0)
    assert turn.delivery_status == "awaiting_acceptance" and turn.status == "completed"


@pytest.mark.parametrize("stop", [False, True])
def test_check_timeout_and_stop_finish_owned_children_but_leave_other_programs(tmp_path: Path, stop: bool) -> None:
    import ctypes
    from pydantic_ai import CancellationToken
    pidfile = tmp_path / "child.pid"
    (tmp_path / "notes.txt").write_text("before-stop", encoding="utf-8")
    script = tmp_path / "long_check.py"
    child_program = (
        "import pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
    )
    if stop:
        child_program += (
            "print('started-real-child',flush=True)\n"
            "pending=pathlib.Path('child.pid.tmp')\n"
            "pending.write_text(str(child.pid))\n"
            "pending.replace('child.pid')\n"
        )
    else:
        child_program += (
            "pathlib.Path('child.pid').write_text(str(child.pid))\n"
            "print('started-real-child',flush=True)\n"
        )
    script.write_text(child_program + "time.sleep(30)\n", encoding="utf-8")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    token = CancellationToken()
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results and stop:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="read-before-stop")}
        elif not results or stop and len(results) == 1:
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"持续检查"}', tool_call_id="long-check")}
            if stop:
                yield {1: DeltaToolCall(name="edit_text", json_args='{"path":"notes.txt","old_text":"before-stop","new_text":"must-not-write"}', tool_call_id="edit-after-stop")}
        else:
            yield "实际检查未通过，已有输出保留。"
    async def run():
        async def stopper():
            while not pidfile.exists():
                await asyncio.sleep(0.01)
            token.cancel()
        stopping = asyncio.create_task(stopper()) if stop else None
        try:
            return await Conversation(tmp_path / "records" / "long.json").ask("运行持续检查", document=None, scope=TaskScope(root=str(tmp_path), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"], checks=[{"name":"持续检查", "argv":[sys.executable,str(script)], "timeout_seconds": 10 if stop else 0.3}]), model=FunctionModel(stream_function=stream), cancellation_token=token)
        finally:
            if stopping:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
    try:
        turn = asyncio.run(run())
        action = next(action for action in turn.actions if action.kind == "check")
        assert pidfile.exists() and "started-real-child" in action.stdout
        assert action.status == ("cancelled" if stop else "failed")
        assert action.timed_out is (not stop)
        assert turn.status == ("cancelled" if stop else "completed")
        fact = next(line for line in turn.execution_facts.splitlines() if action.id in line)
        assert f"状态：{'已停止' if stop else '失败'}" in fact
        assert "影响记录：未知" in fact and "内部逐文件影响未完整观测" in fact
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong], ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, int(pidfile.read_text()))
        if handle:
            code = ctypes.c_ulong()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            assert code.value != 259
        assert unrelated.poll() is None
        assert next(action for action in Conversation.open(tmp_path / "records" / "long.json").turns[-1].actions if action.kind == "check").exit_code is not None
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "before-stop"
        assert not any(action.kind == "edit" for action in turn.actions)
        assert action.cleanup_complete is True
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_two_tasks_in_one_folder_queue_writes_and_reconsider_changed_original(tmp_path: Path) -> None:
    from myrk.conversation import Task
    target = tmp_path / "notes.txt"
    target.write_text("苹果 3\n", encoding="utf-8")
    script = tmp_path / "external_check.py"
    script.write_text("import pathlib,time\npathlib.Path('check.started').touch()\nwhile not pathlib.Path('read.finished').exists(): time.sleep(.01)\npathlib.Path('notes.txt').write_text('苹果 3\\n外部新增\\n',encoding='utf-8')\n", encoding="utf-8")
    observations = []
    phase = 0
    async def check_stream(messages, info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"改写检查"}', tool_call_id="folder-check")}
        else:
            yield "检查结束"
    async def edit_stream(messages, info):
        nonlocal phase
        phase += 1
        if phase == 1:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="folder-read")}
        elif phase in [2, 3]:
            if phase == 2:
                assert "苹果 3" in str(messages)
                (tmp_path / "read.finished").touch()
            if phase == 3:
                assert "原文已变化" in str(messages) and "外部新增" in str(messages)
            yield {0: DeltaToolCall(name="edit_text", json_args='{"path":"notes.txt","old_text":"苹果 3","new_text":"苹果 5"}', tool_call_id=f"folder-edit-{phase}")}
        else:
            yield "重新核对后完成"
    async def run():
        check_scope = TaskScope(root=str(tmp_path), allowed_actions=["check"], provider="deepseek", recipients=["deepseek"], checks=[{"name":"改写检查", "argv":[sys.executable,str(script)]}])
        first = asyncio.create_task(Conversation(tmp_path / "records" / "one.json").ask("检查", document=None, scope=check_scope, model=FunctionModel(stream_function=check_stream), on_update=lambda update: observations.append(("check",update.action.status)) if update.kind == "action" else None))
        while not (tmp_path / "check.started").exists():
            await asyncio.sleep(0.01)
        second = await Conversation(tmp_path / "records" / "two.json").ask("改数量", document=None, scope=TaskScope(root=str(tmp_path),allowed_actions=["read","edit"],provider="deepseek",recipients=["deepseek"]),model=FunctionModel(stream_function=edit_stream),on_update=lambda update: observations.append((update.action.kind,update.action.status)) if update.kind == "action" else None)
        return await first, second
    first, second = asyncio.run(run())
    assert first.checks_passed is True and second.status == "completed"
    assert target.read_text(encoding="utf-8") == "苹果 5\n外部新增\n"
    assert observations.index(("check","completed")) < observations.index(("edit","started"))
    assert [action.status for action in second.actions] == ["completed","conflict","completed"]


def test_default_check_deadline_really_ends_the_owned_program_after_sixty_seconds(tmp_path: Path) -> None:
    from time import monotonic
    script = tmp_path / "default_timeout.py"
    script.write_text("import time\nprint('default-60-second-start',flush=True)\ntime.sleep(120)\n",encoding="utf-8")
    async def stream(messages, info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="run_check",json_args='{"name":"默认时限"}',tool_call_id="default-timeout")}
        else:
            yield "检查超时，不视为通过。"
    started = monotonic()
    turn = asyncio.run(Conversation(tmp_path / "records" / "default-timeout.json").ask("运行默认时限检查",document=None,scope=TaskScope(root=str(tmp_path),allowed_actions=["check"],provider="deepseek",recipients=["deepseek"],checks=[{"name":"默认时限","argv":[sys.executable,str(script)]}]),model=FunctionModel(stream_function=stream)))
    elapsed = monotonic() - started
    assert 59 <= elapsed < 75
    assert turn.actions[0].timed_out and turn.actions[0].exit_code is not None
    assert "default-60-second-start" in turn.actions[0].stdout and turn.checks_passed is False
