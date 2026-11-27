"""An explicitly started saved task retains its original DeepSeek setting."""

import asyncio
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from myrk.conversation import Conversation, QueuedTask, Task
from tests.support.agent_acceptance.core import CaseContext
from tests.support.agent_acceptance.formal import provider_config
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


def test_public_saved_deepseek_queue_keeps_legacy_off_with_new_chat_default_high(
    acceptance_public_root: Path, request: pytest.FixtureRequest,
) -> None:
    acceptance_public_root.mkdir(parents=True)
    context = CaseContext(acceptance_public_root, "deepseek", uuid4().hex, "desktop.saved-deepseek-off")
    config = provider_config("deepseek").model_copy(update={"output_token_reserve": 4096})
    old_task = Task(goal="Run the saved synthetic OFF task.", scope=None, provider_config=config, legacy_thinking=False)
    saved_path = context.root / "records" / "saved-queue.json"
    saved = Conversation(saved_path)
    saved.queue.append(QueuedTask(task=old_task))
    saved.save()
    fresh_prompt = "Run a new synthetic default-high task."

    async def run() -> None:
        replies = [Reply(text="New chat done."), Reply(text="Saved task done.")]
        async with ServiceChannel(context, "desktop-service", replies) as channel:
            await channel.send({"action": "configure", "key": "synthetic-offline-only", "provider_config": config.model_dump(mode="json"), "thinking": True})
            assert (await channel.receive("configured"))["thinking"] is True
            await channel.send({"action": "new"})
            fresh = await channel.receive("conversation")
            assert fresh["path"] != str(saved_path) and fresh["queue"] == fresh["turns"] == []
            await channel.send({"action": "get_model_controls", "path": fresh["path"]})
            controls = await channel.receive("model_controls", path=fresh["path"])
            assert controls["error"] is None and controls["selection"]["reasoning"] == "high"
            await channel.send({"action": "send", "path": fresh["path"], "prompt": fresh_prompt})
            new = await channel.receive("started", path=fresh["path"])
            assert new["task"]["reasoning"] == "high"
            new_finished = await channel.receive("finished", task_id=new["task_id"], path=fresh["path"])
            assert new_finished["saved"] and new_finished["turn"]["status"] == "completed", new_finished

            await channel.send({"action": "open", "path": str(saved_path)})
            opened = await channel.receive("conversation", path=str(saved_path))
            assert not opened["running"] and not opened["turns"]
            assert [item["task"] for item in opened["queue"]] == [old_task.model_dump(mode="json")]
            await channel.send({"action": "start_next", "path": str(saved_path)})
            original = await channel.receive("started", task_id=old_task.id, path=str(saved_path))
            assert original["task"]["reasoning"] is None and original["task"]["legacy_thinking"] is False
            old_finished = await channel.receive("finished", task_id=old_task.id, path=str(saved_path))
            assert old_finished["saved"] and old_finished["turn"]["status"] == "completed", old_finished

        assert channel.process.returncode == 0
        shutil.rmtree(channel.root / "environment")
        exchange = json.loads((channel.root / "exchange.json").read_bytes())
        wire, sdk = exchange["exchanges"], exchange["sdk_inputs"]
        assert len(wire) == len(sdk) == 2
        assert [row["sdk_input_id"] for row in wire] == [row["sdk_input_id"] for row in sdk]
        assert all(row["transport_provenance"] == "fixed_fixture" for row in wire)
        assert fresh_prompt in wire[0]["request_utf8"] and old_task.goal not in wire[0]["request_utf8"]
        assert old_task.goal in wire[1]["request_utf8"] and fresh_prompt not in wire[1]["request_utf8"]
        new_body, old_body = [json.loads(row["request_utf8"]) for row in wire]
        assert [body["model"] for body in (new_body, old_body)] == ["deepseek-flash"] * 2
        assert [body["max_tokens"] for body in (new_body, old_body)] == [4096, 4096]
        assert new_body["reasoning_effort"] == "high"
        assert old_body["reasoning_effort"] == "none" and old_body.get("thinking", {}).get("type") != "enabled"
        assert {attempt["task_id"] for attempt in exchange["product_attempts"]} == {new["task_id"], old_task.id}
        assert Conversation.open(saved_path).queue == []

    (context.root / "pytest-node.json").write_text(json.dumps({"nodeid": request.node.nodeid}), encoding="utf-8")
    asyncio.run(run())
