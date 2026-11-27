import asyncio
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from pydantic_ai.exceptions import ModelContextWindowExceededError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from myrk.conversation import Conversation, Task
from myrk.provider import ProviderConfig
from myrk.task_control import TaskBudget


def configured_task():
    return Task(goal="Verify the original evidence", scope=None, provider_config=ProviderConfig(
        context_window_override=30000, output_token_reserve=1024, context_safety_margin=512))


async def seed_conversation(path, task):
    async def respond(messages, info):
        yield "Original evidence remains unverified. " * 100

    conversation = Conversation(path)
    model = FunctionModel(stream_function=respond, profile={"context_window": 30000})
    for index in range(8):
        turn = await conversation.ask(f"Record evidence item {index}.", document=None, model=model, task=task)
        assert turn.status == "completed", turn.error
    return conversation


def test_reopening_the_same_unfinished_step_does_not_refresh_window_recovery(tmp_path: Path):
    async def run():
        task = configured_task()
        path = tmp_path / "conversation.json"
        conversation = await seed_conversation(path, task)
        sends, summaries = [], []

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            raise ModelContextWindowExceededError("synthetic", "planned transport window error")
            yield "unreachable"

        def summarize(messages, info):
            summaries.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("Original evidence is unverified; keep the same pending work.")])

        model = FunctionModel(summarize, stream_function=respond, profile={"context_window": 30000})
        first = await conversation.ask("Verify the original evidence.", document=None, model=model, task=task)
        assert first.status == "failed", first.error
        assert len(sends) == 2 and len(summaries) == 1
        used_turns, used_requests = task.turns_used, task.total_requests

        reopened = Conversation.open(path)
        assert len(sends) == 2
        assert (await reopened.verify_recovery_async(task.id)).can_resume
        resumed = await reopened.resume_task(task.id, model=model)
        assert not resumed.compactions, "The used recovery must stop before preparing another summary."
        assert resumed.status == "failed", resumed.error
        assert "本步骤已用一次容量超限" in resumed.error
        assert len(sends) == 3, "The same unfinished step gets no second automatic capacity recovery after reopening."
        assert len(summaries) == 1
        assert resumed.task.turns_used == used_turns
        assert resumed.task.total_requests == used_requests + 1
        process = subprocess.run([sys.executable, "-B", "-m", "tests.step_window_process", str(path)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=30,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.returncode == 0, process.stderr
        persisted = json.loads(process.stdout)
        assert persisted["task_id"] == task.id
        assert persisted["step_id"] == resumed.task.work_step.id
        assert persisted["turns"] == used_turns
        assert persisted["requests"] == used_requests + 2
        assert persisted["status"] == "failed"
        assert "已用一次容量超限" in persisted["decision"]

    asyncio.run(run())




def test_save_failure_after_capacity_error_cannot_restore_an_unused_opportunity(tmp_path: Path):
    async def run():
        task = configured_task()
        path = tmp_path / "conversation.json"
        conversation = await seed_conversation(path, task)
        backup = path.with_name("saved-original.json")
        sends = []

        async def respond(messages, info):
            sends.append(messages)
            path.rename(backup)
            path.mkdir()
            raise ModelContextWindowExceededError("synthetic", "planned error before failed persistence")
            yield "unreachable"

        model = FunctionModel(stream_function=respond, profile={"context_window": 30000})
        with pytest.raises(OSError):
            await conversation.ask("Verify the original evidence.", document=None, model=model, task=task)
        assert conversation.save_failures
        assert len(sends) == 1
        original = backup.read_bytes()
        path.rmdir()
        backup.rename(path)
        assert path.read_bytes() == original
        restored = Conversation.open(path)
        assert restored.turns[-1].task.work_step.capacity_recovery_used, "An unconfirmed in-flight step must not gain another opportunity when its result cannot be saved."
        assert restored.turns[-1].task.id == task.id
        assert restored.turns[-1].task.total_requests == task.total_requests

    asyncio.run(run())

def test_legacy_unfinished_step_without_saved_boundary_gets_no_new_recovery(tmp_path: Path):
    async def run():
        task = configured_task()
        path = tmp_path / "conversation.json"
        conversation = await seed_conversation(path, task)

        async def respond(messages, info):
            raise ModelContextWindowExceededError("synthetic", "planned window error")
            yield "unreachable"

        model = FunctionModel(lambda *_: ModelResponse(parts=[TextPart("Evidence remains pending.")]),
                              stream_function=respond, profile={"context_window": 30000})
        failed = await conversation.ask(task.goal, document=None, model=model, task=task)
        assert failed.status == "failed"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for turn in raw["turns"]:
            turn["task"].pop("work_step", None)
        path.write_text(json.dumps(raw), encoding="utf-8")
        original = path.read_bytes()
        reopened = Conversation.open(path)
        assert path.read_bytes() == original, "Opening legacy history does not rewrite it or send requests."
        assert (await reopened.verify_recovery_async(task.id)).can_resume
        resumed = await reopened.resume_task(task.id, model=model)
        assert "已用一次容量超限" in resumed.request_attempts[-1].decision
        assert resumed.task.id == task.id
        assert resumed.task.approval == task.approval
        assert resumed.task.turns_used == task.turns_used

    asyncio.run(run())

def test_interruption_during_recovery_then_plain_continue_keeps_the_step(tmp_path: Path):
    async def run():
        from pydantic_ai import CancellationToken
        task = configured_task()
        path = tmp_path / "conversation.json"
        conversation = await seed_conversation(path, task)
        token = CancellationToken()

        async def respond(messages, info):
            raise ModelContextWindowExceededError("synthetic", "planned window error")
            yield "unreachable"

        async def summarize(messages, info):
            token.cancel()
            await asyncio.sleep(0)
            return ModelResponse(parts=[TextPart("Interrupted while preserving pending evidence.")])

        model = FunctionModel(summarize, stream_function=respond, profile={"context_window": 30000})
        stopped = await conversation.ask(task.goal, document=None, model=model, task=task, cancellation_token=token)
        assert stopped.status == "cancelled", stopped.error
        step_id = task.work_step.id
        restored = Conversation.open(path)
        continued = await restored.ask("Continue", document=None, model=model)
        assert continued.task.id == task.id, "An ambiguous continue after interruption belongs to the unfinished task."
        assert continued.task.work_step.id == step_id
        assert "已用一次容量超限" in continued.request_attempts[-1].decision
        assert continued.task.turns_used == task.turns_used

    asyncio.run(run())

def test_desktop_plain_continue_after_reopen_keeps_unfinished_identity(tmp_path: Path):
    from tests.test_desktop_service import connection
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请停止"})
        started = channel.receive()
        assert started["event"] == "started"
        assert channel.receive()["event"] == "text"
        channel.send({"action": "stop"})
        assert channel.receive()["event"] == "stopping"
        stopped = channel.receive()
        assert stopped["turn"]["status"] == "cancelled"
        step_id = stopped["turn"]["task"]["work_step"]["id"]
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only"})
        channel.receive()
        channel.send({"action": "open", "path": started["path"]})
        assert channel.receive()["event"] == "conversation"
        channel.send({"action": "send", "prompt": "现在继续"})
        continued = channel.receive()
        assert continued["event"] == "started"
        assert continued["task_id"] == started["task_id"]
        while (event := channel.receive())["event"] != "finished":
            assert event["event"] != "error", event
        assert event["turn"]["status"] == "completed"
        assert event["turn"]["task"]["work_step"]["id"] == step_id
        channel.send({"action": "send", "prompt": "新工作", "new_task": True})
        fresh = channel.receive()
        assert fresh["event"] == "started"
        assert fresh["task_id"] != started["task_id"]
        while (event := channel.receive())["event"] != "finished":
            assert event["event"] != "error", event
        assert event["turn"]["task"]["work_step"]["id"] != step_id



def test_reentering_with_a_new_runtime_account_cannot_reset_the_saved_step(tmp_path: Path):
    async def run():
        from pydantic_ai import CancellationToken
        task = configured_task()
        task.request_limit, task.max_turns = 12, 9
        path = tmp_path / "conversation.json"
        conversation = await seed_conversation(path, task)
        token = CancellationToken()

        async def respond(messages, info):
            raise ModelContextWindowExceededError("synthetic", "planned window error")
            yield "unreachable"

        def summarize(messages, info):
            token.cancel()
            return ModelResponse(parts=[TextPart("Same unfinished work.")])

        model = FunctionModel(summarize, stream_function=respond, profile={"context_window": 30000})
        await conversation.ask(task.goal, document=None, model=model, task=task, cancellation_token=token)
        used_requests, used_turns = task.total_requests, task.turns_used
        replacement = configured_task()
        replacement.id = task.id
        resumed = await conversation.ask("Retry", document=None, model=model, task=replacement, budget=TaskBudget(replacement))
        assert resumed.request_attempts[0].step_id == task.work_step.id
        assert resumed.task.work_step.capacity_recovery_used
        assert resumed.task.turns_used == used_turns
        assert resumed.task.total_requests == used_requests + 1
        assert resumed.task.request_limit == 12
        assert resumed.task.max_turns == 9

    asyncio.run(run())

def test_normal_growth_can_create_three_summaries_across_reopens(tmp_path: Path):
    async def run():
        task = configured_task()
        task.provider_config = task.provider_config.model_copy(update={"context_window_override": 15000})
        path = tmp_path / "conversation.json"
        conversation = Conversation(path)
        summaries = []
        step_ids = set()

        def summarize(messages, info):
            summaries.append(messages)
            return ModelResponse(parts=[TextPart("Original evidence 937 remains unverified. Continue the authorized work.")])

        async def respond(messages, info):
            yield "Original evidence 937 remains unverified. " * 600

        model = FunctionModel(summarize, stream_function=respond, profile={"context_window": 15000})
        for index in range(15):
            turn = await conversation.ask(f"Record evidence {index} without inventing results.", document=None, model=model, task=task)
            assert turn.status == "completed", turn.error
            assert turn.task.work_step.completed
            assert not turn.task.work_step.capacity_recovery_used
            step_ids.add(turn.task.work_step.id)
            conversation = Conversation.open(path)
            task = conversation.turns[-1].task
        assert len(summaries) >= 3
        assert len(step_ids) == 15
        assert task.turns_used == 15
        assert task.total_requests == 15 + len(summaries)

    asyncio.run(run())

async def interrupted_recovery(path, task):
    from pydantic_ai import CancellationToken
    conversation = await seed_conversation(path, task)
    token = CancellationToken()

    async def respond(messages, info):
        raise ModelContextWindowExceededError("synthetic", "planned window error")
        yield "unreachable"

    def summarize(messages, info):
        token.cancel()
        return ModelResponse(parts=[TextPart("The authorized verification remains unfinished.")])

    stopped = await conversation.ask(task.goal, document=None, task=task, cancellation_token=token,
        model=FunctionModel(summarize, stream_function=respond, profile={"context_window": 30000}))
    assert stopped.status == "cancelled", stopped.error
    return conversation, stopped


def test_continuing_accepts_explicit_new_material_scope_without_resetting_the_step(tmp_path: Path):
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall
    from myrk.conversation import TaskScope

    async def run():
        task = configured_task()
        path = tmp_path / "records" / "conversation.json"
        path.parent.mkdir()
        conversation, stopped = await interrupted_recovery(path, task)
        original_step = stopped.task.work_step.id
        material = tmp_path / "newly-authorized"
        material.mkdir()
        (material / "notes.txt").write_text("authorized evidence 937", encoding="utf-8")
        scope = TaskScope(root=str(material), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])

        async def respond(messages, info):
            if not any(isinstance(part, ToolReturnPart) and part.tool_call_id == "new-material" for message in messages for part in message.parts):
                yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="new-material")}
            else:
                yield "The authorized evidence was read."

        continued = await conversation.ask("Read the newly authorized material.", document=None, scope=scope,
            model=FunctionModel(stream_function=respond, profile={"context_window": 30000}))
        assert continued.status == "completed", continued.error
        assert continued.task.id == task.id
        assert continued.request_attempts[0].step_id == original_step
        assert continued.task.scope == scope
        assert continued.actions[0].status == "completed"
        assert conversation.turns[-2].task.scope is None

    asyncio.run(run())

