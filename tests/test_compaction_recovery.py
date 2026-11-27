import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
import httpx2
from pydantic_ai import models
from pydantic_ai.exceptions import ModelConnectionError, ModelContextWindowExceededError, ModelHTTPError, ModelTimeoutError
from pydantic_ai.messages import CompactionPart, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, ThinkingPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage

from myrk.compaction import CompactionRecord, SourcePart, source_fingerprint, transcript_sources
from myrk.conversation import Conversation, ConversationRecord, Supplement, Task, TaskScope, Turn
from myrk.provider import ProviderConfig, configured_model
from myrk.task_control import RequestAttempt, TaskBudget


def history():
    messages = [ModelRequest(parts=[UserPromptPart("Original goal: verify 937; never invent evidence.")])]
    for index in range(12):
        messages.extend([ModelResponse(parts=[TextPart(f"Evidence {index}: " + "historical information " * 180)]),
                         ModelRequest(parts=[UserPromptPart(f"Continue item {index}.")])])
    return messages


def conversation_at(path, messages=None):
    path.write_text(ConversationRecord(version=8, messages=messages or history(), turns=[]).model_dump_json(), encoding="utf-8")
    return Conversation.open(path)


def test_window_error_recovers_once_in_the_same_last_work_turn(tmp_path: Path):
    async def run():
        config = ProviderConfig(context_window_override=30000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify originals", scope=None, provider_config=config, max_turns=1)
        sends, summaries = [], []

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            if len(sends) == 1:
                raise ModelContextWindowExceededError("synthetic", "synthetic window exceeded")
            assert any(isinstance(part, CompactionPart) for message in messages for part in message.parts)
            assert not info.function_tools
            yield "Pending original verification."

        def summary(messages, info):
            summaries.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("Goal: verify 937; never invent evidence. Pending original verification.")],
                                 usage=RequestUsage(input_tokens=7000, output_tokens=25))

        path = tmp_path / "record.json"
        turn = await conversation_at(path).ask(task.goal, document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 30000}))
        assert turn.status == "completed", turn.error
        assert len(sends) == 2 and len(summaries) == 1
        assert task.turns_used == 1 and task.total_requests == 3
        assert [(attempt.purpose, attempt.status) for attempt in turn.request_attempts] == [
            ("work", "failed"), ("compaction", "completed"), ("work", "completed")]
        reopened = Conversation.open(path)
        assert reopened.turns[-1].task.turns_used == 1
        assert reopened.turns[-1].task.total_requests == 3
    asyncio.run(run())


def test_valid_summary_above_soft_trigger_is_reused_for_a_temporary_work_retry(tmp_path: Path):
    async def run():
        source = [ModelRequest(parts=[UserPromptPart("Original requirement: " + "x" * 28800)]),
                  ModelResponse(parts=[TextPart("Older observations: " + "y" * 5000)]),
                  ModelRequest(parts=[UserPromptPart("Latest requirement: keep verifying the original evidence.")])]
        for index in range(8):
            source.extend([ModelResponse(parts=[ToolCallPart("read_source", {}, f"retry-evidence-{index}")]),
                           ModelRequest(parts=[ToolReturnPart("read_source", "Evidence " * 300, f"retry-evidence-{index}")])])
        sends, summaries = [], []
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify original evidence.", scope=None, provider_config=config, max_turns=1)

        def summary(messages, info):
            summaries.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("Goal: verify original evidence. Reads completed; verification remains pending.")])

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            if len(sends) == 1:
                raise ModelHTTPError(503, "synthetic", "temporary work service failure")
            yield "Verification remains pending."

        path = tmp_path / "record.json"
        turn = await conversation_at(path, source).ask("Continue verification.", document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000}))
        assert turn.status == "completed", turn.error
        assert len(summaries) == 1 and len(sends) == 2
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        prepared = saved.prepared_contexts[-1]
        assert prepared.input_budget_tokens * config.compaction_trigger <= prepared.input_tokens <= prepared.input_budget_tokens
        assert len(saved.prepared_contexts) == 2
        assert all(item.accepted and item.input_budget_tokens * config.compaction_trigger <= item.input_tokens <= item.input_budget_tokens
                   for item in saved.prepared_contexts)
        assert [(attempt.purpose, attempt.status) for attempt in turn.request_attempts] == [
            ("compaction", "completed"), ("work", "failed"), ("work", "completed")]
        assert task.turns_used == 1 and task.total_requests == 3

    asyncio.run(run())


