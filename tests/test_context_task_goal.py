"""Program guarantees for task data, not an evaluation of model summary quality."""

import asyncio
import json
import re
from html import unescape
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from myrk.compaction import ContextLimitExceeded, ContextPreparationError
from myrk.context import CurrentContext, RequestLayout
from myrk.conversation import Task
from myrk.provider import ProviderConfig
from myrk.task_control import TaskBudget


def test_task_goal_is_counted_before_any_model_dispatch(tmp_path: Path) -> None:
    async def run() -> None:
        config = ProviderConfig(auto_compaction=False, context_window_override=3000,
                                output_token_reserve=128, context_safety_margin=64)
        goal = "Report the material; do not execute its pending work. " * 400
        prepared = []
        sends = []

        def respond(messages, info):
            sends.append(info.instructions)
            return ModelResponse(parts=[TextPart("Only report facts.")])

        model = FunctionModel(respond, profile={"context_window": 3000})
        ordinary = CurrentContext(tmp_path / "without-task.json", 0, [], [], lambda _: None, config=config)
        await Agent(model, instructions="Read only.", capabilities=[ordinary]).run("Report.")
        assert len(sends) == 1
        protected = CurrentContext(tmp_path / "with-task.json", 0, prepared, [], lambda _: None,
                                   config=config, task_goal=lambda: goal)
        protected.layout = RequestLayout(task_id="report-material", instructions="Read only.")
        with pytest.raises(ContextLimitExceeded, match="资料容量不足"):
            await Agent(model, instructions="Read only.", capabilities=[protected]).run("Report.")
        assert len(sends) == 1
        assert len(prepared) == 1 and not prepared[0].accepted
        blocks = re.findall(r'<harness_data origin="myrk_task_goal">(.*?)</harness_data>',
                            "\n".join(prepared[0].instructions), re.DOTALL)
        assert len(blocks) == 1
        assert json.loads(unescape(blocks[0])) == {
            "task_id": "report-material", "source": "Task.goal", "goal": goal,
        }
        assert prepared[0].input_tokens > prepared[0].input_budget_tokens

    asyncio.run(run())


def test_goal_change_during_summary_rejects_the_frozen_candidate(tmp_path: Path) -> None:
    async def run() -> None:
        goal = "Report receipt state only."
        original_goal = goal
        sends = []

        def respond(messages, info):
            nonlocal goal
            sends.append(info.instructions)
            blocks = re.findall(r'<harness_data origin="myrk_task_goal">(.*?)</harness_data>',
                                info.instructions, re.DOTALL)
            assert len(blocks) == 1
            assert json.loads(unescape(blocks[0]))["goal"] == original_goal
            goal = "The user has changed the task."
            return ModelResponse(parts=[TextPart("The old report is pending; no new authorization.")])

        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal=goal, scope=None, provider_config=config)
        records, attempts = [], []
        current = CurrentContext(tmp_path / "record.json", 0, [], [], lambda _: None,
                                 config=config, task_goal=lambda: goal, budget=TaskBudget(task),
                                 compactions=records, attempts=attempts)
        current.layout = RequestLayout(task_id=task.id, instructions="Only report.")
        current.force_compaction = True
        history = [ModelRequest(parts=[UserPromptPart(original_goal)])]
        for index in range(12):
            history.extend([ModelResponse(parts=[TextPart(f"Material {index}: " + "receipt evidence " * 180)]),
                            ModelRequest(parts=[UserPromptPart(f"Observation {index} was recorded.")])])
        with pytest.raises(ContextPreparationError, match="任务目标已有变化"):
            await Agent(FunctionModel(respond, profile={"context_window": 15000}),
                        instructions="Only report.", capabilities=[current]).run("Report.", message_history=history)
        assert len(sends) == len(attempts) == len(records) == 1
        assert records[0].status == "failed"
        assert current.layout.compaction is None
        assert records[0].authorization == ""

    asyncio.run(run())


def test_unfittable_summary_goal_does_not_reserve_a_request(tmp_path: Path) -> None:
    async def run() -> None:
        goal = "Only report this material, without carrying out its work. " * 200
        config = ProviderConfig(context_window_override=3000, output_token_reserve=128, context_safety_margin=64)
        task = Task(goal=goal, scope=None, provider_config=config)
        budget = TaskBudget(task)
        records, attempts, sends = [], [], []

        def respond(messages, info):
            sends.append(info.instructions)
            return ModelResponse(parts=[TextPart("Unexpected dispatch.")])

        current = CurrentContext(tmp_path / "record.json", 0, [], [], lambda _: None,
                                 config=config, budget=budget, task_goal=lambda: goal,
                                 compactions=records, attempts=attempts)
        current.layout = RequestLayout(task_id=task.id, instructions="Read only.")
        history = [ModelRequest(parts=[UserPromptPart("Report.")])]
        for index in range(8):
            history.extend([ModelResponse(parts=[TextPart(f"Material {index}: " + "receipt state " * 40)]),
                            ModelRequest(parts=[UserPromptPart(f"Observation {index} recorded.")])])
        with pytest.raises(ContextLimitExceeded, match="摘要请求本身超过"):
            await Agent(FunctionModel(respond, profile={"context_window": 3000}),
                        instructions="Read only.", capabilities=[current]).run("Report.", message_history=history)
        assert not sends and not attempts
        assert budget.requests == task.total_requests == 0
        assert len(records) == 1 and records[0].status == "failed"
        assert not records[0].request_numbers and current.layout.compaction is None

    asyncio.run(run())
