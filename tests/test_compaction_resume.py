"""Recovery controls preserve a valid summary without hiding new work material."""

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import CompactionPart, ModelRequest, ModelResponse, TextPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from myrk.compaction import ContextPreparationError, part_hash
from myrk.context import CurrentContext, RequestLayout
from myrk.conversation import Conversation, ConversationRecord, Supplement, Task
from myrk.provider import ProviderConfig


MODEL_NAME = "synthetic-compaction-recovery"


async def paused_compacted_conversation(path: Path, *, summary_repetitions: int = 2800) -> Conversation:
    """Save a real completed compaction, then stop ordinary work at the model boundary."""
    history = [ModelRequest(parts=[UserPromptPart("Preserve the recorded goal and pending verification.")])]
    for index in range(12):
        history.extend([ModelResponse(parts=[TextPart(f"Evidence {index}: " + "historical information " * 180)]),
                        ModelRequest(parts=[UserPromptPart(f"Continue item {index}.")])])
    path.write_text(ConversationRecord(version=9, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
    task = Task(goal="Preserve the recorded goal and pending verification.", scope=None,
                provider_config=ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))

    def summary(messages, info):
        return ModelResponse(parts=[TextPart("Synthetic factual record; verification remains pending. " + "资料" * summary_repetitions)])

    async def stop_work(messages, info):
        raise ModelHTTPError(400, MODEL_NAME, {"error": "Synthetic pause before ordinary work output"})
        yield ""

    conversation = Conversation.open(path)
    turn = await conversation.ask("Latest requirement: preserve the original evidence.", document=None, task=task,
        model=FunctionModel(summary, stream_function=stop_work, model_name=MODEL_NAME, profile={"context_window": 15000}))
    assert turn.status == "failed" and turn.service_failure, turn.error
    assert len(turn.compactions) == 1 and turn.compactions[0].status == "completed", turn.error
    assert turn.compactions[0].target_reached is False
    assert [attempt.purpose for attempt in turn.request_attempts] == ["compaction", "work"]
    return Conversation.open(path)


def stopping_model(calls: list[str]) -> FunctionModel:
    """Keep compaction and ordinary model boundaries observable without HTTP."""
    def summary(messages, info):
        calls.append("compaction")
        return ModelResponse(parts=[TextPart("Unexpected repeated summary.")])

    async def stop_work(messages, info):
        calls.append("work")
        raise ModelHTTPError(400, MODEL_NAME, {"error": "Synthetic pause"})
        yield ""

    return FunctionModel(summary, stream_function=stop_work, model_name=MODEL_NAME, profile={"context_window": 15000})


def test_resume_reuses_an_above_trigger_committed_summary(tmp_path: Path):
    async def run():
        conversation = await paused_compacted_conversation(tmp_path / "record.json")
        task = conversation.turns[-1].task
        assert task is not None
        previous = conversation.compaction_status()
        sends, summaries = [], []

        def summary(messages, info):
            summaries.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("An unexpected repeated summary.")])

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            yield "Verification remains pending."

        turn = await conversation.resume_task(task.id, model=FunctionModel(summary, stream_function=respond,
            model_name=MODEL_NAME, profile={"context_window": 15000}))
        assert not turn.compactions, "A recovery control alone must not re-enter compaction"
        assert not summaries, "A recovery control alone must not summarize the same history again"
        assert turn.status == "completed", turn.error
        assert [attempt.purpose for attempt in turn.request_attempts] == ["work"]
        assert len(sends) == 1 and any(isinstance(part, CompactionPart) for message in sends[0] for part in message.parts)
        assert conversation.compaction_status() == previous
        restored = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        prepared = restored.prepared_contexts[-1]
        assert prepared.input_tokens is not None and prepared.input_budget_tokens is not None
        assert prepared.input_budget_tokens * task.provider_config.compaction_trigger < prepared.input_tokens <= prepared.input_budget_tokens
    asyncio.run(run())