def test_supplement_during_summary_is_saved_once_and_applied_by_the_next_work_request(tmp_path: Path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify originals", scope=None, provider_config=config)
        budget = TaskBudget(task)
        preparing_note = Supplement(text="One initial detail: retain the receipt.")
        revoked_note = Supplement(text="Withdrawn detail 971: original draft only.", revoked_at="2026-11-18T00:00:00+00:00")
        task.supplements.extend([preparing_note, revoked_note])
        supplement = Supplement(text="Latest correction 937: stop editing; only report pending verification.")
        sends = []

        async def summary(messages, info):
            saved = ConversationRecord.model_validate_json(path.read_text(encoding="utf-8"))
            saved_task = saved.turns[-1].task
            assert saved_task is not None
            assert saved_task.supplements[0].selected and saved_task.supplements[0].applied_request is None
            assert revoked_note.text not in str(messages)
            entered.set()
            await release.wait()
            assert not supplement.selected and supplement.applied_request is None
            return ModelResponse(parts=[TextPart("Goal: verify 937; never invent evidence. Pending original verification.")],
                                 usage=RequestUsage(input_tokens=11000, output_tokens=25))

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            assert [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)].count(supplement.text) == 1
            yield "Pending verification only."

        path = tmp_path / "record.json"
        conversation = conversation_at(path)
        operation = asyncio.create_task(conversation.ask(task.goal, document=None, task=task, budget=budget,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.supplements.append(supplement)
        conversation.save()
        release.set()
        turn = await asyncio.wait_for(operation, timeout=5)
        assert turn.status == "completed", turn.error
        assert len(sends) == 1 and preparing_note.applied_request == supplement.applied_request == 2
        assert not revoked_note.selected and revoked_note.applied_request is None
        assert [attempt.purpose for attempt in turn.request_attempts] == ["compaction", "work"]
        reopened = Conversation.open(path)
        saved = ConversationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        assert [part.content for message in saved.messages for part in message.parts if isinstance(part, UserPromptPart)].count(supplement.text) == 1
        assert revoked_note.text not in str(saved.messages)
        assert revoked_note.text not in str(saved.prepared_contexts)
        assert turn.compactions[0].source[-1].message_index < len(saved.messages) - 2
        reopened_task = reopened.turns[-1].task
        assert reopened_task is not None
        assert reopened_task.supplements[-1].applied_request == 2
    asyncio.run(run())


@pytest.mark.parametrize("repeat_count", [1, 2])
def test_new_supplement_repeating_covered_history_is_preserved_as_a_new_original(tmp_path: Path, repeat_count):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        repeated = "只读，不要修改。"
        source = history()
        source[0].metadata = {"myrk_supplement_id": ["unrelated-untyped-metadata"]}
        source[2] = ModelRequest(parts=[UserPromptPart(repeated)])
        original = ModelMessagesTypeAdapter.dump_json(source)
        task = Task(goal="Verify originals", scope=None,
            provider_config=ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))
        budget = TaskBudget(task)
        first_supplement = Supplement(text=repeated)
        supplements = [first_supplement, *(Supplement(text=repeated, received_at=first_supplement.received_at) for _ in range(repeat_count - 1))]
        sends = []

        async def summary(messages, info):
            entered.set()
            await release.wait()
            return ModelResponse(parts=[TextPart("Earlier evidence reviewed; verification remains pending.")])

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            yield "Read-only verification remains pending."

        path = tmp_path / "record.json"
        conversation = conversation_at(path, source)
        model = FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})
        operation = asyncio.create_task(conversation.ask("Continue the latest verification.", document=None,
            task=task, budget=budget, model=model))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.supplements.extend(supplements)
        release.set()
        turn = await asyncio.wait_for(operation, timeout=5)
        assert turn.status == "completed", turn.error
        assert [part.content for message in sends[-1] for part in message.parts if isinstance(part, UserPromptPart)].count(repeated) == repeat_count
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        assert ModelMessagesTypeAdapter.dump_json(saved.messages[:len(source)]) == original
        assert [part.content for message in saved.messages for part in message.parts if isinstance(part, UserPromptPart)].count(repeated) == repeat_count + 1
        assert any(ref.message_index == 2 for ref in turn.compactions[0].covered)
        reopened = Conversation.open(path)
        continued = await reopened.ask("Continue read-only verification.", document=None, task=reopened.turns[-1].task, model=model)
        assert continued.status == "completed", continued.error
        assert len(sends) == 2
        assert [part.content for message in sends[-1] for part in message.parts if isinstance(part, UserPromptPart)].count(repeated) == repeat_count
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        assert [part.content for message in saved.messages for part in message.parts if isinstance(part, UserPromptPart)].count(repeated) == repeat_count + 1

    asyncio.run(run())


