import asyncio

import pytest

from pydantic_ai.exceptions import ModelRetry, UsageLimitExceeded

from myrk.conversation import Supplement, Task
from myrk.task_control import RequestAccount, TaskBudget


def test_turn_account_can_bound_work_without_capping_request_attempts() -> None:
    counter = RequestAccount(request_limit=None, max_turns=250)

    assert counter.request_limit is None
    assert counter.max_turns == 250
    assert counter.turns_used == 0


def test_repeating_one_work_step_consumes_only_one_turn() -> None:
    task = Task(goal="服务重试沿用同一办事轮", scope=None, request_limit=10)
    budget = TaskBudget(task)

    assert budget.reserve_turn("run-1:step-1") == 1
    assert budget.reserve_request() == 1
    assert budget.reserve_turn("run-1:step-1") == 1
    assert budget.reserve_request() == 2
    assert budget.reserve_turn("run-1:step-2") == 2
    assert budget.reserve_request() == 3

    assert budget.counter.turns_used == 2
    assert budget.requests == 3


def test_child_turn_limit_preserves_unlimited_main_work_and_unique_request_numbers() -> None:
    task = Task(goal="子助理用完后主助理继续", scope=None)
    budget = TaskBudget(task)
    work = RequestAccount(max_turns=250)
    child = budget.fork_requests(work)

    for turn in range(1, 251):
        assert child.reserve_turn(f"child-run:step-{turn}") == turn
        assert child.reserve_request() == turn
    assert child.reserve_turn("child-run:step-250") == 250
    assert child.reserve_request() == 251
    with pytest.raises(UsageLimitExceeded, match="250轮办事上限"):
        child.reserve_turn("child-run:step-251")

    assert child.counter is work
    assert work.turns_used == 250
    assert child.requests == 251
    assert child.reason is not None
    assert budget.reason is None
    assert not budget.token.cancelled
    for turn in range(1, 301):
        assert budget.reserve_turn(f"main-run:step-{turn}") == turn
        assert budget.reserve_request() == turn + 251

    assert budget.turns_used == 300
    assert budget.requests == 300
    assert task.total_requests == 551


def test_legacy_shared_request_limit_applies_to_main_and_child() -> None:
    task = Task(goal="旧工作继续共用原请求次数", scope=None, request_policy="shared",
                request_limit=10, requests=8, total_requests=8)
    budget = TaskBudget(task)
    work = RequestAccount(max_turns=250)
    child = budget.fork_requests(work)

    assert budget.reserve_request() == 9
    assert child.reserve_request() == 10
    with pytest.raises(UsageLimitExceeded, match="10次请求尝试上限"):
        child.reserve_request()
    with pytest.raises(UsageLimitExceeded, match="10次请求尝试上限"):
        budget.reserve_request()

    assert child.counter is task
    assert task.requests == task.total_requests == 10
    assert work.requests == 0
    assert not budget.token.cancelled


def test_parent_and_child_share_one_active_clock_and_human_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr("myrk.task_control.monotonic", lambda: now[0])
    task = Task(goal="父子只统计同一份实际耗时", scope=None)
    budget = TaskBudget(task)
    child = budget.fork_requests(RequestAccount(max_turns=250))

    budget.start()
    now[0] = 1
    child.start()
    now[0] = 3
    budget.account()
    now[0] = 4
    child.account()
    assert task.active_seconds == 4

    with budget.waiting():
        now[0] = 8
        child.account()
        assert task.active_seconds == 4
        with child.active_work():
            now[0] = 10
            budget.account()
            assert task.active_seconds == 6
            now[0] = 11
        now[0] = 13

    now[0] = 14
    child.finish()
    now[0] = 16
    budget.finish()
    assert task.active_seconds == 10


def test_child_local_limit_does_not_hide_later_global_stop() -> None:
    task = Task(goal="本人停止所有工作", scope=None)
    budget = TaskBudget(task)
    child = budget.fork_requests(RequestAccount(max_turns=1))
    child.reserve_turn("child:step-1")
    with pytest.raises(UsageLimitExceeded, match="1轮办事上限"):
        child.reserve_turn("child:step-2")

    assert budget.reason is None
    assert not budget.token.cancelled
    budget.reason = "本人已要求停止全部工作。"
    budget.token.cancel()

    for participant in [budget, child]:
        assert participant.reason == "本人已要求停止全部工作。"
        with pytest.raises(UsageLimitExceeded, match="本人已要求停止全部工作"):
            participant.checkpoint()


def test_child_and_parent_share_supplement_admission_without_losing_adoption_state() -> None:
    task = Task(goal="分工期间仍采用用户新补充", scope=None)
    budget = TaskBudget(task)
    child = budget.fork_requests(RequestAccount(max_turns=250))
    assert not budget.accepting_supplements and not child.accepting_supplements
    budget.set_supplement_admission(True)
    assert budget.accepting_supplements and child.accepting_supplements
    supplement = Supplement(text="保留原文件，先核对新资料。")
    withdrawn = Supplement(text="已撤回的补充不阻止下一步。", revoked_at="2026-11-18T00:00:00+00:00")
    task.supplements.extend([supplement, withdrawn])

    with pytest.raises(ModelRetry, match="用户已有新补充"):
        child.checkpoint()
    child.checkpoint(applied_supplements={supplement.id})
    with pytest.raises(ModelRetry, match="用户已有新补充"):
        budget.checkpoint()
    supplement.applied_request = child.reserve_request()
    child.checkpoint()
    budget.checkpoint()

    budget.set_supplement_admission(False)
    assert not budget.accepting_supplements and not child.accepting_supplements
    budget.set_supplement_admission(True)
    budget.token.cancel()
    assert not budget.accepting_supplements and not child.accepting_supplements


def test_elapsed_time_remains_statistics_after_old_task_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr("myrk.task_control.monotonic", lambda: now[0])
    task = Task(goal="保留耗时但不自动停止", scope=None, request_limit=10,
                active_seconds_limit=1, active_seconds=30)
    budget = TaskBudget(task)
    budget.start()
    now[0] = 2

    budget.checkpoint()

    assert task.active_seconds == 32
    assert budget.reason is None
    assert not budget.token.cancelled
    assert budget.reserve_request() == 1
    budget.finish()


def test_task_watcher_updates_elapsed_time_until_explicit_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr("myrk.task_control.monotonic", lambda: now[0])
    task = Task(goal="持续统计直到本人停止", scope=None, request_limit=10,
                active_seconds_limit=1, active_seconds=30)
    budget = TaskBudget(task)
    budget.start()
    ticks: list[float] = []

    def on_tick() -> None:
        ticks.append(task.active_seconds)
        budget.token.cancel()

    async def observe() -> None:
        watcher = asyncio.create_task(budget.watch(on_tick))
        await asyncio.sleep(0)
        now[0] = 2
        await asyncio.wait_for(watcher, timeout=1)

    asyncio.run(observe())

    assert ticks == [32]
    assert budget.reason is None
    budget.finish()
