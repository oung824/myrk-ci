"""Per-assistant requests with shared task time, supplements, and stopping."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import monotonic
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, PrivateAttr, field_serializer
from pydantic_ai import CancellationToken
from pydantic_ai.exceptions import ModelRetry, UsageLimitExceeded

from myrk.request_observation import RequestObservation

if TYPE_CHECKING:
    from myrk.conversation import Supplement, Task


def persist_supplement_selection(supplements: list["Supplement"], save: Callable[[], None]) -> bool:
    selected = [item for item in supplements if not item.selected and item.applied_request is None]
    if not selected:
        return False
    for item in selected:
        item.selected = True
    try:
        save()
    except Exception:
        for item in selected:
            item.selected = False
        raise
    return True


class WorkStep(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    completed: bool = False
    capacity_recovery_used: bool = False
    request_pending: bool = False
    turn_number: int | None = Field(default=None, ge=1, strict=True)


class RequestAttempt(BaseModel):
    number: int
    own_number: int | None = None
    step_id: str | None = None
    model: str
    provider: str
    delegation_id: str | None = None
    actor: Literal["main", "delegation", "memory_review", "action_review"] = "main"
    purpose: Literal["work", "compaction"] = "work"
    status: Literal["sent", "completed", "failed"] = "sent"
    http_status: int | None = None
    error: str | None = None
    decision: str = "正在等待服务回应；用量尚未取得。"
    input_tokens: int | None = None
    output_tokens: int | None = None
    observation: RequestObservation | None = None


class RequestAccount(BaseModel):
    _pending_review_requests: int = PrivateAttr(default=0)
    work_step: WorkStep | None = None
    request_limit: int | None = Field(default=None, ge=1, strict=True)
    requests: int = Field(default=0, ge=0, strict=True)
    max_turns: int | None = Field(default=None, ge=1, strict=True)
    turns_used: int = Field(default=0, ge=0, strict=True)
    review_requests: int = 0
    review_unmeasured_requests: int = 0
    review_seconds: float = 0
    review_input_tokens: int | None = None
    review_output_tokens: int | None = None

    @field_serializer("review_input_tokens", "review_output_tokens")
    def serialize_review_total(self, value: int | None) -> int | None:
        return None if self._pending_review_requests else value


class _TaskRuntime:
    def __init__(self, token: CancellationToken | None) -> None:
        self.token = token or CancellationToken()
        self.reason: str | None = None
        self.clock: float | None = None
        self.waiting = 0
        self.active_work = 0
        self.runs = 0
        self.accepting_supplements = False


class TaskBudget:
    def __init__(self, task: "Task", token: CancellationToken | None = None) -> None:
        self.task = task
        self._counter: RequestAccount = task
        self._runtime = _TaskRuntime(token)
        self._reason: str | None = None
        self._turns: dict[str, int] = {}

    def use_saved_task(self, task: "Task") -> None:
        """Keep this runtime's stop signal while restoring the task's account."""
        if task.id != self.task.id or self._counter is not self.task:
            raise ValueError("运行额度不属于原主任务，不能换用其他身份续办。")
        self.task = task
        self._counter = task

    @property
    def counter(self) -> RequestAccount:
        return self._counter

    @property
    def requests(self) -> int:
        return self.counter.requests

    @property
    def request_limit(self) -> int | None:
        return self.counter.request_limit

    @property
    def max_turns(self) -> int | None:
        return self.counter.max_turns

    @property
    def turns_used(self) -> int:
        return self.counter.turns_used

    @property
    def token(self) -> CancellationToken:
        return self._runtime.token

    @property
    def reason(self) -> str | None:
        return self._runtime.reason or self._reason

    @reason.setter
    def reason(self, value: str | None) -> None:
        self._runtime.reason = value

    @property
    def accepting_supplements(self) -> bool:
        return self._runtime.accepting_supplements and not self.token.cancelled

    def set_supplement_admission(self, accepting: bool) -> None:
        self._runtime.accepting_supplements = accepting

    def fork_requests(self, work: RequestAccount) -> "TaskBudget":
        child = TaskBudget(self.task, self.token)
        child._counter = work if self.task.request_policy == "independent" else self.task
        child._runtime = self._runtime
        if child.counter is self.counter:
            child._turns = self._turns
        return child

    def start(self) -> None:
        if self._runtime.runs == 0:
            self._runtime.clock = monotonic()
        self._runtime.runs += 1

    def account(self) -> None:
        now = monotonic()
        if self._runtime.clock is not None and (not self._runtime.waiting or self._runtime.active_work):
            self.task.active_seconds += now - self._runtime.clock
        self._runtime.clock = now

    def checkpoint(self, *, preparing_request: bool = False, applied_supplements: set[str] | None = None) -> None:
        self.account()
        if self.token.cancelled:
            raise UsageLimitExceeded(self.reason or "工作已要求停止，未安排新动作。")
        if not preparing_request and any(
            item.applied_request is None if applied_supplements is None else item.id not in applied_supplements
            for item in self.task.active_supplements
        ):
            raise ModelRetry("用户已有新补充；本次未开始的动作暂停，请先采用补充再重新决定动作。")

    def check_turn(self, key: str) -> int:
        """Check the next work step without charging a summary preparation."""
        self.checkpoint(preparing_request=True)
        if self.counter.work_step is not None and self.counter.work_step.id == key and self.counter.work_step.turn_number is not None:
            return self.counter.work_step.turn_number
        if key in self._turns:
            return self._turns[key]
        if self.max_turns is not None and self.turns_used >= self.max_turns:
            self._reason = self._reason or f"已达到本助理{self.max_turns}轮办事上限（同一步的服务重试不另算）；已有进展保留。"
            raise UsageLimitExceeded(self._reason)
        return self.turns_used + 1

    def reserve_turn(self, key: str) -> int:
        number = self.check_turn(key)
        if key in self._turns or (self.counter.work_step is not None and self.counter.work_step.id == key and self.counter.work_step.turn_number is not None):
            return number
        self.counter.turns_used += 1
        if self.counter.work_step is not None and self.counter.work_step.id == key:
            self.counter.work_step.turn_number = self.counter.turns_used
        self._turns[key] = self.turns_used
        return self.turns_used

    def check_action_start(self) -> None:
        self.checkpoint(preparing_request=True)
        if self.max_turns is not None and self.turns_used >= self.max_turns:
            self._reason = f"已达到本助理{self.max_turns}轮办事上限；最后一轮只用于文字交回，未启动新动作。"
            raise UsageLimitExceeded(self._reason)

    def reserve_request(self) -> int:
        self.checkpoint(preparing_request=True)
        if self.request_limit is not None and self.requests >= self.request_limit:
            self._reason = f"已达到本助理{self.request_limit}次请求尝试上限；已有进展保留，请决定是否增加额度。"
            raise UsageLimitExceeded(self._reason)
        self.counter.requests += 1
        self.task.total_requests += 1
        return self.task.total_requests

    @contextmanager
    def active_work(self) -> Iterator[None]:
        """Independent review remains active while another tool awaits a human."""
        self.account()
        self._runtime.active_work += 1
        try:
            yield
        finally:
            self.account()
            self._runtime.active_work -= 1

    @contextmanager
    def waiting(self) -> Iterator[None]:
        """Pause active time while waiting for a user decision, including nested waits."""
        self.account()
        self._runtime.waiting += 1
        try:
            yield
        finally:
            self.account()
            self._runtime.waiting -= 1

    async def watch(self, on_tick: Callable[[], None] | None = None) -> None:
        last_tick = monotonic()
        while not self.token.cancelled:
            self.account()
            if on_tick is not None and monotonic() - last_tick >= 0.25:
                on_tick()
                last_tick = monotonic()
            await asyncio.sleep(0.02)

    def finish(self) -> None:
        self.account()
        self._runtime.runs -= 1
        if self._runtime.runs == 0:
            self._runtime.clock = None