@pytest.mark.parametrize("late_success", [False, True])
def test_stop_during_summary_awaits_cleanup_and_keeps_late_supplement_without_dispatch(tmp_path: Path, late_success):
    async def run():
        entered, cleaned = asyncio.Event(), asyncio.Event()
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify originals", scope=None, provider_config=config)
        budget = TaskBudget(task)
        supplement = Supplement(text="Cancellation correction: preserve this original input 937.")
        sends = []

        async def summary(messages, info):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not late_success:
                    raise
                return ModelResponse(parts=[TextPart("Late summary must not commit.")],
                                     usage=RequestUsage(input_tokens=11000, output_tokens=25))
            finally:
                cleaned.set()
            return ModelResponse(parts=[TextPart("Late summary must not commit.")])

        async def respond(messages, info):
            sends.append(messages)
            yield "Must not dispatch."

        path = tmp_path / "record.json"
        conversation = conversation_at(path)
        operation = asyncio.create_task(conversation.ask(task.goal, document=None, task=task, budget=budget,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.supplements.append(supplement)
        budget.token.cancel()
        turn = await asyncio.wait_for(operation, timeout=5)
        assert cleaned.is_set() and not sends
        assert turn.status == "cancelled" and turn.compactions[0].status == "cancelled"
        assert [(attempt.purpose, attempt.status) for attempt in turn.request_attempts] == [("compaction", "completed" if late_success else "failed")]
        assert turn.request_attempts[0].input_tokens == (11000 if late_success else None)
        assert task.turns_used == 0 and task.total_requests == 1
        assert not supplement.selected and supplement.applied_request is None
        saved = ConversationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        assert saved.request_layouts["main"].compaction is None
        saved_task = saved.turns[-1].task
        assert saved_task is not None and saved_task.supplements == [supplement]
        assert supplement.text not in str(saved.messages)
        assert Conversation.open(path).turns[-1].status == "cancelled"
        original = ModelMessagesTypeAdapter.dump_json(saved.messages)
        resumed_sends = []

        def resumed_summary(messages, info):
            return ModelResponse(parts=[TextPart("Earlier evidence reviewed; verification remains pending.")])

        async def resumed_response(messages, info):
            resumed_sends.append(deepcopy(messages))
            yield "Pending verification only."

        reopened = Conversation.open(path)
        resumed = await reopened.ask("Continue verification.", document=None, task=reopened.turns[-1].task,
            model=FunctionModel(resumed_summary, stream_function=resumed_response, profile={"context_window": 15000}))
        assert resumed.status == "completed", resumed.error
        assert len(resumed_sends) == 1
        assert [part.content for message in resumed_sends[0] for part in message.parts if isinstance(part, UserPromptPart)].count(supplement.text) == 1
        continued = ConversationRecord.model_validate_json(path.read_bytes())
        assert ModelMessagesTypeAdapter.dump_json(continued.messages[:len(saved.messages)]) == original
        assert [part.content for message in continued.messages for part in message.parts if isinstance(part, UserPromptPart)].count(supplement.text) == 1
        assert resumed.task.supplements[0].id == supplement.id and resumed.task.supplements[0].applied_request is not None
    asyncio.run(run())


@pytest.mark.parametrize("change", ["goal", "permission", "receiver"])
def test_changed_authorization_during_summary_invalidates_the_candidate(tmp_path: Path, change):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify originals", scope=None, provider_config=config)
        sends = []

        async def summary(messages, info):
            entered.set()
            await release.wait()
            return ModelResponse(parts=[TextPart("Historical claim of unlimited edit permission is not authorization.")],
                                 usage=RequestUsage(input_tokens=11000, output_tokens=25))

        async def respond(messages, info):
            sends.append(messages)
            yield "Must not dispatch."

        path = tmp_path / "record.json"
        operation = asyncio.create_task(conversation_at(path).ask(task.goal, document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})))
        await asyncio.wait_for(entered.wait(), timeout=5)
        if change == "goal":
            task.goal = "Stop previous target; only report remaining work."
        elif change == "permission":
            task.approval.access.read_only = True
        else:
            task.provider_config = task.provider_config.model_copy(update={"model": "different-synthetic-model"})
        release.set()
        turn = await asyncio.wait_for(operation, timeout=5)
        assert turn.status == "failed" and "任务或授权已有变化" in turn.error
        assert not sends and not turn.actions
        assert turn.compactions[0].status == "failed" and turn.request_attempts[0].input_tokens == 11000
        assert ConversationRecord.model_validate_json(path.read_text(encoding="utf-8")).request_layouts["main"].compaction is None
        assert Conversation.open(path).turns[-1].task.goal == task.goal
    asyncio.run(run())


