"""Desktop continuation commands preserve an already usable compacted view."""

import asyncio
import json
from pathlib import Path
import sys

import httpx2
import pytest
from pydantic_ai import models

from myrk.conversation import Conversation, ConversationRecord
from myrk.desktop_service import serve
from myrk.provider import deepseek_model
from tests.test_compaction_resume import paused_compacted_conversation
from tests.test_desktop_service import connection


@pytest.mark.parametrize("entry", ["resume", "increase_limit", "approve_receiver"])
def test_desktop_continuation_reuses_the_existing_above_trigger_summary(tmp_path: Path, entry: str):
    path = tmp_path / "records" / "paused.json"
    path.parent.mkdir()
    original = asyncio.run(paused_compacted_conversation(path))
    task = original.turns[-1].task
    assert task is not None
    summary = original.compaction_status()
    used = task.requests
    if entry == "increase_limit":
        task.request_limit = used
        original.save()
    with connection(path.parent, "tests.test_recovery_control_desktop") as channel:
        try:
            channel.send({"action": "open", "path": str(path)})
            assert channel.receive()["event"] == "conversation"
            channel.send({"action": "configure", "key": "synthetic-desktop-recovery-only",
                "provider_config": task.provider_config.model_dump(mode="json")})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "verify_recovery", "path": str(path), "task_id": task.id})
            assert channel.receive()["recovery"]["can_resume"] is True
            command = {"action": "resume_task", "path": str(path), "task_id": task.id}
            if entry == "increase_limit":
                command = {"action": "continue_task", "path": str(path), "task_id": task.id,
                    "request_limit": used + 3}
            elif entry == "approve_receiver":
                target = task.provider_config.model_copy(update={"name": "Synthetic approved receiver",
                    "base_url": "https://receiver.example/v1", "protocol": "openai-chat",
                    "model": "synthetic-recovery-model"})
                channel.send({"action": "preview_provider", "path": str(path), "task_id": task.id,
                    "provider_config": target.model_dump(mode="json")})
                preview = channel.receive()
                assert preview["event"] == "provider_preview" and preview["change"]["compatible"] is True
                channel.send({"action": "configure", "key": "synthetic-approved-receiver-only",
                    "provider_config": target.model_dump(mode="json")})
                assert channel.receive()["event"] == "configured"
                command = {"action": "decide_provider", "path": str(path),
                    "change_id": preview["change"]["id"], "approved": True}
            channel.send(command)
            while (finished := channel.receive())["event"] != "finished":
                assert finished["event"] != "error", finished
            turn = finished["turn"]
            assert turn["status"] == "completed", turn["error"]
            assert [attempt["purpose"] for attempt in turn["request_attempts"]] == ["work"]
            assert turn["task"]["id"] == task.id and turn["task"]["requests"] == used + 1
            if entry == "increase_limit":
                assert turn["task"]["request_limit"] == used + 3
            elif entry == "approve_receiver":
                assert turn["task"]["provider_config"]["base_url"] == "https://receiver.example/v1"
            stored = Conversation.open(path)
            assert stored.turns[-1].compactions == []
            assert stored.compaction_status() == summary
            record = ConversationRecord.model_validate_json(path.read_bytes())
            prepared = record.prepared_contexts[-1]
            assert prepared.input_tokens is not None and prepared.input_budget_tokens is not None
            assert prepared.input_budget_tokens * task.provider_config.compaction_trigger < prepared.input_tokens <= prepared.input_budget_tokens
        finally:
            channel.send({"action": "shutdown"})
            assert channel.receive()["event"] == "closed"
            assert channel.process.wait(timeout=5) == 0


async def offline_service(directory: Path):
    def respond(request):
        payload = json.loads(request.content)
        if not payload.get("tools"):
            return httpx2.Response(400, json={"error": "A repeated summary is not a work continuation"})
        chunk = {"id": "synthetic-desktop-recovery", "object": "chat.completion.chunk", "created": 1,
            "model": payload["model"], "choices": [{"index": 0,
                "delta": {"role": "assistant", "content": "Continued the original pending verification."},
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
        return httpx2.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as client:
        await serve(directory, deepseek_model, http_client=client)


if __name__ == "__main__":
    import httpx

    async def refuse_real_http(_transport, _request):
        raise RuntimeError("The desktop recovery fixture cannot dispatch real HTTP")

    for module in {httpx, httpx2}:
        module.AsyncHTTPTransport.handle_async_request = refuse_real_http
    models.ALLOW_MODEL_REQUESTS = True
    asyncio.run(offline_service(Path(sys.argv[1])))