def test_saved_unsent_recovery_controls_survive_two_reopens(tmp_path: Path):
    class LocalRestart(BaseException):
        """Stop after persisted preparation, before the model boundary."""

    async def run():
        path = tmp_path / "record.json"
        conversation = await paused_compacted_conversation(path)
        task = conversation.turns[-1].task
        assert task is not None
        original = conversation.compaction_status()
        sends, summaries = [], []

        def summary(messages, info):
            summaries.append(1)
            return ModelResponse(parts=[TextPart("Unexpected repeated summary.")])

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            yield "Verification remains pending."

        model = FunctionModel(summary, stream_function=respond, model_name=MODEL_NAME, profile={"context_window": 15000})

        def interrupt(update):
            if update.kind == "status" and update.text == "正在等待回答服务":
                raise LocalRestart()

        for _ in range(2):
            with pytest.raises(LocalRestart):
                await conversation.resume_task(task.id, model=model, on_update=interrupt)
            conversation = Conversation.open(path)
            assert conversation.recovery is not None and conversation.recovery.can_resume
            assert conversation.compaction_status() == original
            assert not conversation.turns[-1].compactions
            assert not sends and not summaries
        turn = await conversation.resume_task(task.id, model=model)
        assert turn.status == "completed", turn.error
        assert not turn.compactions and not summaries and len(sends) == 1
        restored = ConversationRecord.model_validate_json(path.read_bytes())
        recovery_messages = [message for message in restored.messages if isinstance(message, ModelRequest)
                             and "myrk_recovery_control" in (message.metadata or {})]
        assert len(recovery_messages) == 3
        assert [message.parts[0].content for message in recovery_messages] == [turn.prompt] * 3
        assert sum(isinstance(part, UserPromptPart) and part.content == turn.prompt for message in sends[0] for part in message.parts) == 3
    asyncio.run(run())


def test_user_entering_the_same_recovery_text_remains_new_work_material(tmp_path: Path):
    async def run():
        conversation = await paused_compacted_conversation(tmp_path / "record.json")
        task = conversation.turns[-1].task
        assert task is not None
        calls, inputs = [], []

        def summary(messages, info):
            calls.append("compaction")
            return ModelResponse(parts=[TextPart("Unexpected repeated summary.")])

        async def stop_work(messages, info):
            calls.append("work")
            inputs.append(deepcopy(messages))
            raise ModelHTTPError(400, MODEL_NAME, {"error": "Synthetic pause"})
            yield ""

        model = FunctionModel(summary, stream_function=stop_work, model_name=MODEL_NAME, profile={"context_window": 15000})
        resumed = await conversation.resume_task(task.id, model=model)
        assert resumed.service_failure and not resumed.compactions and calls == ["work"]
        conversation = Conversation.open(conversation.path)
        task = conversation.prepare_resume(task.id)
        previous = conversation.compaction_status()
        original = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        calls.clear()
        inputs.clear()
        user_turn = await conversation.ask(resumed.prompt, document=None, task=task, model=model)
        assert user_turn.service_failure and not user_turn.compactions and calls == ["work"], user_turn.error
        assert sum(isinstance(part, UserPromptPart) and part.content == resumed.prompt
                   for message in inputs[0] for part in message.parts) == 2
        assert conversation.compaction_status() == previous
        saved = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        assert saved.messages[:len(original.messages)] == original.messages
        assert saved.request_layouts["main"].compaction == original.request_layouts["main"].compaction
        assert saved.request_layouts["main"].version == original.request_layouts["main"].version
        identical = [message for message in saved.messages if isinstance(message, ModelRequest)
                     and any(isinstance(part, UserPromptPart) and part.content == resumed.prompt for part in message.parts)]
        assert len(identical) == 2
        assert "myrk_recovery_control" in (identical[0].metadata or {})
        assert "myrk_recovery_control" not in (identical[1].metadata or {})
    asyncio.run(run())