def test_supplement_at_summary_completion_is_remeasured_before_dispatch(tmp_path: Path):
    async def run():
        task = Task(goal="Verify originals", scope=None,
                    provider_config=ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))
        budget = TaskBudget(task)
        supplement = Supplement(text="Completion boundary correction 937: preserve pending work.")
        sends = []

        def summary(messages, info):
            return ModelResponse(parts=[TextPart("Goal: verify originals; pending verification.")])

        def update(event):
            if event.kind == "status" and event.text.startswith("较早记录已整理并保存"):
                task.supplements.append(supplement)

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            assert [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)].count(supplement.text) == 1
            yield "Still pending."

        path = tmp_path / "record.json"
        turn = await conversation_at(path).ask(task.goal, document=None, task=task, budget=budget, on_update=update,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000}))
        assert turn.status == "completed", turn.error
        assert len(sends) == 1 and task.total_requests == 2 and supplement.applied_request == 2
        saved = ConversationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        assert saved.prepared_contexts[-1].request_attempt == 2
        assert supplement.text in str(saved.prepared_contexts[-1].messages)
        assert [part.content for message in saved.messages for part in message.parts if isinstance(part, UserPromptPart)].count(supplement.text) == 1
    asyncio.run(run())


def test_legacy_shared_request_limit_includes_the_summary_without_resetting(tmp_path: Path):
    async def run():
        task = Task(goal="Verify originals", scope=None, request_policy="shared", request_limit=10, requests=9, total_requests=9,
                    provider_config=ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))
        summaries, sends = [], []

        def summary(messages, info):
            summaries.append(messages)
            return ModelResponse(parts=[TextPart("Goal: verify originals; pending verification.")])

        async def respond(messages, info):
            sends.append(messages)
            yield "Must not dispatch after consuming the last request."

        path = tmp_path / "record.json"
        turn = await conversation_at(path).ask(task.goal, document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000}))
        assert turn.status == "limited" and len(summaries) == 1 and not sends
        assert task.requests == task.total_requests == 10
        assert task.turns_used == 0
        assert turn.request_attempts[0].number == 10 and turn.request_attempts[0].purpose == "compaction"
        restored = Conversation.open(path).turns[-1].task
        assert restored.request_policy == "shared" and restored.request_limit == restored.requests == 10
    asyncio.run(run())


def test_no_remaining_work_turn_does_not_spend_a_summary_request(tmp_path: Path):
    async def run():
        task = Task(goal="Return unfinished work", scope=None, max_turns=1, turns_used=1,
                    provider_config=ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))
        sends = []

        def summary(messages, info):
            sends.append(messages)
            return ModelResponse(parts=[TextPart("Must not summarize.")])

        async def respond(messages, info):
            sends.append(messages)
            yield "Must not dispatch."

        turn = await conversation_at(tmp_path / "record.json").ask(task.goal, document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000}))
        assert turn.status == "limited" and not sends
        assert not turn.compactions and not turn.request_attempts
        assert task.turns_used == 1 and task.total_requests == 0
    asyncio.run(run())


