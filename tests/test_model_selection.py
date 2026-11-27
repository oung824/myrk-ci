from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import pytest

from myrk.conversation import Conversation, QueuedTask, Task
from myrk.model_controls import ModelSelection
from myrk.provider import ProviderConfig
from tests.test_desktop_service import Connection, connection


def receive_event(channel: Connection, kind: str) -> dict:
    for _ in range(30):
        event = channel.receive(timeout=15)
        if event["event"] == kind:
            return event
        assert event["event"] != "error", event
    raise AssertionError(f"No {kind} event received")


def test_conversation_remembers_its_model_and_reasoning_after_reopening(tmp_path: Path) -> None:
    conversation = Conversation(tmp_path / "selection.json")
    selection = ModelSelection(provider_config=ProviderConfig(), reasoning="high")
    conversation.set_model_selection(selection)

    reopened = Conversation.open(conversation.path)
    assert reopened.model_selection == selection
    assert reopened.turns == ()


def test_model_controls_remember_selection_per_chat_without_resetting_work_preferences(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only", "delegation_max_turns": 17})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "get_model_controls", "request_id": "first"})
        first = channel.receive()
        assert first["event"] == "model_controls" and first["request_id"] == "first"
        first_path = first["path"]
        assert first["selection"]["reasoning"] == "high"
        channel.send({"action": "select_model", "model": "deepseek-flash", "reasoning": "high", "request_id": "selected"})
        selected = channel.receive()
        assert selected["event"] == "model_controls" and selected["selection"]["reasoning"] == "high"
        channel.send({"action": "new"})
        second = channel.receive()
        assert second["event"] == "conversation" and second["path"] != first_path
        channel.send({"action": "get_model_controls"})
        assert channel.receive()["selection"]["reasoning"] == "high"
        channel.send({"action": "open", "path": first_path})
        assert channel.receive()["event"] == "conversation"
        channel.send({"action": "get_model_controls"})
        assert channel.receive()["selection"]["reasoning"] == "high"
        channel.send({"action": "snapshot"})
        assert channel.receive()["delegation_max_turns"] == 17

    assert Conversation.open(Path(first_path)).model_selection.reasoning == "high"


def test_queued_work_keeps_its_reasoning_when_another_chat_changes_settings(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "get_model_controls"})
        controls = channel.receive()
        first_path = controls["path"]
        selection = {**controls["selection"], "reasoning": "low"}
        channel.send({"action": "send", "prompt": "停止，等我补充", "model_selection": selection})
        started = receive_event(channel, "started")
        assert started["task"]["reasoning"] == "low"
        receive_event(channel, "text")
        queued_selection = {**selection, "reasoning": "high"}
        channel.send({"action": "send", "prompt": "排队的工作", "new_task": True, "model_selection": queued_selection})
        queued = receive_event(channel, "queue")
        assert queued["queue"][0]["task"]["reasoning"] == "high"
        channel.send({"action": "select_model", "model": "deepseek-flash", "reasoning": "max"})
        rejected = receive_event(channel, "model_controls")
        assert rejected["error"] and rejected["selection"]["reasoning"] == "high"
        channel.send({"action": "new"})
        receive_event(channel, "conversation")
        channel.send({"action": "configure", "thinking": True})
        receive_event(channel, "configured")
        channel.send({"action": "stop", "path": first_path})
        stopped = receive_event(channel, "finished")
        assert stopped["turn"]["reasoning"] == "low"
        channel.send({"action": "start_next", "path": first_path})
        following = receive_event(channel, "started")
        assert following["task"]["reasoning"] == "high"
        completed = receive_event(channel, "finished")
        assert completed["turn"]["reasoning"] == "high"
        assert completed["turn"]["task"]["provider_config"]["model"] == "deepseek-flash"


