import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import CompactionPart, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from myrk.context import ContextLimitExceeded, CurrentContext, RequestLayout
from myrk.conversation import Task
from myrk.conversation import Conversation, ConversationRecord
from myrk.provider import ProviderConfig
from myrk.task_control import TaskBudget


def long_history():
    history = [ModelRequest(parts=[UserPromptPart("Original goal: verify 937; never invent evidence.")])]
    for index in range(12):
        history.extend([ModelResponse(parts=[TextPart(f"Evidence {index}: " + "historical information " * 180)]),
                        ModelRequest(parts=[UserPromptPart(f"Continue item {index}.")])])
    return history


def test_long_history_compacts_without_changing_originals(tmp_path: Path):
    async def run():
        source = long_history()
        original = ModelMessagesTypeAdapter.dump_json(source)
        sends = []

        def respond(messages, info):
            sends.append(deepcopy(messages))
            if "compaction" in str(info.instructions).lower():
                assert not info.function_tools
                return ModelResponse(parts=[TextPart("Goal: verify 937; never invent evidence. Pending: verify originals. Failed: file absent.")],
                                     usage=RequestUsage(input_tokens=9000, output_tokens=30))
            assert any(isinstance(part, CompactionPart) for message in messages for part in message.parts)
            assert "Latest correction: read only." in str(messages)
            return ModelResponse(parts=[TextPart("Continue verified work.")])

        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify originals", scope=None, provider_config=config)
        budget = TaskBudget(task)
        attempts = []
        saved = []
        layout = RequestLayout(task_id=task.id, instructions="Read only.")
        current = CurrentContext(tmp_path / "record.json", 0, [], [], lambda _: None,
                                 config=config, budget=budget, attempts=attempts, persist=lambda: saved.append(layout.model_dump_json()))
        current.layout = layout
        model = FunctionModel(respond, profile={"context_window": 15000})
        result = await Agent(model, instructions="Read only.", capabilities=[current]).run("Latest correction: read only.", message_history=source)
        assert result.output == "Continue verified work."
        assert ModelMessagesTypeAdapter.dump_json(source) == original
        assert len(attempts) == budget.requests == 1 and budget.turns_used == 0
        assert attempts[0].purpose == "compaction" and attempts[0].input_tokens == 9000
        assert layout.compaction is not None and layout.compaction.status == "completed"
        assert layout.compaction.covered and layout.compaction.retained
        assert saved and layout.changes[-1].reasons == ["capacity_compression"]
        assert len(sends) == 2
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["hard-budget", "forced-recovery", "changed-without-summary"])
def test_no_compressible_history_does_not_bypass_capacity_or_candidate_failures(tmp_path: Path, monkeypatch, reason):
    async def run():
        requests = []

        def respond(messages, info):
            requests.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("Unexpected request.")])

        if reason == "changed-without-summary":
            async def changed_without_summary(*_args, **_kwargs):
                return []
            monkeypatch.setattr("myrk.compaction.compact_now", changed_without_summary)
        config = ProviderConfig(context_window_override=6000, output_token_reserve=1024, context_safety_margin=512,
                                compaction_trigger=.3)
        task = Task(goal="Preserve the complete latest requirement.", scope=None, provider_config=config)
        attempts, records = [], []
        current = CurrentContext(tmp_path / "record.json", 0, [], [], lambda _: None, config=config,
                                 budget=TaskBudget(task), attempts=attempts, compactions=records)
        current.layout = RequestLayout(task_id=task.id, instructions="Preserve the complete latest requirement.")
        current.force_compaction = reason == "forced-recovery"
        prompt = "近期原话。" * (1000 if reason == "hard-budget" else 350)
        with pytest.raises(ContextLimitExceeded):
            await Agent(FunctionModel(respond, profile={"context_window": 6000}), capabilities=[current]).run(prompt)
        assert not requests and not attempts
        assert len(records) == 1 and records[0].status == "failed"
        assert not records[0].request_numbers
        assert current.layout.compaction is None and current.layout.version == 1
        if reason == "hard-budget":
            assert records[0].before_tokens > records[0].input_budget_tokens
        else:
            assert records[0].before_tokens <= records[0].input_budget_tokens
    asyncio.run(run())