@pytest.mark.parametrize("kind, expected_sends", [("http400", 1), ("connection", 3)])
def test_other_request_errors_do_not_start_window_recovery(tmp_path: Path, kind, expected_sends):
    async def run():
        task = Task(goal="Verify originals", scope=None,
                    provider_config=ProviderConfig(context_window_override=30000, output_token_reserve=1024, context_safety_margin=512))
        sends, summaries = [], []

        def summary(messages, info):
            summaries.append(messages)
            return ModelResponse(parts=[TextPart("Must not summarize.")])

        async def respond(messages, info):
            sends.append(messages)
            if kind == "http400":
                raise ModelHTTPError(400, "synthetic", {"error": "ordinary invalid parameter"})
            raise ModelConnectionError("synthetic", "ordinary connection failure")
            yield "unreachable"

        turn = await conversation_at(tmp_path / "record.json").ask(task.goal, document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 30000}))
        assert turn.status == "failed" and len(sends) == expected_sends
        assert not summaries and not turn.compactions
        assert task.turns_used == 1 and task.total_requests == expected_sends
    asyncio.run(run())


def test_repeated_window_error_does_not_replay_an_already_completed_action(tmp_path: Path):
    async def run():
        task = Task(goal="Inspect memory and report pending verification", scope=None,
                    provider_config=ProviderConfig(context_window_override=30000, output_token_reserve=1024, context_safety_margin=512))
        sends, summaries = [], []

        def summary(messages, info):
            summaries.append(messages)
            return ModelResponse(parts=[TextPart("Goal: verify 937. Memory read completed; original verification still pending.")])

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            if len(sends) == 1:
                yield {0: DeltaToolCall(name="memory", json_args='{"action":"list"}', tool_call_id="read-once")}
            else:
                raise ModelContextWindowExceededError("synthetic", "synthetic window exceeded")

        path = tmp_path / "record.json"
        turn = await conversation_at(path).ask(task.goal, document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 30000}))
        assert turn.status == "failed" and len(sends) == 3 and len(summaries) == 1
        assert task.turns_used == 2 and task.total_requests == 4
        assert len(turn.actions) == 1 and turn.actions[0].status == "completed"
        saved = ConversationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        assert len([part for message in saved.messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_call_id == "read-once"]) == 1
    asyncio.run(run())


def test_main_and_delegated_summary_keep_separate_histories_and_request_accounts(tmp_path: Path):
    async def run():
        (tmp_path / "notes.txt").write_text("Original evidence 937; still needs verification.", encoding="utf-8")
        scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify originals by delegation", scope=scope, provider_config=config, delegation_max_turns=5)
        work_sends = {"main": [], "child": []}
        summaries = []

        def summary(messages, info):
            summaries.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("Goal: verify original evidence 937. Reads completed, independent verification pending.")],
                                 usage=RequestUsage(input_tokens=8000, output_tokens=25))

        async def respond(messages, info):
            child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
            actor = "child" if child else "main"
            work_sends[actor].append(deepcopy(messages))
            results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            if child and not any(isinstance(part, CompactionPart) for message in messages for part in message.parts):
                yield "Read observation, original verification pending. " * 250
                yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id=f"child-read-{len(work_sends[actor])}")}
            elif child:
                yield "Read evidence; independent verification pending."
            elif not any(part.tool_name == "delegate_work" for part in results):
                yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "Read original evidence", "context": "Read notes.txt only", "evidence_requirements": "Keep originals and pending verification", "expected_files": ["notes.txt"]}), tool_call_id="delegate-once")}
            else:
                yield "Main retains its own remaining work."

        path = tmp_path / "record.json"
        turn = await conversation_at(path).ask(task.goal, document=None, scope=scope, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000}))
        assert turn.status == "completed", turn.error
        assert len(turn.compactions) == 1 and turn.compactions[0].status == "completed"
        work = turn.delegations[0]
        child = Conversation.open(Path(work.record_path))
        child_turn = child.turns[-1]
        assert child_turn.status == "completed", child_turn.error
        assert len(child_turn.compactions) == 1 and child_turn.compactions[0].status == "completed"
        assert work.max_turns == 5 and work.turns_used <= 5 and task.max_turns is None
        assert task.requests == 3 and task.turns_used == 2
        assert work.requests == len(work_sends["child"]) + 1
        assert task.total_requests == task.requests + work.requests
        assert all(attempt.actor == "delegation" and attempt.delegation_id == work.id for attempt in child_turn.request_attempts)
        assert all(attempt.actor == "main" and attempt.delegation_id is None for attempt in turn.request_attempts)
        assert turn.compactions[0].id != child_turn.compactions[0].id
        assert child_turn.task.provider_config.identity == task.provider_config.identity
        with pytest.raises(ValueError, match="子助理"):
            await child.ask("Do not promote to main", document=None, task=child_turn.task,
                            model=FunctionModel(summary, stream_function=respond))
    asyncio.run(run())


