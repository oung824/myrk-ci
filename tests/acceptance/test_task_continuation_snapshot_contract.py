"""Prove public task identity, finite spent capacity and saved turn snapshots."""
import asyncio
import json


def test_same_task_references_preserve_saved_usage_caps_and_prior_snapshots(acceptance_public_root):
    from tests.support.agent_acceptance.replay import OfflineSession, Reply
    from myrk.conversation import Conversation, ConversationRecord, Task
    async def run():
        session = OfflineSession("deepseek", [Reply(text="First exact answer."), Reply(text="Second exact answer.")])
        conversation = Conversation(acceptance_public_root / "conversation.json")
        task = Task(goal="Continue this exact finite task without replay or resetting its spent capacity.", scope=None,
            request_limit=2, provider_config=session.config)
        async with session.model() as model:
            first = await conversation.ask(task.goal, document=None, task=task, model=model)
            assert first.status == "completed" and first.task is not None
            original = first.model_dump(mode="json")
            second = await conversation.ask("Continue the same task once.", document=None, task=task, model=model)
            assert second.status == "completed" and second.task is not None
            assert second.task.id == first.task.id == task.id
            assert second.task.requests == second.task.total_requests == 2 and second.task.request_limit == 2
            assert first.model_dump(mode="json") == original
            reopened = Conversation.open(conversation.path)
            stale = reopened.turns[0].task
            assert stale is not None and stale.requests == 1
            exhausted = await reopened.ask("An old reference cannot grant a third send.", document=None, task=stale, model=model)
            assert exhausted.status == "limited" and exhausted.task is not None
            assert exhausted.task.id == task.id and exhausted.task.requests == exhausted.task.total_requests == exhausted.task.request_limit == 2
            assert exhausted.request_attempts == [] and len(session.exchanges) == len(session.sdk_inputs) == 2
        record = ConversationRecord.model_validate_json(conversation.path.read_bytes())
        assert [turn.task.requests for turn in record.turns] == [1, 2, 2]
        assert record.turns[0].model_dump(mode="json") == original
        exchange = session.save(acceptance_public_root / "exchange.json", attempts=[attempt.model_dump(mode="json") for turn in record.turns for attempt in turn.request_attempts],
            run_id="public-cap-snapshot-control", case_id="task.same-reference", task_id=task.id)
        assert len(json.loads(exchange.read_bytes())["exchanges"]) == 2
    asyncio.run(run())