def test_conversation_reopens_committed_summary_and_keeps_exact_original_log(tmp_path: Path):
    async def run():
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify originals; never invent evidence.", scope=None, provider_config=config)
        original_history = long_history()
        original = original_history[1].parts[0].content
        normal_sends = []
        summaries = []

        async def respond(messages, info):
            normal_sends.append(deepcopy(messages))
            assert any(isinstance(part, CompactionPart) for message in messages for part in message.parts)
            yield "Still pending verification."

        def summary(messages, info):
            summaries.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("Goal: verify originals; never invent evidence. Pending verification. Sources remain in original conversation.")],
                                 usage=RequestUsage(input_tokens=15000, output_tokens=30))

        model = FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})
        path = tmp_path / "record.json"
        path.write_text(ConversationRecord(version=8, messages=original_history, turns=[]).model_dump_json(), encoding="utf-8")
        conversation = Conversation.open(path)
        second = await conversation.ask("Latest correction: read only.", document=None, task=task, model=model)
        assert second.status == "completed", second.error
        assert len(summaries) == 1
        assert second.compactions[0].status == "completed"
        assert second.request_attempts[0].purpose == "compaction"
        assert second.input_tokens == 15050 and second.output_tokens >= 30
        assert task.turns_used == 1 and task.total_requests == 2
        reopened = Conversation.open(path)
        restored_task = reopened.turns[-1].task
        third = await reopened.ask("Continue using saved summary.", document=None, task=restored_task, model=model)
        assert third.status == "completed", third.error
        assert len(summaries) == 1
        saved = path.read_text(encoding="utf-8")
        assert original in saved
        assert reopened.compaction_status()[0]["status"] == "completed"
        assert "Evidence 0: historical information" in reopened.read_compaction_source(second.compactions[0].id, 1, 0)
    asyncio.run(run())


def test_disabling_auto_compaction_reopens_full_history_and_stops_before_dispatch(tmp_path: Path):
    async def run():
        path = tmp_path / "record.json"
        history = long_history()
        path.write_text(ConversationRecord(version=8, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        task = Task(goal="Verify originals; never invent evidence.", scope=None, provider_config=ProviderConfig(
            context_window_override=15000, output_token_reserve=1024, context_safety_margin=512))
        summaries, sends = [], []

        def summary(messages, info):
            summaries.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("Goal: verify originals; never invent evidence. Pending original verification.")])

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            yield "Still pending verification."

        model = FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})
        first = await Conversation.open(path).ask("Latest correction: read only.", document=None, task=task, model=model)
        assert first.status == "completed" and len(first.compactions) == 1, first.error
        reopened = Conversation.open(path)
        saved_compactions = reopened.compaction_status()
        resumed_task = reopened.turns[-1].task
        resumed_task.provider_config = resumed_task.provider_config.model_copy(update={"auto_compaction": False})
        stopped = await reopened.ask("Continue with the full original history.", document=None, task=resumed_task, model=model)
        assert stopped.status == "limited" and "资料容量不足" in stopped.error
        assert len(summaries) == len(sends) == 1
        assert not stopped.compactions and not stopped.request_attempts
        restored = Conversation.open(path)
        assert restored.compaction_status() == saved_compactions
        assert "Evidence 0: historical information" in restored.read_compaction_source(first.compactions[0].id, 1, 0)
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        assert ModelMessagesTypeAdapter.dump_json(saved.messages[:len(history)]) == ModelMessagesTypeAdapter.dump_json(history)
        assert saved.request_layouts["main"].compaction.summary == first.compactions[0].summary
    asyncio.run(run())


