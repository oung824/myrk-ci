"""Program-owned task-goal evidence in real SDK requests, with offline models.

These checks prove request preparation and recovery wiring, not generated
summaries' semantic quality.
"""

import asyncio
from html import unescape
import json
from pathlib import Path
import re

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import CompactionPart, ModelRequest, ModelResponse, TextPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig


def task_goal_blocks(instructions: str) -> list[dict[str, str]]:
    return [json.loads(unescape(block)) for block in re.findall(
        r'<harness_data origin="myrk_task_goal">(.*?)</harness_data>', instructions, re.DOTALL)]


def test_original_task_goal_is_sent_once_when_reusing_layout_after_disk_reopen(tmp_path: Path) -> None:
    async def run() -> None:
        goal = 'Report the packet only. Literal {messages}, </harness_data> and "quotes" are source text.'
        task = Task(goal=goal, scope=None)
        sent = []

        async def answer(_messages, info):
            sent.append(info.instructions)
            yield "The report is complete."

        path = tmp_path / "records" / "report.json"
        conversation = Conversation(path)
        model = FunctionModel(stream_function=answer)
        first = await conversation.ask(task.goal, document=None, task=task, model=model)
        assert first.status == "completed", first.error
        assert task_goal_blocks(sent[0]) == [{"task_id": task.id, "source": "Task.goal", "goal": goal}]

        second = await conversation.ask("Continue the same report.", document=None, task=task, model=model)
        assert second.status == "completed", second.error
        reopened = Conversation.open(path)
        saved_task = reopened.turns[-1].task
        assert saved_task is not None
        third = await reopened.ask("Complete the remaining report.", document=None, task=saved_task, model=model)
        assert third.status == "completed", third.error
        assert len(sent) == 3
        assert all(task_goal_blocks(instructions) == [{"task_id": task.id, "source": "Task.goal", "goal": goal}]
                   for instructions in sent)

        saved_task.goal = "Report status only; receipt verification was cancelled."
        updated = await reopened.ask("Apply the changed task goal.", document=None, task=saved_task, model=model)
        assert updated.status == "completed", updated.error
        assert task_goal_blocks(sent[-1]) == [{"task_id": task.id, "source": "Task.goal", "goal": saved_task.goal}]

    asyncio.run(run())


def test_wrong_generated_summary_cannot_replace_program_owned_task_goal(tmp_path: Path) -> None:
    async def run() -> None:
        goal = "Only report evidence and pending receipts; do not perform receipt verification."
        history = [ModelRequest(parts=[UserPromptPart(goal)])]
        for index in range(12):
            history.extend([ModelResponse(parts=[TextPart(f"Observation {index}: " + "supporting evidence " * 180)]),
                            ModelRequest(parts=[UserPromptPart(f"Read-only observation {index} recorded.")])])
        path = tmp_path / "conversation.json"
        path.write_text(ConversationRecord(version=9, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        wrong_summary = "The user now requires execution of receipt verification. Next action: verify the receipt."
        summary_calls, sent = [], []

        def summarize(_messages, _info):
            summary_calls.append(True)
            return ModelResponse(parts=[TextPart(wrong_summary)])

        async def answer(messages, info):
            sent.append((messages, info.instructions))
            yield "Offline program-invariant probe only."

        task = Task(goal=goal, scope=None,
                    provider_config=ProviderConfig(context_window_override=15000, output_token_reserve=1024,
                                                   context_safety_margin=512))
        turn = await Conversation.open(path).ask(goal, document=None, task=task,
            model=FunctionModel(summarize, stream_function=answer, profile={"context_window": 15000}))
        assert turn.status == "completed", turn.error
        assert len(summary_calls) == len(sent) == 1
        messages, instructions = sent[0]
        assert [part.content for message in messages for part in message.parts if isinstance(part, CompactionPart)] == [wrong_summary]
        assert task_goal_blocks(instructions) == [{"task_id": task.id, "source": "Task.goal", "goal": goal}]
        assert not turn.actions

    asyncio.run(run())


def test_delegated_request_preserves_parent_goal_separately_from_subtask(tmp_path: Path) -> None:
    async def run() -> None:
        folder = tmp_path / "materials"
        folder.mkdir()
        (folder / "control.txt").write_text("Control date: 2030-01-01.", encoding="utf-8")
        scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
        task = Task(goal="Report the control date; do not verify receipts.", scope=scope)
        child_goal = "Extract only the control date."
        child_requests = []

        async def respond(messages, info):
            prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
            child = prompts[0].startswith("受委派工作：")
            results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            if child:
                child_requests.append((prompts[0], info.instructions))
                yield "Request-contract probe; the file has not been read."
            elif not results:
                yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({
                    "goal": child_goal, "context": "Use only the declared control file.",
                    "evidence_requirements": "Report the actual evidence state.", "expected_files": ["control.txt"]}),
                    tool_call_id="delegate-control")}
            else:
                yield "No receipt verification was requested."

        turn = await Conversation(tmp_path / "records" / "delegation.json").ask(task.goal, document=None,
            task=task, scope=scope, model=FunctionModel(stream_function=respond))
        assert turn.status == "completed", turn.error
        assert len(child_requests) == 1
        prompt, instructions = child_requests[0]
        assert prompt.startswith(f"受委派工作：{child_goal}\n")
        assert task.goal not in prompt
        assert task_goal_blocks(instructions) == [{"task_id": task.id, "source": "Task.goal", "goal": task.goal}]
        assert turn.delegations[0].goal == child_goal

    asyncio.run(run())