def test_summary_reservation_save_failure_does_not_charge_an_unsent_request(tmp_path: Path, monkeypatch):
    original_replace = Path.replace
    failed = False

    def replace_file(source, target):
        nonlocal failed
        if source.name == "record.json.tmp" and not failed:
            payload = json.loads(source.read_text(encoding="utf-8"))
            attempts = payload["turns"][-1]["request_attempts"] if payload["turns"] else []
            if attempts and attempts[-1]["purpose"] == "compaction":
                failed = True
                raise OSError("synthetic disk reservation failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_file)

    async def run():
        sends = []
        task = Task(goal="Verify originals", scope=None,
                    provider_config=ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))

        def summary(messages, info):
            sends.append(messages)
            return ModelResponse(parts=[TextPart("Must not dispatch.")])

        async def respond(messages, info):
            sends.append(messages)
            yield "Must not dispatch."

        path = tmp_path / "record.json"
        turn = await conversation_at(path).ask(task.goal, document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000}))
        assert failed and not sends and turn.status == "failed"
        assert task.requests == task.total_requests == 0
        assert not turn.request_attempts and not turn.compactions[0].request_numbers
        assert Conversation.open(path).turns[-1].task.total_requests == 0
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["blank", "nontext", "length", "refusal", "growth", "invalid_tool", "window", "timeout"])
def test_invalid_summary_stops_without_replacing_originals_or_summarizing_itself(tmp_path: Path, failure):
    async def run():
        summaries, sends = [], []
        source = history()
        original = ModelMessagesTypeAdapter.dump_json(source)
        task = Task(goal="Verify originals", scope=None,
                    provider_config=ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))

        def summary(messages, info):
            summaries.append(deepcopy(messages))
            assert not info.function_tools and not info.output_tools
            if failure == "window":
                raise ModelContextWindowExceededError("synthetic", "summary input window exceeded")
            if failure == "timeout":
                raise ModelTimeoutError("synthetic", "summary timed out")
            parts = [ToolCallPart("unavailable", {}, "invalid-summary-tool")] if failure == "invalid_tool" else [TextPart(
                " " if failure == "blank" else "Repeating historical records without reducing size. " * 6000 if failure == "growth" else "Summary did not finish reliably.")]
            if failure == "nontext":
                parts = [ThinkingPart("No summary text was returned.")]
            return ModelResponse(parts=parts, finish_reason="length" if failure == "length" else "content_filter" if failure == "refusal" else "stop",
                                 usage=RequestUsage(input_tokens=11000, output_tokens=25))

        async def respond(messages, info):
            sends.append(messages)
            yield "Must not dispatch."

        path = tmp_path / "record.json"
        turn = await conversation_at(path, source).ask(task.goal, document=None, task=task,
            model=FunctionModel(summary, stream_function=respond, profile={"context_window": 15000}))
        assert turn.status in {"failed", "limited"} and not sends
        assert len(summaries) == (3 if failure == "timeout" else 1)
        assert len(turn.compactions) == 1 and turn.compactions[0].status == "failed"
        assert task.total_requests == len(summaries) and task.turns_used == 0
        assert all(attempt.purpose == "compaction" and attempt.status != "sent" for attempt in turn.request_attempts)
        if failure not in {"window", "timeout"}:
            assert turn.request_attempts[0].input_tokens == 11000 and turn.request_attempts[0].output_tokens == 25
        saved = ConversationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        assert saved.request_layouts["main"].compaction is None
        assert ModelMessagesTypeAdapter.dump_json(saved.messages[:len(source)]) == original
        assert Conversation.open(path).turns[-1].task.total_requests == len(summaries)
    asyncio.run(run())