def test_new_supplement_remains_visible_when_no_older_history_can_be_compacted(tmp_path: Path):
    async def run():
        conversation = await paused_compacted_conversation(tmp_path / "record.json")
        task = conversation.turns[-1].task
        assert task is not None
        supplement = Supplement(text="New correction: verify the second source before proceeding.")
        task.supplements.append(supplement)
        previous = conversation.compaction_status()
        original = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        work = []

        def summary(messages, info):
            return ModelResponse(parts=[TextPart("Unexpected repeated summary.")])

        async def respond(messages, info):
            work.append(deepcopy(messages))
            yield "The new correction remains pending verification."

        model = FunctionModel(summary, stream_function=respond, model_name=MODEL_NAME, profile={"context_window": 15000})
        turn = await conversation.resume_task(task.id, model=model)
        assert not turn.compactions and turn.status == "completed", turn.error
        assert len(work) == 1 and supplement.text in str(work[0])
        assert conversation.compaction_status() == previous
        saved = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        assert saved.messages[:len(original.messages)] == original.messages
        assert saved.request_layouts["main"].compaction == original.request_layouts["main"].compaction
        assert saved.request_layouts["main"].version == original.request_layouts["main"].version
        assert any(isinstance(message, ModelRequest) and (message.metadata or {}).get("myrk_supplement_id") == supplement.id
                   for message in saved.messages)
    asyncio.run(run())


def test_only_the_marked_part_in_a_merged_saved_request_is_a_control(tmp_path: Path):
    async def run():
        path = tmp_path / "record.json"
        conversation = await paused_compacted_conversation(path)
        task = conversation.turns[-1].task
        assert task is not None
        work = []

        def summary(messages, info):
            return ModelResponse(parts=[TextPart("Unexpected repeated summary.")])

        async def stop_work(messages, info):
            work.append(deepcopy(messages))
            raise ModelHTTPError(400, MODEL_NAME, {"error": "Synthetic pause"})
            yield ""

        model = FunctionModel(summary, stream_function=stop_work, model_name=MODEL_NAME, profile={"context_window": 15000})
        resumed = await conversation.resume_task(task.id, model=model)
        assert resumed.service_failure and not resumed.compactions
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        merged = saved.messages[-1]
        assert isinstance(merged, ModelRequest) and "myrk_recovery_control" in (merged.metadata or {})
        merged.parts.append(UserPromptPart("New user material in the same saved request container."))
        path.write_text(saved.model_dump_json(), encoding="utf-8")
        conversation = Conversation.open(path)
        previous = conversation.compaction_status()
        original = ConversationRecord.model_validate_json(path.read_bytes())
        work.clear()
        turn = await conversation.resume_task(task.id, model=model)
        assert not turn.compactions and turn.service_failure, turn.error
        assert len(work) == 1 and "New user material in the same saved request container." in str(work[0])
        assert conversation.compaction_status() == previous
        restored = ConversationRecord.model_validate_json(path.read_bytes())
        assert restored.messages[:len(original.messages)] == original.messages
        assert restored.request_layouts["main"].compaction == original.request_layouts["main"].compaction
        assert restored.request_layouts["main"].version == original.request_layouts["main"].version
        marked = restored.messages[len(original.messages) - 1]
        assert isinstance(marked, ModelRequest) and marked.metadata == merged.metadata
        assert marked.parts == merged.parts
    asyncio.run(run())