def test_disabling_auto_compaction_shrinks_previews_against_the_full_history(tmp_path: Path):
    async def run():
        path = tmp_path / "record.json"
        history = [ModelRequest(parts=[UserPromptPart("Goal: verify evidence.")])]
        for index in range(8):
            history.extend([ModelResponse(parts=[TextPart("Historical detail " * 180)]),
                            ModelRequest(parts=[UserPromptPart(f"Continue {index}.")])])
        original = "原始证据不得改写。" * 1800
        history.extend([ModelResponse(parts=[ToolCallPart("read_source", {}, "retained-source")]),
                        ModelRequest(parts=[ToolReturnPart("read_source", original, "retained-source")])])
        path.write_text(ConversationRecord(version=8, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        task = Task(goal="Verify evidence.", scope=None, provider_config=ProviderConfig(context_window_override=20000,
            output_token_reserve=1024, context_safety_margin=512))
        sends, summaries = [], []

        def summary(messages, info):
            summaries.append(1)
            return ModelResponse(parts=[TextPart("Goal: verify evidence; source remains pending verification.")])

        async def respond(messages, info):
            sends.append(deepcopy(messages))
            yield "Still pending."

        model = FunctionModel(summary, stream_function=respond, profile={"context_window": 20000})
        first = await Conversation.open(path).ask("Latest requirement: read only.", document=None, task=task, model=model)
        assert first.status == "completed" and len(summaries) == 1, first.error
        reopened = Conversation.open(path)
        resumed_task = reopened.turns[-1].task
        resumed_task.provider_config = resumed_task.provider_config.model_copy(update={"auto_compaction": False, "context_window_override": 16000})
        second = await reopened.ask("Continue with the full original history.", document=None, task=resumed_task, model=model)
        assert second.status == "completed", second.error
        assert len(summaries) == 1 and len(sends) == 2
        latest = sends[-1]
        assert not any(isinstance(part, CompactionPart) for message in latest for part in message.parts)
        assert "各1000个字符" in next(str(part.content) for message in latest for part in message.parts if isinstance(part, ToolReturnPart))
        assert all(f"Continue {index}." in str(latest) for index in range(8))
        restored = Conversation.open(path)
        assert restored.compaction_status()[0]["id"] == first.compactions[0].id
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        assert original in [part.content for message in saved.messages for part in message.parts if isinstance(part, ToolReturnPart)]
    asyncio.run(run())


def test_reopening_rejects_a_corrupt_retained_source_mapping_before_dispatch(tmp_path: Path):
    async def run():
        path = tmp_path / "record.json"
        path.write_text(ConversationRecord(version=8, messages=long_history(), turns=[]).model_dump_json(), encoding="utf-8")
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Preserve original evidence.", scope=None, provider_config=config)
        sends = []

        def summary(messages, info):
            return ModelResponse(parts=[TextPart("Preserve original evidence. Pending verification.")])

        async def respond(messages, info):
            sends.append(1)
            yield "Still pending."

        model = FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})
        first = await Conversation.open(path).ask("Latest requirement.", document=None, task=task, model=model)
        assert first.status == "completed", first.error
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["request_layouts"]["main"]["compaction"]["retained"][0]["part_index"] = 999
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        reopened = Conversation.open(path)
        failed = await reopened.ask("Continue.", document=None, task=reopened.turns[-1].task, model=model)
        assert failed.status == "failed" and "来源" in failed.error
        assert len(sends) == 1
    asyncio.run(run())