@pytest.mark.parametrize("finish_reason", [None, "CONTINUATION", "OTHER"])
def test_google_summary_requires_native_stop_even_when_text_and_usage_arrive(tmp_path: Path, monkeypatch, finish_reason):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    sends = []

    def respond(request):
        sends.append(request.url.path)
        candidate = {"content": {"role": "model", "parts": [{"text": "Goal: verify 937; original verification remains pending."}]}}
        if finish_reason is not None:
            candidate["finishReason"] = finish_reason
        data = {"candidates": [candidate], "usageMetadata": {"promptTokenCount": 11000, "candidatesTokenCount": 25, "thoughtsTokenCount": 0, "totalTokenCount": 11025}}
        if "streamGenerateContent" in request.url.path:
            candidate["finishReason"] = "STOP"
            return httpx2.Response(200, text=f"data: {json.dumps(data)}\n\n", headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, json=data)

    async def run():
        config = ProviderConfig(name="Controlled Google", protocol="google", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash",
                                context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify originals", scope=None, provider_config=config)
        path = tmp_path / "record.json"
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await conversation_at(path).ask(task.goal, document=None, task=task, model=model)
        assert turn.status == "failed", turn.error
        assert len(sends) == 1 and "streamGenerateContent" not in sends[0]
        assert len(turn.compactions) == 1 and turn.compactions[0].status == "failed"
        assert "Gemini 回应未完整结束" in turn.compactions[0].error
        assert turn.request_attempts[0].input_tokens == 11000 and turn.request_attempts[0].output_tokens == 25
        assert task.requests == 1 and task.turns_used == 0
        assert ConversationRecord.model_validate_json(path.read_text(encoding="utf-8")).request_layouts["main"].compaction is None
    asyncio.run(run())


def test_reopen_settles_an_interrupted_uncommitted_summary_without_writing_or_retrying(tmp_path: Path):
    task = Task(goal="Interrupted original verification", scope=None, requests=1, total_requests=1)
    candidate = CompactionRecord(task_id=task.id, authorization="original authority", provider=task.provider_config.identity,
                                 model=task.provider_config.model, layout_version=1, before_tokens=11000, input_budget_tokens=13000,
                                 measurement_source="local_text_estimate", request_numbers=[1])
    attempt = RequestAttempt(number=1, model=task.provider_config.model, provider=task.provider_config.base_url, purpose="compaction")
    turn = Turn(prompt=task.goal, answer="", task=task, compactions=[candidate], request_attempts=[attempt])
    path = tmp_path / "interrupted.json"
    original = ConversationRecord(version=9, messages=history(), turns=[turn]).model_dump_json()
    path.write_text(original, encoding="utf-8")

    reopened = Conversation.open(path)
    assert reopened.compaction_status()[0]["status"] == "failed"
    assert "中断" in reopened.compaction_status()[0]["error"]
    settled = reopened.turns[-1].request_attempts[0]
    assert settled.status == "failed" and settled.input_tokens is None and settled.output_tokens is None
    assert reopened.turns[-1].task.total_requests == 1
    assert reopened.recovery is not None
    assert path.read_text(encoding="utf-8") == original
    reopened.save()
    saved = Conversation.open(path)
    assert saved.compaction_status()[0]["status"] == "failed"
    assert saved.turns[-1].request_attempts[0].status == "failed"


def test_reopen_locks_only_supplements_with_exact_summary_source_evidence(tmp_path: Path):
    selected = Supplement(id="exact", text="The original note included in summary preparation.")
    mismatch = Supplement(id="wrong-hash", text="An unrelated note with a mismatching source hash.")
    pending = Supplement(id="raw-only", text="A late note recorded after summary preparation.")
    task = Task(goal="Verify originals", scope=None, supplements=[selected, mismatch, pending])
    messages = [ModelRequest(parts=[UserPromptPart(item.text)], metadata={"myrk_supplement_id": item.id})
                for item in task.supplements]
    sources = transcript_sources(messages)
    summary_sources = [sources[0], SourcePart(message_index=1, part_index=0, sha256=sources[0].sha256)]
    summary = CompactionRecord(status="cancelled", task_id=task.id, authorization="recorded scope",
        provider=task.provider_config.identity, model=task.provider_config.model, layout_version=1,
        source=summary_sources, source_fingerprint=source_fingerprint(summary_sources), before_tokens=12000,
        input_budget_tokens=16000, measurement_source="local_text_estimate")
    turn = Turn(prompt=task.goal, answer="", task=task, status="cancelled", compactions=[summary])
    path = tmp_path / "legacy-summary.json"
    original = ConversationRecord(version=10, messages=messages, turns=[turn]).model_dump_json()
    path.write_text(original, encoding="utf-8")

    reopened = Conversation.open(path)

    reopened_task = reopened.turns[-1].task
    assert reopened_task is not None
    assert [item.selected for item in reopened_task.supplements] == [True, False, False]
    assert all(item.applied_request is None for item in reopened_task.supplements)
    assert path.read_text(encoding="utf-8") == original