def test_a_new_tool_result_after_resume_remains_work_material(tmp_path: Path):
    async def run():
        conversation = await paused_compacted_conversation(tmp_path / "record.json")
        task = conversation.turns[-1].task
        assert task is not None
        previous = conversation.compaction_status()
        original = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        work = []

        def summary(messages, info):
            return ModelResponse(parts=[TextPart("Unexpected repeated summary.")])

        async def read_notes(messages, info):
            work.append(deepcopy(messages))
            if any(isinstance(part, ToolReturnPart) and part.tool_name == "memory" for message in messages for part in message.parts):
                yield "The current memory result was received."
            else:
                yield {0: DeltaToolCall(name="memory", json_args='{"action":"list"}')}

        turn = await conversation.resume_task(task.id, model=FunctionModel(summary, stream_function=read_notes,
            model_name=MODEL_NAME, profile={"context_window": 15000}))
        assert len(work) == 2
        assert not turn.compactions and turn.status == "completed", turn.error
        assert conversation.compaction_status() == previous
        saved = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        assert saved.messages[:len(original.messages)] == original.messages
        assert saved.request_layouts["main"].compaction == original.request_layouts["main"].compaction
        assert saved.request_layouts["main"].version == original.request_layouts["main"].version
        memory_results = [part for message in saved.messages for part in message.parts
                          if isinstance(part, ToolReturnPart) and part.tool_name == "memory"]
        assert len(memory_results) == 1
        assert memory_results[0] in [part for message in work[1] for part in message.parts]
        assert [attempt.purpose for attempt in turn.request_attempts] == ["work", "work"]
    asyncio.run(run())


def test_only_recovery_controls_can_grow_past_the_hard_budget_without_dispatch(tmp_path: Path):
    class LocalRestart(BaseException):
        """Stop once the prepared request has been saved."""

    async def run():
        path = tmp_path / "record.json"
        conversation = await paused_compacted_conversation(path, summary_repetitions=2470)
        task = conversation.turns[-1].task
        assert task is not None
        task.provider_config = task.provider_config.model_copy(update={"input_token_limit_override": 11700})
        calls = []

        def summary(messages, info):
            calls.append("compaction")
            return ModelResponse(parts=[TextPart("Unexpected repeated summary.")])

        async def respond(messages, info):
            calls.append("work")
            yield "Unexpected dispatch."

        def interrupt(update):
            if update.kind == "status" and update.text == "正在等待回答服务":
                raise LocalRestart()

        model = FunctionModel(summary, stream_function=respond, model_name=MODEL_NAME, profile={"context_window": 15000})
        with pytest.raises(LocalRestart):
            await conversation.resume_task(task.id, model=model, on_update=interrupt)
        conversation = Conversation.open(path)
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        assert saved.prepared_contexts[-1].input_budget_tokens == 11188
        assert saved.prepared_contexts[-1].accepted
        assert (saved.prepared_contexts[-1].input_budget_tokens * task.provider_config.compaction_trigger
                < saved.prepared_contexts[-1].input_tokens <= saved.prepared_contexts[-1].input_budget_tokens)
        turn = await conversation.resume_task(task.id, model=model)
        assert turn.status == "limited" and "没有新增可压缩原文" in turn.error, (
            turn.error, [(context.input_tokens, context.input_budget_tokens)
                         for context in ConversationRecord.model_validate_json(path.read_bytes()).prepared_contexts])
        assert not turn.compactions and not turn.request_attempts and not calls
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["schema", "fingerprint", "position", "missing_task"])
def test_malformed_recovery_origin_stops_before_model_dispatch(tmp_path: Path, damage: str):
    async def run():
        path = tmp_path / "record.json"
        conversation = await paused_compacted_conversation(path)
        task = conversation.turns[-1].task
        assert task is not None
        calls = []
        model = stopping_model(calls)
        first = await conversation.resume_task(task.id, model=model)
        assert first.service_failure and calls == ["work"]
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        message = saved.messages[-1]
        assert isinstance(message, ModelRequest) and message.metadata is not None
        if damage == "schema":
            message.metadata["myrk_recovery_control"] = None
        elif damage == "fingerprint":
            message.metadata["myrk_recovery_control"]["source"]["sha256"] = "0" * 64
        elif damage == "missing_task":
            assert all(turn.task is None or turn.task.id != "missing-recovery-task" for turn in saved.turns)
            message.metadata["myrk_recovery_control"]["task_id"] = "missing-recovery-task"
        else:
            message.metadata["myrk_recovery_control"]["source"]["message_index"] = 0
        path.write_text(saved.model_dump_json(), encoding="utf-8")
        conversation = Conversation.open(path)
        calls.clear()
        turn = await conversation.resume_task(task.id, model=model)
        assert turn.status == "failed" and "恢复控制提示" in turn.error, turn.error
        assert not calls and not turn.compactions and not turn.request_attempts
    asyncio.run(run())


