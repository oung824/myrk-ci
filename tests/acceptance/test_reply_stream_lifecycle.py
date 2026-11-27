"""Observe fixed terminal HTTP response close through the public Mock boundary."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import httpx2
import pytest

from tests.support.agent_acceptance.recorded_response import response_evidence
from tests.support.agent_acceptance.replay import OfflineSession, Reply


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
@pytest.mark.parametrize("progress", ["unstarted", "partial", "complete"])
def test_native_mock_response_close_records_only_actual_body_progress(protocol, progress):
    async def observe():
        session = OfflineSession(protocol, [Reply(text="Synthetic public close evidence.")])
        url = (session.config.base_url + "/v1beta/models/" + session.config.model + ":streamGenerateContent"
            if protocol == "google" else session.config.base_url + "/chat/completions")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(session.respond)) as client:
            async with client.stream("POST", url, json={"stream": True}) as response:
                if progress == "complete":
                    await response.aread()
                elif progress == "partial":
                    stream = response.aiter_raw()
                    assert await anext(stream)
                    await stream.aclose()
        assert len(session.exchanges) == 1
        return session.exchanges[0]

    record = asyncio.run(observe())
    original = deepcopy(record)
    observed = response_evidence(record)
    assert record == original
    if progress == "complete":
        assert "response_body_state" not in record and record["received_base64"]
        assert observed["status"] == "decoded" and observed["events"]
    else:
        assert record["response_body_state"] == "closed_before_eof"
        assert observed["status"] == "incomplete" and observed["text"] is None
        if progress == "unstarted":
            assert record["received_utf8"] == "" and "received_base64" not in record
            assert observed["raw_source"] == "no_body_bytes_received"
        else:
            assert record["received_base64"] and observed["raw_sha256"]


def test_unobserved_old_empty_capture_is_still_rejected_without_backfilling_it():
    originals = Path(__file__).parent / "materials/response-close-4ef"
    index = json.loads((originals / "index.json").read_bytes())
    with ZipFile(originals / "originals.zip") as archive:
        raw = archive.read(index["member"])
    assert len(raw) == index["bytes"] and sha256(raw).hexdigest() == index["sha256"]
    exchange = json.loads(raw)
    record = exchange["exchanges"][1]
    assert record["status"] == 200 and record["received_utf8"] == ""
    assert "response_body_state" not in record and "received_base64" not in record and "error" not in record
    before = deepcopy(record)
    with pytest.raises(ValueError, match="no captured transport bytes"):
        response_evidence(record)
    assert record == before


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_complete_native_sdk_reply_keeps_decoded_response_evidence(tmp_path, protocol):
    from myrk.conversation import Conversation, Task, TaskScope

    async def observe():
        session = OfflineSession(protocol, [Reply(text="Synthetic complete native response 7319.")])
        conversation = Conversation(tmp_path / "conversation.json")
        provider = "openai-compatible" if protocol == "openai-chat" else protocol
        task = Task(goal="Return the declared response.", provider_config=session.config, request_limit=1,
            scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider=provider, recipients=[provider]))
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model)
        assert turn.status == "completed" and turn.answer == "Synthetic complete native response 7319."
        assert len(session.sdk_inputs) == len(session.exchanges) == 1
        return session.exchanges[0]

    record = asyncio.run(observe())
    assert "response_body_state" not in record and record["received_base64"]
    evidence = response_evidence(record)
    assert evidence["status"] == "decoded" and evidence["events"]