def test_same_connection_model_switch_reuses_key_and_sends_new_choice_with_existing_history(tmp_path: Path) -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            event = {"id": "selection-response", "object": "chat.completion.chunk", "created": 1, "model": body["model"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"model={body['model']}"}, "finish_reason": "stop"}]}
            response = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with connection(tmp_path, "myrk.desktop_service") as channel:
            provider = {"name": "受控服务", "base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "main-model", "protocol": "openai-chat", "input_token_limit_override": 128000}
            channel.send({"action": "configure", "key": "synthetic-only", "provider_config": provider})
            receive_event(channel, "configured")
            channel.send({"action": "get_model_controls"})
            first = receive_event(channel, "model_controls")
            channel.send({"action": "send", "prompt": "第一轮", "model_selection": first["selection"]})
            answered = receive_event(channel, "finished")
            assert answered["turn"]["answer"] == "model=main-model", answered

            channel.send({"action": "select_model", "model": "other-model", "reasoning": "default"})
            changed = receive_event(channel, "model_controls")
            assert changed["error"] is None
            assert changed["selection"]["provider_config"].get("input_token_limit_override") is None
            channel.send({"action": "configure", "provider_config": {**provider, "model": "other-model"}})
            receive_event(channel, "configured")
            channel.send({"action": "get_model_controls"})
            changed = receive_event(channel, "model_controls")
            channel.send({"action": "send", "prompt": "按刚选的型号继续", "model_selection": changed["selection"]})
            started = receive_event(channel, "started")
            pending = receive_event(channel, "action")
            assert pending["action"]["kind"] == "history_reuse" and pending["action"]["status"] == "awaiting_approval"
            action = pending["action"]
            channel.send({"action": "approve_action", "path": pending["path"], "task_id": started["task"]["id"],
                          "action_id": action["id"], "approval_id": action["approval_id"], "approved": True})
            following = receive_event(channel, "finished")
            assert following["turn"]["answer"] == "model=other-model"
            assert following["turn"]["reasoning"] == "default"
            assert following["turn"]["task"]["provider_config"]["model"] == "other-model"
        assert [body["model"] for body in requests] == ["main-model", "other-model"]
        assert all("reasoning_effort" not in body for body in requests)
        assert len(requests[1]["messages"]) > len(requests[0]["messages"])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_reopening_legacy_work_resumes_its_saved_thinking_instead_of_new_global_default(tmp_path: Path) -> None:
    stored = Conversation(tmp_path / "legacy.json")
    task = Task(goal="保留原工作", scope=None)
    failed = stored.record_failure("原工作", thinking=True, error="受控服务中断")
    failed.task = task
    failed.service_failure = True
    stored.save()
    original = json.loads(stored.path.read_text(encoding="utf-8"))
    original["version"] = 7
    original.pop("model_selection")
    original["turns"][0].pop("reasoning")
    original["turns"][0]["task"].pop("reasoning")
    original["turns"][0]["task"].pop("legacy_thinking")
    stored.path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only", "thinking": False})
        receive_event(channel, "configured")
        channel.send({"action": "open", "path": str(stored.path)})
        receive_event(channel, "conversation")
        channel.send({"action": "resume_task", "task_id": task.id})
        resumed = receive_event(channel, "finished")
        assert resumed["turn"]["status"] == "completed"
        assert resumed["turn"]["thinking"] is True
        assert resumed["turn"]["reasoning"] is None
        assert resumed["turn"]["task"]["legacy_thinking"] is True
    assert Conversation.open(stored.path).turns[0].thinking is True


def test_automatic_default_controls_do_not_create_history_but_explicit_empty_selection_is_saved(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only"})
        receive_event(channel, "configured")
        channel.send({"action": "get_model_controls"})
        first = receive_event(channel, "model_controls")
        assert first["selection"]["provider_config"]["model"] == "deepseek-flash"
        channel.send({"action": "list"})
        assert receive_event(channel, "conversations")["items"] == []
        channel.send({"action": "new"})
        created = receive_event(channel, "conversation")
        assert created["model_selection"]["reasoning"] == "high"
        channel.send({"action": "get_model_controls"})
        receive_event(channel, "model_controls")
        channel.send({"action": "list"})
        assert receive_event(channel, "conversations")["items"] == []
        channel.send({"action": "select_model", "model": "deepseek-v4-pro", "reasoning": "default"})
        selected = receive_event(channel, "model_controls")
        assert selected["error"] is None
        channel.send({"action": "list"})
        saved = receive_event(channel, "conversations")["items"]
        assert [item["path"] for item in saved] == [created["path"]]
        assert Conversation.open(Path(created["path"])).model_selection.provider_config.model == "deepseek-v4-pro"


def test_default_controls_and_snapshot_work_when_record_directory_is_blocked_by_a_file(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked-records"
    blocked.write_text("保留原文件", encoding="utf-8")
    with connection(blocked) as channel:
        channel.send({"action": "get_model_controls"})
        controls = receive_event(channel, "model_controls")
        assert controls["error"] is None
        channel.send({"action": "snapshot"})
        snapshot = receive_event(channel, "snapshot")
        assert snapshot["configured"] is False
        assert snapshot["conversations"][0]["model_selection"]["reasoning"] == "high"
        assert blocked.read_text(encoding="utf-8") == "保留原文件"


def test_explicit_provider_save_updates_current_chat_even_when_another_chat_already_selected_it(tmp_path: Path) -> None:
    google = {"name": "Gemini", "base_url": "https://generativelanguage.googleapis.com", "model": "gemini-2.5-pro", "protocol": "google"}
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only"})
        receive_event(channel, "configured")
        channel.send({"action": "select_model", "model": "deepseek-flash", "reasoning": "max"})
        first = receive_event(channel, "model_controls")
        channel.send({"action": "new"})
        receive_event(channel, "conversation")
        channel.send({"action": "configure", "key": "synthetic-google", "provider_config": google})
        receive_event(channel, "configured")
        channel.send({"action": "open", "path": first["path"]})
        receive_event(channel, "conversation")
        channel.send({"action": "configure", "provider_config": google})
        receive_event(channel, "configured")
        channel.send({"action": "get_model_controls"})
        changed = receive_event(channel, "model_controls")
        assert changed["selection"] == {"provider_config": ProviderConfig.model_validate(google).model_dump(), "reasoning": "dynamic"}
        channel.send({"action": "select_model", "model": "gemini-2.5-pro", "reasoning": "high"})
        receive_event(channel, "model_controls")
        channel.send({"action": "configure", "provider_config": google, "delegation_max_turns": 17})
        receive_event(channel, "configured")
        channel.send({"action": "get_model_controls"})
        assert receive_event(channel, "model_controls")["selection"]["reasoning"] == "high"


def test_provider_preview_explains_and_freezes_target_default_when_original_effort_is_unavailable(tmp_path: Path) -> None:
    conversation = Conversation(tmp_path / "provider.json")
    source = Task(goal="继续原工作", scope=None, reasoning="max")
    failed = conversation.record_failure("原工作", thinking=False, error="受控失败")
    failed.task, failed.reasoning, failed.service_failure = source, "max", True
    target = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-3-flash-preview", protocol="google")

    change = conversation.preview_provider_change(source.id, target)

    assert change.compatible and change.target_reasoning == "high"
    assert "目标型号的初始" in change.reason
    approved = conversation.decide_provider_change(change.id, True)
    assert approved.provider_config == target
    assert approved.reasoning == "high"
    assert failed.reasoning == "max"


def test_provider_preview_preserves_supported_effort_but_rejects_known_tool_incompatible_target(tmp_path: Path) -> None:
    conversation = Conversation(tmp_path / "provider-capability.json")
    source = Task(goal="继续原工作", scope=None, reasoning="high")
    failed = conversation.record_failure("原工作", thinking=False, error="受控失败")
    failed.task, failed.reasoning, failed.service_failure = source, "high", True
    compatible = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-3-flash-preview", protocol="google")
    accepted = conversation.preview_provider_change(source.id, compatible)
    assert accepted.compatible and accepted.target_reasoning == "high"
    assert "目标型号的初始" not in accepted.reason

    incompatible = ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model="gpt-6.1-sol", protocol="openai-chat")
    rejected = conversation.preview_provider_change(source.id, incompatible)
    assert rejected.compatible is False
    assert "工具" in rejected.reason
    with pytest.raises(ValueError, match="工具"):
        conversation.decide_provider_change(rejected.id, True)
    assert source.provider_config == ProviderConfig() and source.reasoning == "high"


@pytest.mark.parametrize("original,expected", [("default", "high"), ("none", "low")])
def test_old_chat_preferences_resolve_for_new_work_without_changing_queued_work(tmp_path: Path, original: str, expected: str) -> None:
    stored = Conversation(tmp_path / "saved-preferences.json")
    stored.set_model_selection(ModelSelection(provider_config=ProviderConfig(), reasoning=original))
    stored.queue.append(QueuedTask(task=Task(goal="原排队工作", scope=None, reasoning=original)))
    stored.save()

    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-only"})
        receive_event(channel, "configured")
        channel.send({"action": "open", "path": str(stored.path)})
        receive_event(channel, "conversation")
        channel.send({"action": "get_model_controls"})
        controls = receive_event(channel, "model_controls")
        assert controls["selection"]["reasoning"] == expected
        assert Conversation.open(stored.path).queue[0].task.reasoning == original
        channel.send({"action": "start_next"})
        started = receive_event(channel, "started")
        assert started["task"]["reasoning"] == original
        assert receive_event(channel, "finished")["turn"]["reasoning"] == original
        channel.send({"action": "send", "prompt": "新工作", "new_task": True, "model_selection": controls["selection"]})
        assert receive_event(channel, "started")["task"]["reasoning"] == expected
        receive_event(channel, "finished")

    reopened = Conversation.open(stored.path)
    assert reopened.turns[0].reasoning == original
    assert reopened.model_selection is not None and reopened.model_selection.reasoning == expected


@pytest.mark.parametrize("legacy_configuration", [False, True])
def test_resumed_supplements_keep_the_running_connection_and_future_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_configuration: bool) -> None:
    lifecycle = tmp_path / "controlled-lifecycle"
    lifecycle.mkdir()
    monkeypatch.setenv("MYRK_LIFECYCLE_DIRECTORY", str(lifecycle))
    original = ProviderConfig()
    future = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-pro", protocol="google")
    stored = Conversation(tmp_path / "resumed-connection.json")
    task = Task(goal="继续原工作", scope=None, reasoning="high")
    failed = stored.record_failure(task.goal, thinking=False, error="Controlled prior outage")
    failed.task, failed.reasoning, failed.service_failure = task, "high", True
    stored.set_model_selection(ModelSelection(provider_config=original, reasoning="high"))

    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "lifecycle-controlled"})
        receive_event(channel, "configured")
        channel.send({"action": "open", "path": str(stored.path)})
        receive_event(channel, "conversation")
        channel.send({"action": "configure", "key": "synthetic-only-unused", "provider_config": future.model_dump(),
                      **({"thinking": True} if legacy_configuration else {})})
        receive_event(channel, "configured")
        channel.send({"action": "resume_task", "task_id": task.id})
        started = receive_event(channel, "started")
        assert started["task"]["provider_config"] == original.model_dump()
        try:
            receive_event(channel, "text")
            channel.send({"action": "send", "prompt": "这是给原工作的补充"})
            received = receive_event(channel, "supplement")
            assert received["task_id"] == task.id and received["saved"]

            channel.send({"action": "snapshot"})
            snapshot = receive_event(channel, "snapshot")
            current = next(item for item in snapshot["conversations"] if item["path"] == str(stored.path))
            assert current["turns"][-1]["task"]["provider_config"] == original.model_dump()
            assert current["turns"][-1]["task"]["reasoning"] == "high"
            assert current["model_selection"] == (None if legacy_configuration else {"provider_config": future.model_dump(), "reasoning": "dynamic"})

            channel.send({"action": "send", "prompt": "另办未批准的新工作", "new_task": True})
            assert "原会话历史尚未批准" in receive_event(channel, "error")["message"]
            channel.send({"action": "send", "prompt": "补充不改服务", "model_selection": {"provider_config": future.model_dump(), "reasoning": "high"}})
            assert receive_event(channel, "error")["path"] == str(stored.path)
            channel.send({"action": "send", "prompt": "补充不改批准方式", "approval_mode": "full_access"})
            assert "补充不能变更" in receive_event(channel, "error")["message"]
        finally:
            channel.send({"action": "stop", "path": str(stored.path)})
            stopped = receive_event(channel, "finished")["turn"]["task"]
        assert stopped["provider_config"] == original.model_dump()
        assert stopped["reasoning"] == "high"
        assert [item["text"] for item in stopped["supplements"]] == ["这是给原工作的补充"]