def test_continuing_after_the_tool_phase_stops_keeps_task_but_starts_a_new_step(tmp_path: Path):
    from pydantic_ai import CancellationToken
    from pydantic_ai.models.function import DeltaToolCall
    from myrk.conversation import TaskScope

    async def run():
        scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
        task = configured_task()
        task.scope = scope
        path = tmp_path / "records" / "conversation.json"
        conversation = Conversation(path)
        token = CancellationToken()

        async def request_read(messages, info):
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"missing.txt"}', tool_call_id="failed-read")}

        def stop_after_failure(update):
            if update.action is not None and update.action.kind == "read" and update.action.status == "failed":
                token.cancel()

        stopped = await conversation.ask(task.goal, document=None, task=task, scope=scope,
            model=FunctionModel(stream_function=request_read, profile={"context_window": 30000}),
            cancellation_token=token, on_update=stop_after_failure)
        assert stopped.status == "cancelled", stopped.error
        assert task.work_step.completed
        previous_step = task.work_step.id
        used_requests, used_turns = task.total_requests, task.turns_used

        async def report(messages, info):
            yield "The read failed; report the unfinished work."

        restored = Conversation.open(path)
        continued = await restored.ask("Continue and report the failed read.", document=None,
            model=FunctionModel(stream_function=report, profile={"context_window": 30000}))
        assert continued.status == "completed", continued.error
        assert continued.task.id == task.id
        assert continued.task.scope == scope
        assert continued.task.total_requests == used_requests + 1
        assert continued.task.turns_used == used_turns + 1
        assert continued.request_attempts[0].step_id != previous_step
        assert len(restored.turns[-2].actions) == 1 and not continued.actions

    asyncio.run(run())