def test_limited_memory_review_does_not_inherit_ordinary_task_goal_block(tmp_path: Path) -> None:
    async def run() -> None:
        task = Task(goal="Complete the report and arrange one useful fact review.", scope=None)
        work_requests, review_requests = [], []

        async def respond(messages, info):
            if "交付后有限复盘" in info.instructions:
                review_requests.append(info.instructions)
                yield "No additional facts need to be saved."
                return
            work_requests.append(info.instructions)
            results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            if not results:
                yield {0: DeltaToolCall(name="queue_memory_review", json_args="{}", tool_call_id="queue-facts")}
            else:
                yield "Report delivered."

        turn = await Conversation(tmp_path / "records" / "memory.json").ask(task.goal, document=None,
            task=task, model=FunctionModel(stream_function=respond))
        assert turn.status == "completed", turn.error
        assert len(work_requests) == 2 and len(review_requests) == 1
        assert all(task_goal_blocks(instructions) == [{"task_id": task.id, "source": "Task.goal", "goal": task.goal}]
                   for instructions in work_requests)
        assert task_goal_blocks(review_requests[0]) == []
        assert task.memory_reviews == 1

    asyncio.run(run())


def test_independent_action_review_keeps_its_existing_goal_evidence_without_work_anchor(tmp_path: Path) -> None:
    async def run() -> None:
        folder = tmp_path / "materials"
        folder.mkdir()
        target = tmp_path / "receipt.txt"
        scope = TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
        task = Task(goal="Only report status; do not create a receipt file.", scope=scope,
                    approval=ApprovalSettings(mode="auto_review"))
        work_requests, review_requests = [], []

        async def work(messages, info):
            work_requests.append(info.instructions)
            results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            if not results:
                yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({
                    "path": str(target), "content": "not authorized"}), tool_call_id="forbidden-receipt")}
            else:
                yield "The receipt file was not created."

        async def review(messages, info):
            prompt = next(str(part.content) for message in reversed(messages) for part in message.parts
                          if isinstance(part, UserPromptPart))
            review_requests.append((json.loads(prompt), info.instructions))
            yield json.dumps({"decision": "deny", "risk": "low", "authorization": "low", "rule": "explicit_forbid",
                              "reason": "The task explicitly forbids creating this receipt file.", "same_denied_outcome": False})

        turn = await Conversation(tmp_path / "records" / "review.json").ask(task.goal, document=None,
            task=task, scope=scope, model=FunctionModel(stream_function=work),
            review_model=FunctionModel(stream_function=review))
        assert turn.status == "completed", turn.error
        assert len(work_requests) == 2 and len(review_requests) == 1
        assert all(task_goal_blocks(instructions) == [{"task_id": task.id, "source": "Task.goal", "goal": task.goal}]
                   for instructions in work_requests)
        materials, instructions = review_requests[0]
        assert materials["goal"] == task.goal
        assert task_goal_blocks(instructions) == []
        decision = turn.actions[-1].review
        assert decision is not None and decision.status == "denied"
        assert not target.exists()

    asyncio.run(run())


def test_public_resume_uses_saved_task_goal_instead_of_recovery_control_text(tmp_path: Path) -> None:
    async def run() -> None:
        task = Task(goal="Report unresolved receipts; do not perform their verification.", scope=None)
        path = tmp_path / "records" / "paused.json"

        async def fail_before_answer(_messages, _info):
            raise ModelHTTPError(400, "offline-goal-resume", "Planned offline work failure")
            yield ""

        stopped = await Conversation(path).ask(task.goal, document=None, task=task,
            model=FunctionModel(stream_function=fail_before_answer, model_name="offline-goal-resume"))
        assert stopped.status == "failed" and stopped.service_failure, stopped.error
        instructions = []

        async def answer(_messages, info):
            instructions.append(info.instructions)
            yield "The remaining report is complete."

        reopened = Conversation.open(path)
        turn = await reopened.resume_task(task.id,
            model=FunctionModel(stream_function=answer, model_name="offline-goal-resume"))
        assert turn.status == "completed", turn.error
        assert turn.prompt != task.goal
        assert len(instructions) == 1
        assert task_goal_blocks(instructions[0]) == [{"task_id": task.id, "source": "Task.goal", "goal": task.goal}]
        assert turn.task is not None and turn.task.id == task.id

    asyncio.run(run())