@pytest.mark.parametrize("save_stage", ["origin", "dispatch"])
def test_failed_recovery_origin_save_stops_before_model_dispatch(tmp_path: Path, monkeypatch, save_stage: str):
    async def run():
        path = tmp_path / "record.json"
        conversation = await paused_compacted_conversation(path)
        task = conversation.turns[-1].task
        assert task is not None
        original = conversation.compaction_status()
        replace = Path.replace
        failures, calls = [], []

        def fail_origin_save_once(source, target):
            if source == path.with_suffix(".json.tmp") and not failures:
                pending = ConversationRecord.model_validate_json(source.read_bytes())
                if any(isinstance(message, ModelRequest) and "myrk_recovery_control" in (message.metadata or {})
                       for message in pending.messages):
                    dispatch_recorded = any(attempt.purpose == "work" and attempt.status == "sent"
                                            for attempt in pending.turns[-1].request_attempts)
                    if save_stage == "origin" or dispatch_recorded:
                        failures.append(1)
                        raise OSError("Injected recovery-origin atomic save failure")
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_origin_save_once)
        turn = await conversation.resume_task(task.id, model=stopping_model(calls))
        assert failures == [1] and turn.status == "failed", turn.error
        assert not calls and not turn.compactions
        if save_stage == "origin":
            assert not turn.request_attempts
        assert Conversation.open(path).compaction_status() == original
    asyncio.run(run())


@pytest.mark.parametrize("version", [8, 9])
@pytest.mark.parametrize("has_origin", [False, True])
def test_reopening_old_records_never_guesses_missing_prompt_origins(tmp_path: Path, version: int, has_origin: bool):
    async def run():
        path = tmp_path / "record.json"
        conversation = await paused_compacted_conversation(path)
        task = conversation.turns[-1].task
        assert task is not None
        calls = []
        model = stopping_model(calls)
        first = await conversation.resume_task(task.id, model=model)
        assert first.service_failure and calls == ["work"]
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        saved.version = version
        message = saved.messages[-1]
        assert isinstance(message, ModelRequest) and message.metadata is not None
        if not has_origin:
            del message.metadata["myrk_recovery_control"]
        path.write_text(saved.model_dump_json(), encoding="utf-8")
        conversation = Conversation.open(path)
        previous = conversation.compaction_status()
        original = ConversationRecord.model_validate_json(path.read_bytes())
        calls.clear()
        inputs = []

        async def capture_work(messages, info):
            calls.append("work")
            inputs.append(deepcopy(messages))
            raise ModelHTTPError(400, MODEL_NAME, {"error": "Synthetic pause"})
            yield ""

        turn = await conversation.resume_task(task.id, model=FunctionModel(stream_function=capture_work,
            model_name=MODEL_NAME, profile={"context_window": 15000}))
        assert turn.service_failure and calls == ["work"] and not turn.compactions
        assert conversation.compaction_status() == previous
        restored = ConversationRecord.model_validate_json(path.read_bytes())
        assert restored.version == 10
        assert restored.messages[:len(original.messages)] == original.messages
        assert restored.request_layouts["main"].compaction == original.request_layouts["main"].compaction
        assert restored.request_layouts["main"].version == original.request_layouts["main"].version
        saved_origin = restored.messages[len(original.messages) - 1]
        assert isinstance(saved_origin, ModelRequest)
        assert ("myrk_recovery_control" in (saved_origin.metadata or {})) is has_origin
        prior_prompts = [part.content for part in message.parts if isinstance(part, UserPromptPart)]
        sent_prompts = [part.content for outgoing in inputs[0] for part in outgoing.parts if isinstance(part, UserPromptPart)]
        assert prior_prompts and all(prompt in sent_prompts for prompt in prior_prompts)
    asyncio.run(run())