def test_later_compaction_carries_previous_summary_and_exact_original_sources(tmp_path: Path):
    async def run():
        path = tmp_path / "record.json"
        history = long_history()
        path.write_text(ConversationRecord(version=8, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify 937 without inventing evidence.", scope=None, provider_config=config)
        summary_inputs = []

        def summary(messages, info):
            summary_inputs.append(str(messages))
            if len(summary_inputs) > 1:
                assert "Never invent evidence; failed missing file; pending verification." in str(messages)
            return ModelResponse(parts=[TextPart("Never invent evidence; failed missing file; pending verification.")])

        async def respond(messages, info):
            assert "Latest correction: read only." in str(messages)
            yield "New verifiable evidence. " * 600

        model = FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})
        conversation = Conversation.open(path)
        turns = []
        for index in range(8):
            turn = await conversation.ask(f"Latest correction: read only. Continue {index}.", document=None, task=task, model=model)
            assert turn.status == "completed", turn.error
            turns.append(turn)
        records = [record for turn in turns for record in turn.compactions if record.status == "completed"]
        assert len(records) >= 2 and records[1].replaced_id == records[0].id, [(p["input_tokens"], p["measurement_source"]) for p in json.loads(path.read_text(encoding="utf-8"))["prepared_contexts"]]
        assert records[0].source[0] in records[1].source
        assert records[0].source[0] in [*records[1].covered, *records[1].retained]
        assert len(summary_inputs) == len(records)
        assert task.turns_used == 8 and task.total_requests == 8 + len(records)
        assert "Original goal: verify 937; never invent evidence." in conversation.read_compaction_source(records[1].id, 0, 0)
    asyncio.run(run())


def test_unknown_model_stops_until_configured_and_independent_input_limit_is_respected(tmp_path: Path):
    async def run():
        sends = []
        async def respond(messages, info):
            sends.append(info.model_settings)
            yield "Configured input accepted."
        model = FunctionModel(stream_function=respond, profile={})
        task = Task(goal="Use the configured model only.", scope=None)
        path = tmp_path / "record.json"
        conversation = Conversation(path)
        first = await conversation.ask(task.goal, document=None, task=task, model=model)
        assert first.status == "limited" and "容量未知" in first.error and not sends
        task.provider_config = task.provider_config.model_copy(update={"context_window_override": 20000,
            "input_token_limit_override": 5000, "output_token_reserve": 1024, "context_safety_margin": 512})
        second = await conversation.ask("Continue with the configured limit.", document=None, task=task, model=model)
        assert second.status == "completed", second.error
        prepared = json.loads(path.read_text(encoding="utf-8"))["prepared_contexts"][-1]
        assert prepared["input_budget_tokens"] == 4488 and prepared["measurement_source"] == "sdk_rendered_local_text_estimate"
        assert sends == [{"max_tokens": 1024}]
        assert task.total_requests == task.turns_used == 1
    asyncio.run(run())


