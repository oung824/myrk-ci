"""Desktop connection restoration and forgetting, without real model requests."""

from pathlib import Path
import pytest

from myrk.conversation import Conversation, QueuedTask, Task
from myrk.model_controls import ModelSelection
from myrk.provider import ProviderConfig
from tests.test_desktop_service import connection
from tests.test_model_selection import receive_event


def test_restored_connections_acknowledge_without_starting_or_replacing_chat_preferences(tmp_path: Path) -> None:
    stored = Conversation(tmp_path / "existing.json")
    preferred = ModelSelection(provider_config=ProviderConfig(), reasoning="max")
    stored.set_model_selection(preferred)
    other = ProviderConfig(name="Other", base_url="https://example.invalid/v1", model="model", protocol="openai-chat")
    with connection(tmp_path) as channel:
        channel.send({"action": "open", "path": str(stored.path)})
        receive_event(channel, "conversation")
        channel.send({"action": "restore_connections", "request_id": "restore-test", "connections": [
            {"provider_config": ProviderConfig().model_dump(), "key": "synthetic-first"},
            {"provider_config": other.model_dump(), "key": "synthetic-second"},
        ], "selected_provider": other.model_dump()})
        restored = channel.receive()
        assert restored["event"] == "configured" and restored["request_id"] == "restore-test", restored
        assert restored["configured"] is True and restored["provider_config"]["name"] == "Other"
        assert "synthetic-" not in str(restored)
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        restored_chat = next(item for item in snapshot["conversations"] if item["path"] == str(stored.path))
        assert restored_chat["model_selection"] == preferred.model_dump()
        assert restored_chat["turns"] == []
        assert restored_chat["running"] is False
        assert not list(tmp_path.glob("*.json.tmp"))
        channel.send({"action": "configure", "request_id": "reuse-test", "provider_config": other.model_copy(update={"model": "another-model"}).model_dump()})
        reused = channel.receive()
        assert reused["event"] == "configured" and reused["request_id"] == "reuse-test"
        assert reused["configured"] is True


def test_forgetting_selected_connection_keeps_other_chat_queue_and_new_work_available(tmp_path: Path) -> None:
    stored = Conversation(tmp_path / "queued.json")
    stored.set_model_selection(ModelSelection(provider_config=ProviderConfig(), reasoning="high"))
    stored.queue.append(QueuedTask(task=Task(goal="原连接排队工作", scope=None), document=None))
    stored.save()
    other = ProviderConfig(name="Other", base_url="https://example.invalid/v1", model="model", protocol="openai-chat")
    with connection(tmp_path) as channel:
        channel.send({"action": "restore_connections", "request_id": "restore", "connections": [
            {"provider_config": ProviderConfig().model_dump(), "key": "synthetic-first"},
            {"provider_config": other.model_dump(), "key": "synthetic-second"},
        ], "selected_provider": other.model_dump()})
        receive_event(channel, "configured")
        channel.send({"action": "forget_connection", "request_id": "forget-other", "provider_config": other.model_dump()})
        forgotten = channel.receive()
        assert forgotten["event"] == "configured" and forgotten["request_id"] == "forget-other"
        assert forgotten["configured"] is False
        channel.send({"action": "open", "path": str(stored.path)})
        receive_event(channel, "conversation")
        channel.send({"action": "start_next"})
        queued = receive_event(channel, "finished")
        assert queued["turn"]["status"] == "completed"
        channel.send({"action": "send", "prompt": "继续当前聊天"})
        following = receive_event(channel, "finished")
        assert following["turn"]["status"] == "completed"
        channel.send({"action": "configure", "request_id": "missing-other", "provider_config": other.model_dump()})
        rejected = channel.receive()
        assert rejected["event"] == "error" and rejected["request_id"] == "missing-other"
        assert "synthetic-" not in str(rejected)