def test_desktop_continuation_accepts_a_new_approval_choice_and_preserves_old_turn(tmp_path: Path):
    from tests.test_desktop_service import connection
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请停止"})
        started = channel.receive()
        assert channel.receive()["event"] == "text"
        channel.send({"action": "stop"})
        assert channel.receive()["event"] == "stopping"
        stopped = channel.receive()
        original = stopped["turn"]["task"]
        next_mode = "request_approval" if original["approval"]["mode"] == "full_access" else "full_access"
        channel.send({"action": "send", "prompt": "按新批准方式继续", "approval_mode": next_mode})
        while (event := channel.receive())["event"] != "started":
            assert event["event"] != "error", event
        assert event["task_id"] == started["task_id"]
        while (event := channel.receive())["event"] != "finished":
            assert event["event"] != "error", event
        assert event["turn"]["status"] == "completed"
        assert event["turn"]["task"]["approval"]["mode"] == next_mode
        assert event["turn"]["request_attempts"][0]["step_id"] == original["work_step"]["id"]
        restored = Conversation.open(Path(event["path"]))
        assert restored.turns[-2].task.approval.mode == original["approval"]["mode"]
        assert restored.turns[-2].task.requests == original["requests"]


def test_public_resume_keeps_the_previous_turn_snapshot_unchanged(tmp_path: Path):
    async def run():
        task = configured_task()
        task.request_limit, task.max_turns = 12, 9
        path = tmp_path / "conversation.json"
        conversation = await seed_conversation(path, task)

        async def respond(messages, info):
            raise ModelContextWindowExceededError("synthetic", "planned capacity failure")
            yield "unreachable"

        model = FunctionModel(lambda *_: ModelResponse(parts=[TextPart("The verification remains unfinished.")]),
            stream_function=respond, profile={"context_window": 30000})
        failed = await conversation.ask(task.goal, document=None, task=task, model=model)
        assert failed.status == "failed"
        restored = Conversation.open(path)
        assert (await restored.verify_recovery_async(task.id)).can_resume
        previous = restored.turns[-1]
        snapshot = previous.model_dump(mode="json")
        resumed = await restored.resume_task(task.id, model=model)
        assert previous.model_dump(mode="json") == snapshot
        assert Conversation.open(path).turns[-2].model_dump(mode="json") == snapshot
        assert resumed.task.requests == snapshot["task"]["requests"] + 1
        assert resumed.task.total_requests == snapshot["task"]["total_requests"] + 1
        assert resumed.task.turns_used == snapshot["task"]["turns_used"]
        assert resumed.task.request_limit == 12 and resumed.task.max_turns == 9
        assert resumed.task.work_step.id == snapshot["task"]["work_step"]["id"]
        assert resumed.task.work_step.capacity_recovery_used

    asyncio.run(run())