def test_failed_atomic_summary_save_preserves_original_log_and_does_not_send_work_request(tmp_path: Path, monkeypatch):
    async def run():
        path = tmp_path / "record.json"
        history = long_history()
        original_json = ModelMessagesTypeAdapter.dump_json(history)
        path.write_text(ConversationRecord(version=8, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Preserve original evidence.", scope=None, provider_config=config)
        work_sends = []

        def summary(messages, info):
            return ModelResponse(parts=[TextPart("Goal: preserve original evidence; verification pending.")],
                                 usage=RequestUsage(input_tokens=9000, output_tokens=30))

        async def respond(messages, info):
            work_sends.append(1)
            yield "Should not continue after failed persistence."

        replace = Path.replace
        failures = []
        def fail_commit_once(source, target):
            if source.name == "record.json.tmp" and not failures:
                pending = json.loads(source.read_text(encoding="utf-8"))
                if pending["request_layouts"]["main"]["compaction"] is not None:
                    failures.append(1)
                    raise OSError("Injected atomic replacement failure")
            return replace(source, target)
        monkeypatch.setattr(Path, "replace", fail_commit_once)
        model = FunctionModel(summary, stream_function=respond, profile={"context_window": 15000})
        turn = await Conversation.open(path).ask("Latest requirement.", document=None, task=task, model=model)
        assert turn.status == "failed" and failures and not work_sends
        assert len(turn.request_attempts) == 1 and turn.request_attempts[0].purpose == "compaction"
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        assert ModelMessagesTypeAdapter.dump_json(saved.messages[:len(history)]) == original_json
        assert saved.request_layouts["main"].compaction is None
        assert saved.turns[-1].compactions[0].status == "failed"
    asyncio.run(run())


def test_actual_model_output_limit_is_reserved_before_the_request(tmp_path: Path):
    async def run():
        prepared = []
        config = ProviderConfig(context_window_override=20000, output_token_reserve=1024, context_safety_margin=512)
        def respond(messages, info):
            assert info.model_settings["max_tokens"] == 6000
            return ModelResponse(parts=[TextPart("Accepted configured output reservation.")])
        current = CurrentContext(tmp_path / "record.json", 0, prepared, [], lambda _: None, config=config)
        model = FunctionModel(respond, profile={"context_window": 20000}, settings={"max_tokens": 6000})
        result = await Agent(model, capabilities=[current]).run("Small request.")
        assert result.output == "Accepted configured output reservation."
        assert prepared[-1].input_budget_tokens == 13488
    asyncio.run(run())


def test_committed_summary_preserves_preview_until_its_effective_view_needs_shrinking(tmp_path: Path):
    async def run():
        path = tmp_path / "record.json"
        history = [ModelRequest(parts=[UserPromptPart("Goal: verify evidence.")])]
        for index in range(8):
            history.extend([ModelResponse(parts=[TextPart("Historical detail " * 180)]),
                            ModelRequest(parts=[UserPromptPart(f"Continue {index}.")])])
        original = "原始证据不得改写。" * 1800
        history.extend([ModelResponse(parts=[ToolCallPart("read_source", {}, "retained-source")]),
                        ModelRequest(parts=[ToolReturnPart("read_source", original, "retained-source")])])
        path.write_text(ConversationRecord(version=8, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        task = Task(goal="Verify evidence.", scope=None, provider_config=ProviderConfig(context_window_override=20000,
            output_token_reserve=1024, context_safety_margin=512))
        previews = []
        summaries = []
        def summary(messages, info):
            summaries.append(1)
            return ModelResponse(parts=[TextPart("Goal: verify evidence; source remains pending verification.")])
        async def respond(messages, info):
            previews.append(next(str(part.content) for message in messages for part in message.parts if isinstance(part, ToolReturnPart)))
            yield "Still pending."
        model = FunctionModel(summary, stream_function=respond, profile={"context_window": 20000})
        conversation = Conversation.open(path)
        first = await conversation.ask("Latest requirement: read only.", document=None, task=task, model=model)
        assert first.status == "completed" and len(summaries) == 1, first.error
        assert "各4000个字符" in previews[-1]
        task.provider_config = task.provider_config.model_copy(update={"context_window_override": 16000})
        second = await conversation.ask("Continue.", document=None, task=task, model=model)
        assert second.status == "completed", second.error
        assert "各4000个字符" in previews[-1] and len(summaries) == 1
        task.provider_config = task.provider_config.model_copy(update={"context_window_override": 13000})
        latest = "新增限制必须保留。" * 400
        third = await conversation.ask(latest, document=None, task=task, model=model)
        assert third.status == "completed", third.error
        assert "各1000个字符" in previews[-1] and len(summaries) == 1
        reopened = Conversation.open(path)
        fourth = await reopened.ask("Continue with saved preview.", document=None, task=reopened.turns[-1].task, model=model)
        assert fourth.status == "completed", fourth.error
        assert "各1000个字符" in previews[-1] and len(summaries) == 1
        saved = ConversationRecord.model_validate_json(path.read_bytes())
        assert original in [part.content for message in saved.messages for part in message.parts if isinstance(part, ToolReturnPart)]
    asyncio.run(run())