@pytest.mark.parametrize("action", ["resume_task", "continue_task"])
def test_forgetting_other_connection_does_not_block_explicit_recovery(tmp_path: Path, action: str) -> None:
    stored = Conversation(tmp_path / "recover.json")
    task = Task(goal="原连接中断工作", scope=None, request_limit=3)
    failed = stored.record_failure("原工作", thinking=False, error="受控服务中断")
    failed.task = task
    failed.service_failure = True
    stored.save()
    other = ProviderConfig(name="Other", base_url="https://example.invalid/v1", model="model", protocol="openai-chat")
    with connection(tmp_path) as channel:
        channel.send({"action": "restore_connections", "request_id": "restore", "connections": [
            {"provider_config": ProviderConfig().model_dump(), "key": "synthetic-first"},
            {"provider_config": other.model_dump(), "key": "synthetic-second"},
        ], "selected_provider": other.model_dump()})
        receive_event(channel, "configured")
        channel.send({"action": "forget_connection", "request_id": "forget", "provider_config": other.model_dump()})
        receive_event(channel, "configured")
        channel.send({"action": "open", "path": str(stored.path)})
        receive_event(channel, "conversation")
        channel.send({"action": action, "task_id": task.id, **({"request_limit": 5} if action == "continue_task" else {})})
        completed = receive_event(channel, "finished")
        assert completed["turn"]["status"] == "completed"
        assert completed["turn"]["task"]["id"] == task.id


def test_forgotten_connection_keeps_running_work_but_requires_key_for_future_work(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "request_id": "save", "key": "synthetic-first"})
        configured = channel.receive()
        assert configured["request_id"] == "save"
        channel.send({"action": "send", "prompt": "停止，等补充"})
        started = receive_event(channel, "started")
        receive_event(channel, "text")
        channel.send({"action": "forget_connection", "request_id": "forget", "provider_config": ProviderConfig().model_dump()})
        forgotten = receive_event(channel, "configured")
        assert forgotten["request_id"] == "forget" and forgotten["configured"] is False
        channel.send({"action": "send", "prompt": "补充当前工作"})
        supplement = receive_event(channel, "supplement")
        assert supplement["task_id"] == started["task"]["id"] and supplement["state"] == "received"
        channel.send({"action": "send", "prompt": "另办新工作", "new_task": True})
        rejected = channel.receive()
        assert rejected["event"] == "error"
        channel.send({"action": "stop"})
        stopped = receive_event(channel, "finished")
        assert stopped["turn"]["status"] == "cancelled"
        channel.send({"action": "send", "prompt": "另办新工作"})
        assert channel.receive()["event"] == "error"
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert snapshot["configured"] is False
        assert snapshot["conversations"][0]["queue"] == []
        assert snapshot["conversations"][0]["model_selection"]["reasoning"] == "high"
        assert "synthetic-first" not in str(snapshot)
        channel.send({"action": "configure", "request_id": "approval-only", "approval_mode": "full_access"})
        approval = channel.receive()
        assert approval["event"] == "configured" and approval["request_id"] == "approval-only"
        assert approval["configured"] is False and approval["approval_mode"] == "full_access"
    assert all("synthetic-first" not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))


def test_saved_google_catalog_uses_matching_connection_after_other_connection_is_forgotten(tmp_path: Path) -> None:
    google = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
    other = google.model_copy(update={"name": "Other"})
    with connection(tmp_path, "tests.gemini_desktop_fixture") as channel:
        channel.send({"action": "restore_connections", "request_id": "restore", "connections": [
            {"provider_config": google.model_dump(), "key": "synthetic-only"},
            {"provider_config": other.model_dump(), "key": "catalog-rejected"},
        ], "selected_provider": other.model_dump()})
        receive_event(channel, "configured")
        channel.send({"action": "forget_connection", "request_id": "forget", "provider_config": other.model_dump()})
        assert receive_event(channel, "configured")["configured"] is False
        channel.send({"action": "fetch_model_catalog", "request_id": "saved-catalog", "provider_config": {
            "name": "Gemini", "base_url": google.base_url, "protocol": "google",
        }})
        catalog = receive_event(channel, "model_catalog")
        assert catalog["request_id"] == "saved-catalog" and catalog["error"] is None
        assert [item["id"] for item in catalog["models"]] == ["gemini-first", "gemini-2.5-flash"]
        channel.send({"action": "fetch_model_catalog", "request_id": "missing-catalog", "provider_config": {
            "name": "Other", "base_url": google.base_url, "protocol": "google",
        }})
        missing = receive_event(channel, "model_catalog")
        assert missing["error"] and missing["models"] == []


def test_configure_reports_correlated_write_failure_and_keeps_service_available(tmp_path: Path) -> None:
    (tmp_path / "settings").write_text("controlled write blocker", encoding="utf-8")
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "request_id": "blocked-settings", "approval_mode": "full_access"})
        rejected = channel.receive()
        assert rejected["event"] == "error" and rejected["request_id"] == "blocked-settings"
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert snapshot["event"] == "snapshot" and snapshot["configured"] is False
        assert snapshot["approval_mode"] == "request_approval"