@pytest.mark.parametrize("task_source", ["caller", "stored_turn"])
def test_explicit_task_reentry_keeps_the_previous_turn_snapshot(tmp_path: Path, task_source):
    async def run():
        task = configured_task()
        task.request_limit, task.max_turns = 4, 3
        path = tmp_path / "conversation.json"
        conversation = Conversation(path)

        async def respond(messages, info):
            yield "The evidence still needs verification."

        model = FunctionModel(stream_function=respond, profile={"context_window": 30000})
        previous = await conversation.ask(task.goal, document=None, task=task, model=model)
        snapshot = previous.model_dump(mode="json")
        incoming = task if task_source == "caller" else previous.task
        if task_source == "caller":
            incoming.provider_config = incoming.provider_config.model_copy(update={"auto_compaction": False})
        current = await conversation.ask("Continue the same task.", document=None, task=incoming, model=model)
        assert previous.model_dump(mode="json") == snapshot
        assert Conversation.open(path).turns[-2].model_dump(mode="json") == snapshot
        assert current.task.id == snapshot["task"]["id"]
        assert current.task.requests == current.task.total_requests == 2
        assert current.task.turns_used == 2
        assert current.task.request_limit == 4 and current.task.max_turns == 3
        assert current.task.work_step.id != snapshot["task"]["work_step"]["id"]
        if task_source == "caller":
            assert task.requests == 2
            assert current.task.provider_config.auto_compaction is False

    asyncio.run(run())