@pytest.mark.parametrize("change", ["model_window", "output_reserve", "input_limit"])
def test_resume_reestimates_the_selected_model_and_budget_before_dispatch(tmp_path: Path, change: str):
    async def run():
        conversation = await paused_compacted_conversation(tmp_path / "record.json")
        task = conversation.turns[-1].task
        assert task is not None
        if change == "model_window":
            task.provider_config = task.provider_config.model_copy(update={"context_window_override": None})
        elif change == "output_reserve":
            task.provider_config = task.provider_config.model_copy(update={"output_token_reserve": 4096})
        else:
            task.provider_config = task.provider_config.model_copy(update={"input_token_limit_override": 10000})
        calls = []

        def summary(messages, info):
            calls.append("compaction")
            return ModelResponse(parts=[TextPart("Unexpected repeated summary.")])

        async def respond(messages, info):
            calls.append("work")
            yield "Unexpected dispatch."

        model = FunctionModel(summary, stream_function=respond, model_name="smaller-synthetic-model" if change == "model_window" else MODEL_NAME,
            profile={"context_window": 12000 if change == "model_window" else 15000})
        turn = await conversation.resume_task(task.id, model=model)
        assert turn.status == "limited" and "没有新增可压缩原文" in turn.error, turn.error
        assert not calls and not turn.compactions and not turn.request_attempts
    asyncio.run(run())


def test_an_existing_other_tasks_recovery_marker_remains_valid_history(tmp_path: Path):
    async def run():
        path = tmp_path / "record.json"
        conversation = Conversation(path)
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        original = Task(goal="Original task.", scope=None, provider_config=config)
        stopped = await conversation.ask(original.goal, document=None, task=original, model=stopping_model([]))
        assert stopped.service_failure
        calls = []

        async def respond(messages, info):
            calls.append(deepcopy(messages))
            yield "Recorded task completed."

        model = FunctionModel(stream_function=respond, model_name=MODEL_NAME, profile={"context_window": 15000})
        resumed = await conversation.resume_task(original.id, model=model)
        assert resumed.status == "completed", resumed.error
        conversation = Conversation.open(path)
        following = Task(goal="Another task.", scope=None, provider_config=config)
        turn = await conversation.ask(following.goal, document=None, task=following, model=model)
        assert turn.status == "completed", turn.error
        assert len(calls) == 2 and not turn.compactions
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        origins = [message.metadata["myrk_recovery_control"]["task_id"] for message in saved.messages
                   if isinstance(message, ModelRequest) and message.metadata and "myrk_recovery_control" in message.metadata]
        assert origins == [original.id] and original.id != following.id
    asyncio.run(run())


@pytest.mark.parametrize("layout_task", ["original-task", "another-task", None])
def test_standalone_context_only_accepts_origins_for_a_known_task(tmp_path: Path, layout_task: str | None):
    async def run():
        prompt = UserPromptPart("Saved program recovery control.")
        history = [ModelRequest(parts=[prompt], metadata={"myrk_recovery_control": {
            "task_id": "original-task", "source": {"message_index": 0, "part_index": 0, "sha256": part_hash(prompt)}}})]
        current = CurrentContext(tmp_path / "record.json", 0, [], [], lambda _: None,
            config=ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))
        if layout_task is not None:
            current.layout = RequestLayout(task_id=layout_task, instructions="Read only.")
        calls = []

        def respond(messages, info):
            calls.append(1)
            return ModelResponse(parts=[TextPart("Accepted known task history.")])

        agent = Agent(FunctionModel(respond, profile={"context_window": 15000}), capabilities=[current])
        if layout_task == "original-task":
            result = await agent.run("New material.", message_history=history)
            assert result.output == "Accepted known task history." and calls == [1]
        else:
            with pytest.raises(ContextPreparationError, match="关联的任务"):
                await agent.run("New material.", message_history=history)
            assert not calls
    asyncio.run(run())