def test_caller_scope_update_keeps_the_previous_execution_scope_snapshot(tmp_path: Path):
    from myrk.conversation import TaskScope

    async def run():
        scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
        task = Task(goal="Read authorized evidence", scope=scope, provider_config=configured_task().provider_config)
        conversation = Conversation(tmp_path / "conversation.json")

        async def respond(messages, info):
            yield "The evidence remains unverified."

        model = FunctionModel(stream_function=respond, profile={"context_window": 30000})
        previous = await conversation.ask(task.goal, document=None, task=task, scope=scope, model=model)
        assert previous.status == "completed", previous.error
        snapshot = previous.model_dump(mode="json")
        task.scope.allowed_actions.append("calculate")
        current = await conversation.ask("Continue with the newly authorized calculation scope.", document=None,
            task=task, scope=task.scope, model=model)
        assert current.status == "completed", current.error
        assert current.execution_scope.allowed_actions == ["read", "calculate"]
        assert task.scope.allowed_actions == ["read", "calculate"]
        assert task.requests == current.task.requests == 2
        assert previous.model_dump(mode="json") == snapshot
        assert Conversation.open(conversation.path).turns[-2].model_dump(mode="json") == snapshot
        assert previous.execution_scope.allowed_actions == ["read"]

    asyncio.run(run())
