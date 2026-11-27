import json
import os
import subprocess
import sys
import pytest
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from threading import Thread
from collections.abc import Iterator

from myrk.actions import Action
from myrk.conversation import Conversation, ConversationRecord, QueuedTask, Task, TaskScope, Turn
from myrk.task_control import RequestAccount, WorkStep
from myrk.provider import ProviderConfig


class Connection:
    def __init__(self, directory: Path, module: str = "tests.desktop_fixture") -> None:
        self.process = subprocess.Popen(
        [sys.executable, "-m", module, str(directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "MYRK_DATA_DIRECTORY": str(directory)},
        creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.events: Queue[dict] = Queue()
        assert self.process.stdout is not None
        stream = self.process.stdout

        def read_events() -> None:
            for line in stream:
                self.events.put(json.loads(line))

        Thread(target=read_events, daemon=True).start()

    def receive(self, timeout: float = 5) -> dict:
        while True:
            event = self.events.get(timeout=timeout)
            if event["event"] == "task" or event["event"] == "status" and event.get("text") == "正在等待回答服务":
                continue
            return event

    def send(self, command: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()

    def crash(self) -> None:
        if self.process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        self.process.wait(timeout=5)


@contextmanager
def connection(directory: Path, module: str = "tests.desktop_fixture") -> Iterator[Connection]:
    channel = Connection(directory, module)
    try:
        assert channel.receive(timeout=15)["event"] == "ready"
        yield channel
    finally:
        if channel.process.poll() is None:
            channel.crash()
        channel.process.wait(timeout=5)


def legacy_queued_conversation(path: Path, goal: str, request_limit: int, *, document: Path | None = None, scope: dict | None = None) -> Path:
    conversation = Conversation(path)
    conversation.queue.append(QueuedTask(task=Task(goal=goal, scope=TaskScope.model_validate(scope) if scope else None,
        request_limit=request_limit, active_seconds_limit=600, document=str(document) if document else None), document=str(document) if document else None))
    conversation.save()
    record = json.loads(path.read_text(encoding="utf-8"))
    record["version"] = 5
    for item in record["queue"]:
        for field in ("request_policy", "total_requests", "delegation_max_turns", "max_turns", "turns_used"):
            item["task"].pop(field, None)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return path


def test_full_access_default_is_saved_and_restored_without_connection_secrets(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "approval_mode": "full_access"})
        configured = channel.receive()
        assert configured["event"] == "configured" and configured["approval_mode"] == "full_access"
        assert configured["configured"] is False
    with connection(tmp_path) as channel:
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert snapshot["event"] == "snapshot" and snapshot["approval_mode"] == "full_access"


def test_gemini_catalog_command_reports_missing_key_without_changing_configuration(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "fetch_model_catalog", "request_id": "catalog-no-key", "provider_config": {"name": "Gemini", "base_url": "https://generativelanguage.googleapis.com", "protocol": "google"}})
        result = channel.receive()
        assert result["event"] == "model_catalog" and result["request_id"] == "catalog-no-key"
        assert result["models"] == [] and "连接密码" in result["error"]
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert snapshot["configured"] is False and snapshot["provider_config"]["protocol"] == "deepseek"


def test_gemini_catalog_selection_and_native_text_work_through_desktop_commands(tmp_path: Path) -> None:
    connection_config = {"name": "Gemini", "base_url": "https://generativelanguage.googleapis.com", "protocol": "google"}
    with connection(tmp_path, "tests.gemini_desktop_fixture") as channel:
        channel.send({"action": "fetch_model_catalog", "request_id": "first-catalog", "key": "synthetic-only", "provider_config": connection_config})
        catalog = channel.receive()
        assert catalog["event"] == "model_catalog" and catalog["request_id"] == "first-catalog" and catalog["error"] is None
        assert [entry["id"] for entry in catalog["models"]] == ["gemini-first", "gemini-2.5-flash"]
        channel.send({"action": "snapshot"})
        assert channel.receive()["configured"] is False
        config = ProviderConfig(**connection_config, model="gemini-2.5-flash").model_dump()
        channel.send({"action": "configure", "key": "synthetic-only", "provider_config": config})
        assert channel.receive()["provider_config"] == config
        channel.send({"action": "fetch_model_catalog", "request_id": "refresh-catalog", "provider_config": connection_config})
        assert channel.receive()["error"] is None
        channel.send({"action": "send", "prompt": "文字对话", "scope": {"root": str(tmp_path), "allowed_actions": [], "provider": "google", "recipients": ["google"]}})
        started = channel.receive()
        assert started["event"] == "started" and started["task"]["scope"]["recipients"] == ["google"]
        events = []
        while not events or events[-1]["event"] != "finished":
            events.append(channel.receive())
        assert events[-1]["turn"]["status"] == "completed", events[-1]["turn"]["error"]
        assert any(event["event"] == "text" and event["text"] == "原生文字流" for event in events)
        assert events[-1]["turn"]["status"] == "completed" and events[-1]["turn"]["requests"] == 1
        reopened = Conversation.open(Path(events[-1]["path"]))
        assert reopened.turns[-1].task.provider_config == ProviderConfig(**config)
        assert "synthetic-only" not in reopened.path.read_text(encoding="utf-8")


def test_legacy_whole_task_deadline_is_ignored_and_each_child_inherits_saved_turn_limit(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only", "delegation_max_turns": 7, "active_seconds_limit": 0.001})
        configured = channel.receive()
        assert configured["active_seconds_limit"] is None
        channel.send({"action": "send", "prompt": "普通回答"})
        started = channel.receive()
        assert started["event"] == "started"
        assert started["task"]["request_limit"] is None and started["task"]["max_turns"] is None
        assert started["task"]["delegation_max_turns"] == 7
        assert started["task"]["request_policy"] == "independent"
        assert started["task"]["active_seconds_limit"] is None
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["status"] == "completed"
        assert finished["turn"]["task"]["active_seconds"] > 0.001
        assert finished["turn"]["task"]["requests"] == finished["turn"]["task"]["total_requests"] == 1


def test_imported_child_record_cannot_silently_start_an_unlimited_main_account(tmp_path: Path) -> None:
    path = tmp_path / "child.json"
    task = Task(goal="原完整工作", scope=None, requests=1, turns_used=1, total_requests=4)
    record = ConversationRecord(version=6, messages=[], turns=[Turn(prompt="受委派工作：报告", answer="已有部分报告", status="limited", task=task,
        requests=3, request_owner_id="child", request_account=RequestAccount(max_turns=2, turns_used=2, requests=3))])
    path.write_text(record.model_dump_json(), encoding="utf-8")
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": str(path)})
        assert channel.receive()["event"] == "conversation"
        channel.send({"action": "send", "prompt": "继续这份子助理工作"})
        refused = channel.receive()
        assert refused["event"] == "error" and "主聊天" in refused["message"]
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        child = next(item for item in snapshot["conversations"] if item["path"] == str(path))["turns"][-1]
        assert child["task"]["id"] == task.id and child["request_account"]["requests"] == 3
        channel.send({"action": "send", "prompt": "明确另办新工作", "new_task": True})
        assert channel.receive()["event"] == "started"
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["status"] == "completed" and finished["turn"]["task"]["id"] != task.id
        assert finished["turn"]["task"]["request_limit"] is None


def test_service_failure_manual_resume_and_rejected_new_receiver(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "暂时服务故障"})
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["service_failure"] is True
        assert finished["turn"]["requests"] == 3
        task = finished["turn"]["task"]
        target = {"name": "DeepSeek", "base_url": "http://127.0.0.1:1/v1", "model": "controlled-model", "protocol": "openai-chat"}
        channel.send({"action": "configure", "key": "synthetic-second-key", "provider_config": target})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "preview_provider", "task_id": task["id"], "provider_config": target})
        preview = channel.receive()
        assert preview["event"] == "provider_preview" and preview["change"]["compatible"]
        assert preview["change"]["target"]["base_url"] == target["base_url"]
        assert "暂时服务故障" in preview["change"]["materials"]
        channel.send({"action": "decide_provider", "change_id": preview["change"]["id"], "approved": False})
        assert channel.receive()["event"] == "provider_decided"
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert not snapshot["conversations"][0]["running"]
        assert snapshot["conversations"][0]["turns"][-1]["task"]["requests"] == 3
        channel.send({"action": "resume_task", "task_id": task["id"]})
        while (resumed := channel.receive())["event"] != "finished":
            pass
        assert resumed["turn"]["status"] == "completed", resumed
        assert resumed["turn"]["task"]["id"] == task["id"]
        assert resumed["turn"]["task"]["requests"] == 4


@pytest.mark.parametrize("legacy_limit", [False, True])
def test_service_failure_remains_resumable_by_id_after_automatic_queue_and_reopen(tmp_path: Path, legacy_limit: bool) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "库存工作一次失败"})
        original = channel.receive()
        assert original["event"] == "started"
        original_id, path = original["task_id"], original["path"]
        channel.send({"action": "send", "path": path, "prompt": "独立补货工作", "new_task": True})
        while (failed := channel.receive())["event"] != "finished":
            pass
        assert failed["task_id"] == original_id and failed["turn"]["service_failure"]
        while (following := channel.receive())["event"] != "finished":
            pass
        later_id = following["task_id"]
        assert later_id != original_id and following["turn"]["answer"] == "补货完成"
        assert [report["task_id"] for report in following["earlier_recoveries"]] == [original_id]

    if legacy_limit:
        record = json.loads(Path(path).read_bytes())
        saved_task = record["turns"][0]["task"]
        saved_task.update(request_policy="shared", request_limit=saved_task["requests"])
        Path(path).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": path})
        opened = channel.receive()
        assert opened["event"] == "conversation" and not opened["running"]
        assert [report["task_id"] for report in opened["earlier_recoveries"]] == [original_id]
        channel.send({"action": "verify_recovery", "path": path, "task_id": original_id})
        checked = channel.receive()
        assert checked["event"] == "recovery" and checked["recovery"]["task_id"] == original_id
        channel.send({"action": "continue_task", "path": path, "task_id": original_id,
                      "request_limit": failed["turn"]["task"]["requests"] if legacy_limit else 11})
        rejected = channel.receive()
        assert rejected["event"] == "error"
        expected_reason = "须大于已用次数" if legacy_limit else "只适用于保留原共用限制的旧记录"
        assert expected_reason in rejected["message"]
        channel.send({"action": "resume_task", "path": path, "task_id": original_id})
        if legacy_limit:
            rejected = channel.receive()
            assert rejected["event"] == "error" and "已到请求次数上限" in rejected["message"]
            channel.send({"action": "continue_task", "path": path, "task_id": original_id, "request_limit": 11})
        started = channel.receive()
        assert started["event"] == "started" and started["task_id"] == original_id
        assert started["task"]["request_limit"] == (11 if legacy_limit else None)
        for field in ("requests", "turns_used", "work_step", "provider_config", "approval"):
            assert started["task"][field] == failed["turn"]["task"][field]
        while (resumed := channel.receive())["event"] != "finished":
            pass
        assert resumed["turn"]["answer"] == "用户选择后续办完成", resumed
        assert resumed["turn"]["task"]["requests"] == failed["turn"]["task"]["requests"] + 1
        assert resumed["earlier_recoveries"] == []
    turns = Conversation.open(Path(path)).turns
    assert [turn.task.id for turn in turns] == [original_id, later_id, original_id]
    assert turns[0].status == "failed" and turns[1].answer == "补货完成"


@pytest.mark.parametrize("damage", ["invalid_encoding", "missing"])
def test_attachment_failure_during_resume_keeps_the_earlier_recovery_after_reopen(tmp_path: Path, damage: str) -> None:
    document = tmp_path / "inventory.txt"
    source = b"Inventory count: 12.\n"
    document.write_bytes(source)
    records = tmp_path / "records"
    with connection(records) as channel:
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "库存工作一次失败", "document": str(document)})
        original = channel.receive()
        assert original["event"] == "started"
        original_id, path = original["task_id"], original["path"]
        channel.send({"action": "send", "path": path, "prompt": "独立补货工作", "new_task": True})
        while (failed := channel.receive())["event"] != "finished":
            pass
        assert failed["task_id"] == original_id and failed["turn"]["service_failure"]
        while (following := channel.receive())["event"] != "finished":
            pass
        assert following["turn"]["answer"] == "补货完成"
        assert [report["task_id"] for report in following["earlier_recoveries"]] == [original_id]

        if damage == "invalid_encoding":
            document.write_bytes(b"\xff")
        else:
            document.unlink()
        channel.send({"action": "resume_task", "path": path, "task_id": original_id})
        assert channel.receive()["event"] == "started"
        while (blocked := channel.receive())["event"] != "finished":
            pass
        blocked_turn = blocked["turn"]
        assert blocked["saved"] and blocked_turn["status"] == "failed"
        assert blocked_turn["requests"] == 0 and not blocked_turn["request_attempts"] and not blocked_turn["actions"]
        assert [report["task_id"] for report in blocked["earlier_recoveries"]] == [original_id]
        assert "无法读取所选资料" in blocked_turn["error"]
        for field in ("document", "requests", "turns_used", "work_step", "provider_config", "approval"):
            assert blocked_turn["task"][field] == failed["turn"]["task"][field]

    if damage == "invalid_encoding":
        assert document.read_bytes() == b"\xff"
    else:
        assert not document.exists()
    with connection(records) as channel:
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": path})
        assert [report["task_id"] for report in channel.receive()["earlier_recoveries"]] == [original_id]
        document.write_bytes(source)
        channel.send({"action": "resume_task", "path": path, "task_id": original_id})
        resumed = channel.receive()
        assert resumed["event"] == "started" and resumed["task_id"] == original_id
        while (completed := channel.receive())["event"] != "finished":
            pass
        assert completed["turn"]["answer"] == "用户选择后续办完成"
        assert completed["turn"]["task"]["requests"] == failed["turn"]["task"]["requests"] + 1
        assert completed["earlier_recoveries"] == []
    assert document.read_bytes() == source


def test_verified_service_failure_can_leave_queue_after_blocked_attachment_resume(tmp_path: Path) -> None:
    path = tmp_path / "service-failure.json"
    document = tmp_path / "inventory.txt"
    document.write_bytes(b"\xff")
    output = tmp_path / "completed-inventory.txt"
    output.write_bytes(b"Inventory already recorded.\n")
    task = Task(goal="库存核对", scope=None, document=str(document), requests=2, request_limit=3, request_policy="shared")
    action = Action(task_id=task.id, kind="create", target=str(output), status="completed", effect="completed")
    following = Task(goal="独立补货工作", scope=None)
    record = ConversationRecord(version=10, messages=[], turns=[
        Turn(prompt=task.goal, answer="已保留的部分答复", task=task, status="failed", service_failure=True,
             requests=2, actions=[action]),
    ], queue=[QueuedTask(task=following)])
    path.write_text(record.model_dump_json(), encoding="utf-8")

    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": str(path)})
        assert channel.receive()["recovery"]["task_id"] == task.id
        channel.send({"action": "resume_task", "task_id": task.id})
        assert channel.receive()["event"] == "started"
        while (blocked := channel.receive())["event"] != "finished":
            pass
        assert blocked["saved"] and blocked["turn"]["resume_attachment_read_failed"]
        assert blocked["turn"]["requests"] == 0 and blocked["turn"]["actions"] == []
        assert blocked["turn"]["task"]["id"] == task.id
        assert blocked["turn"]["task"]["requests"] == task.requests
        assert blocked["turn"]["task"]["request_limit"] == task.request_limit

    with connection(tmp_path) as channel:
        channel.send({"action": "open", "path": str(path)})
        opened = channel.receive()
        assert opened["recovery"]["task_id"] == task.id
        assert [item["task"]["id"] for item in opened["queue"]] == [following.id]
        channel.send({"action": "start_next"})
        rejected = channel.receive()
        assert rejected["event"] == "error" and "密码" in rejected["message"]
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "verify_recovery", "task_id": task.id})
        verified = channel.receive()
        assert verified["event"] == "recovery" and verified["saved"] and verified["recovery"]["can_resume"]
        assert [(item["action_id"], item["effect"]) for item in verified["recovery"]["actions"]] == [(action.id, "completed")]
        channel.send({"action": "start_next"})
        queue = channel.receive()
        assert queue["event"] == "queue" and queue["queue"] == [], queue
        started = channel.receive()
        assert started["event"] == "started" and started["task_id"] == following.id
        while (completed := channel.receive())["event"] != "finished":
            pass
        assert completed["turn"]["answer"] == "补货完成" and completed["turn"]["requests"] == 1
        assert completed["recovery"] is None
        assert [report["task_id"] for report in completed["earlier_recoveries"]] == [task.id]
    assert output.read_bytes() == b"Inventory already recorded.\n"


def test_running_task_stays_current_after_blocked_attachment_resume(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.json"
    document = tmp_path / "inventory.txt"
    document.write_bytes(b"\xff")
    earlier = Task(goal="旧工单", scope=None)
    task = Task(goal="库存核对", scope=None, document=str(document), requests=2, turns_used=2,
                request_limit=3, request_policy="shared",
                work_step=WorkStep(turn_number=2, request_pending=True, capacity_recovery_used=True))
    next_task = Task(goal="独立补货工作", scope=None)
    record = ConversationRecord(version=10, messages=[], turns=[
        Turn(prompt=earlier.goal, answer="暂未完成", task=earlier, status="failed", service_failure=True),
        Turn(prompt=task.goal, answer="已保留的部分答复", task=task, status="running", requests=2),
    ], queue=[QueuedTask(task=next_task)])
    path.write_text(record.model_dump_json(), encoding="utf-8")
    for _ in range(2):
        with connection(tmp_path) as channel:
            channel.send({"action": "configure", "key": "recovery-controlled"})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "open", "path": str(path)})
            opened = channel.receive()
            assert opened["recovery"]["task_id"] == task.id
            assert [report["task_id"] for report in opened["earlier_recoveries"]] == [earlier.id]
            assert [item["task"]["id"] for item in opened["queue"]] == [next_task.id]

            channel.send({"action": "resume_task", "path": str(path), "task_id": earlier.id})
            rejected = channel.receive()
            assert rejected["event"] == "error" and "最近一项工作仍待核查" in rejected["message"]
            channel.send({"action": "start_next"})
            rejected = channel.receive()
            assert rejected["event"] == "error" and "排队工作已保留" in rejected["message"]
            channel.send({"action": "send", "path": str(path), "prompt": "另办新工单", "new_task": True})
            rejected = channel.receive()
            assert rejected["event"] == "error" and "意外中断" in rejected["message"]

            channel.send({"action": "resume_task", "path": str(path), "task_id": task.id})
            assert channel.receive()["event"] == "started"
            while (blocked := channel.receive())["event"] != "finished":
                pass
            assert blocked["saved"] and blocked["turn"]["resume_attachment_read_failed"]
            assert blocked["turn"]["requests"] == 0 and not blocked["turn"]["actions"]
            assert blocked["recovery"]["task_id"] == task.id
            for field in ("document", "requests", "turns_used", "work_step", "provider_config", "approval"):
                assert blocked["turn"]["task"][field] == task.model_dump(mode="json")[field]

    source = b"Inventory count: 12.\n"
    document.write_bytes(source)
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": str(path)})
        assert channel.receive()["recovery"]["task_id"] == task.id
        channel.send({"action": "continue_task", "path": str(path), "task_id": task.id, "request_limit": 13})
        started = channel.receive()
        assert started["event"] == "started" and started["task_id"] == task.id
        assert started["task"]["request_limit"] == 13
        while (completed := channel.receive())["event"] != "finished":
            pass
        assert completed["turn"]["answer"] == "用户选择后续办完成"
        assert completed["turn"]["task"]["requests"] == task.requests + 1
        assert completed["turn"]["task"]["request_limit"] == 13
        assert completed["recovery"] is None
        while (queued := channel.receive())["event"] != "finished":
            pass
        assert queued["task_id"] == next_task.id and queued["turn"]["status"] == "completed"
    assert document.read_bytes() == source


@pytest.mark.parametrize("approve_history", [False, True], ids=["denied", "approved_then_attachment_fails"])
def test_earlier_failure_resume_requires_approval_to_send_history_from_a_later_receiver(tmp_path: Path, approve_history: bool) -> None:
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    path = tmp_path / "history.json"
    document = tmp_path / "inventory.txt"
    if approve_history:
        document.write_bytes(b"\xff")
    earlier = Task(goal="库存工作", scope=None, document=str(document) if approve_history else None)
    later = Task(goal="另一接收方的补货工作", scope=None, provider_config=ProviderConfig(
        name="Synthetic B", base_url="https://synthetic-b.example/v1", model="synthetic-b-model", protocol="openai-chat"))
    original = Turn(prompt=earlier.goal, answer="库存未完成", status="failed", service_failure=True, task=earlier)
    completed = Turn(prompt=later.goal, answer="另一接收方的历史", status="completed", task=later)
    record = ConversationRecord(version=10, turns=[original, completed], messages=[
        ModelRequest(parts=[UserPromptPart(content=later.goal)]), ModelResponse(parts=[TextPart(content=completed.answer)]),
    ])
    path.write_text(record.model_dump_json(), encoding="utf-8")
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "recovery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": str(path)})
        assert channel.receive()["earlier_recoveries"][0]["task_id"] == earlier.id
        channel.send({"action": "resume_task", "path": str(path), "task_id": earlier.id})
        assert channel.receive()["event"] == "started"
        while (event := channel.receive())["event"] != "action" or event["action"]["status"] != "awaiting_approval":
            assert event["event"] != "finished", event
        action = event["action"]
        assert action["kind"] == "history_reuse" and "synthetic-b.example" in action["impact"]
        channel.send({"action": "approve_action", "path": str(path), "task_id": earlier.id,
            "action_id": action["id"], "approval_id": action["approval_id"], "approved": approve_history})
        while (result := channel.receive())["event"] != "finished":
            pass
        assert result["turn"]["requests"] == 0 and result["turn"]["status"] == "failed"
        if approve_history:
            assert "无法读取所选资料" in result["turn"]["error"]
            assert [(item["kind"], item["status"]) for item in result["turn"]["actions"]] == [("history_reuse", "completed")]
        assert [report["task_id"] for report in result["earlier_recoveries"]] == [earlier.id]
    assert [report.task_id for report in Conversation.open(path).earlier_recoveries] == [earlier.id]
    if approve_history:
        document.write_bytes(b"Inventory count: 12.\n")
        with connection(tmp_path) as channel:
            channel.send({"action": "configure", "key": "recovery-controlled"})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "open", "path": str(path)})
            assert channel.receive()["earlier_recoveries"][0]["task_id"] == earlier.id
            channel.send({"action": "resume_task", "path": str(path), "task_id": earlier.id})
            started = channel.receive()
            assert started["event"] == "started" and started["task_id"] == earlier.id
            while (result := channel.receive())["event"] != "finished":
                assert result["event"] != "action" or result["action"]["status"] != "awaiting_approval", result
            assert result["turn"]["answer"] == "用户选择后续办完成"
            assert result["turn"]["requests"] == 1 and result["earlier_recoveries"] == []


def test_stopping_an_earlier_task_attempt_removes_its_recovery_entry(tmp_path: Path) -> None:
    path = tmp_path / "stopped.json"
    task = Task(goal="原工单", scope=None)
    other = Task(goal="稍后排队的独立工作", scope=None)
    record = ConversationRecord(version=10, messages=[], turns=[
        Turn(prompt=task.goal, answer="已有内容", task=task, status="failed", service_failure=True),
        Turn(prompt=other.goal, answer="用户停止独立工作", task=other, status="cancelled"),
        Turn(prompt=task.goal, answer="用户停止续办", task=task, status="cancelled"),
    ])
    path.write_text(record.model_dump_json(), encoding="utf-8")
    opened = Conversation.open(path)
    assert not opened.earlier_recoveries
    with pytest.raises(ValueError, match="不能续办"):
        opened.prepare_resume(task.id)


def test_second_openai_message_service_dispatches_only_after_exact_receiver_approval(tmp_path: Path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            requests.append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            if self.path.startswith("/source/"):
                body = json.dumps({"error": {"message": "controlled 503", "type": "server_error"}}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            chunk = {"id": "controlled-second", "object": "chat.completion.chunk", "created": 1, "model": "second-model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "经批准的新服务完成"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}}
            body = (f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with connection(tmp_path, module="myrk.desktop_service") as channel:
            source = {"name": "DeepSeek", "base_url": f"http://127.0.0.1:{server.server_port}/source/v1", "model": "source-model", "protocol": "openai-chat", "input_token_limit_override": 128000}
            channel.send({"action": "configure", "key": "controlled-first-password", "provider_config": source})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "send", "prompt": "保留的原始任务"})
            while (first := channel.receive())["event"] != "finished":
                pass
            task = first["turn"]["task"]
            assert len(requests) == 3
            target = {"name": "DeepSeek", "base_url": f"http://127.0.0.1:{server.server_port}/target/v1", "model": "second-model", "protocol": "openai-chat", "input_token_limit_override": 128000}
            channel.send({"action": "configure", "key": "controlled-second-password", "provider_config": target})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "send", "prompt": "不能用设置变化偷偷外发旧历史"})
            assert channel.receive()["event"] == "error" and len(requests) == 3
            for approved in [False, True]:
                channel.send({"action": "preview_provider", "task_id": task["id"], "provider_config": target})
                preview = channel.receive()
                assert preview["event"] == "provider_preview" and preview["change"]["compatible"]
                assert target["base_url"] in str(preview)
                assert len(requests) == 3
                channel.send({"action": "decide_provider", "change_id": preview["change"]["id"], "approved": approved})
                assert channel.receive()["event"] == "provider_decided"
            while (finished := channel.receive())["event"] != "finished":
                pass
            assert finished["turn"]["status"] == "completed", finished
            assert finished["turn"]["task"]["id"] == task["id"]
            assert finished["turn"]["task"]["requests"] == 4
            assert len(requests) == 4 and requests[-1]["path"] == "/target/v1/chat/completions"
            assert requests[-1]["body"]["model"] == "second-model"
            assert "保留的原始任务" in str(requests[-1]["body"])
            assert "controlled-second-password" not in Path(finished["path"]).read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("automatic", [False, True])
def test_reopened_queue_keeps_original_tasks_when_only_another_provider_has_a_password(tmp_path: Path, automatic: bool) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event
    release_failure = Event()
    source_ready = Event()
    source_received = Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            requests.append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            if self.path.startswith("/source/") and not source_ready.is_set():
                source_received.set()
                assert release_failure.wait(5)
                body = b'{"error":{"message":"controlled temporary failure","type":"server_error"}}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
            else:
                chunk = {"id": "queued-answer", "object": "chat.completion.chunk", "created": 1, "model": "controlled-model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "本次工作完成"}, "finish_reason": "stop"}]}
                body = (f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    source = {"name": "原服务", "base_url": f"http://127.0.0.1:{server.server_port}/source/v1", "model": "controlled-model", "protocol": "openai-chat", "input_token_limit_override": 128000}
    target = {**source, "name": "另一服务", "base_url": f"http://127.0.0.1:{server.server_port}/target/v1"}
    scope = {"root": str(tmp_path), "allowed_actions": ["read"], "provider": "openai-compatible", "recipients": ["openai-compatible"]}
    directory = tmp_path / "records"
    try:
        with connection(directory, module="myrk.desktop_service") as channel:
            channel.send({"action": "configure", "key": "controlled-source-password", "provider_config": source})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "send", "prompt": "原工作失败后保留队列", "scope": scope})
            started = channel.receive()
            assert started["event"] == "started", started
            for goal in ["排队第一项", "排队第二项"]:
                channel.send({"action": "send", "prompt": goal, "scope": scope, "new_task": True})
                queued = channel.receive()
                assert queued["event"] == "queue"
            original_queue = queued["queue"]
            assert source_received.wait(5)
            if not automatic:
                channel.send({"action": "stop"})
                assert channel.receive()["event"] == "stopping"
            else:
                channel.send({"action": "forget_connection", "request_id": "forget-queued-provider", "provider_config": source})
                assert channel.receive()["event"] == "configured"
            release_failure.set()
            while (failed := channel.receive())["event"] != "finished":
                pass
            assert failed["turn"]["status"] == ("failed" if automatic else "cancelled")
            assert failed["turn"]["service_failure"] is automatic
            assert len(requests) == (3 if automatic else 1)
            channel.send({"action": "shutdown"})
            assert channel.receive()["event"] == "closed"
            assert channel.process.wait(timeout=5) == 0
        with connection(directory, module="myrk.desktop_service") as reopened:
            reopened.send({"action": "configure", "key": "controlled-target-password", "provider_config": target})
            assert reopened.receive()["event"] == "configured"
            reopened.send({"action": "open", "path": started["path"]})
            assert reopened.receive()["queue"] == original_queue
            if automatic:
                reopened.send({"action": "preview_provider", "task_id": failed["task_id"], "provider_config": target})
                preview = reopened.receive()
                assert preview["event"] == "provider_preview" and preview["change"]["compatible"]
                reopened.send({"action": "decide_provider", "change_id": preview["change"]["id"], "approved": True})
                while (event := reopened.receive())["event"] != "finished":
                    pass
                assert event["turn"]["status"] == "completed"
            else:
                reopened.send({"action": "start_next"})
            while (blocked := reopened.receive())["event"] != "error":
                pass
            assert "原工作所选接收方" in blocked["message"] and "密码" in blocked["message"]
            assert len(requests) == (4 if automatic else 1)
            assert [item.model_dump(mode="json") for item in Conversation.open(Path(started["path"])).queue] == original_queue
            reopened.send({"action": "open", "path": started["path"]})
            opened = reopened.receive()
            assert opened["queue"] == original_queue
            assert [item["task"]["requests"] for item in opened["queue"]] == [0, 0]
            source_ready.set()
            reopened.send({"action": "configure", "key": "controlled-source-password", "provider_config": source})
            assert reopened.receive()["event"] == "configured"
            reopened.send({"action": "start_next"})
            completed = []
            while len(completed) < 2:
                event = reopened.receive()
                if event["event"] == "action" and event["action"]["kind"] == "history_reuse" and event["action"]["status"] == "awaiting_approval":
                    assert automatic and event["action"]["approval_settings"]["reviewer"] == "user"
                    assert source["base_url"] in event["action"]["impact"]
                    reopened.send({"action": "approve_action", "path": started["path"], "task_id": event["task_id"],
                        "action_id": event["action"]["id"], "approval_id": event["action"]["approval_id"], "approved": True})
                if event["event"] == "finished":
                    completed.append(event)
            assert [event["task_id"] for event in completed] == [item["task"]["id"] for item in original_queue]
            assert all(event["turn"]["task"]["requests"] == 1 and event["turn"]["task"]["scope"] == original_queue[index]["task"]["scope"] for index, event in enumerate(completed))
            assert len(requests) == (6 if automatic else 3)
            reopened.send({"action": "shutdown"})
            assert reopened.receive()["event"] == "closed"
            assert reopened.process.wait(timeout=5) == 0
        assert Conversation.open(Path(started["path"])).queue == []
    finally:
        release_failure.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("existing_work", [False, True])
def test_failed_settings_save_preserves_receiver_password_and_existing_work(tmp_path: Path, existing_work: bool) -> None:
    source = ProviderConfig().model_dump()
    target = {**source, "base_url": "http://127.0.0.1:1/v1", "model": "other-model", "protocol": "openai-chat"}
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only", "provider_config": source})
        assert channel.receive() == {"event": "configured", "approval_mode": "request_approval", "trusted_projects": [], "configured": True, "provider_config": source, "request_limit": None, "delegation_max_turns": 250, "active_seconds_limit": None, "thinking": False}
        if existing_work:
            channel.send({"action": "send", "prompt": "原接收方的工作"})
            while (first := channel.receive())["event"] != "finished":
                pass
            assert first["turn"]["status"] == "completed"

        channel.send({"action": "configure", "provider_config": target, "delegation_max_turns": 1, "active_seconds_limit": 0.01, "thinking": True})
        refused = channel.receive()
        assert refused["event"] == "error" and "连接密码" in refused["message"]
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert snapshot["provider_config"] == source
        assert snapshot["configured"] is True
        assert snapshot["request_limit"] is None
        assert snapshot["delegation_max_turns"] == 250
        assert snapshot["active_seconds_limit"] is None
        assert snapshot["thinking"] is False
        if existing_work:
            assert snapshot["conversations"][0]["turns"][-1]["task"]["provider_config"] == source

        channel.send({"action": "send", "prompt": "继续原接收方的工作"})
        while (continued := channel.receive())["event"] != "finished":
            pass
        assert continued["turn"]["status"] == "completed"
        assert continued["turn"]["task"]["provider_config"] == source
        assert continued["turn"]["task"]["request_limit"] is None
        assert continued["turn"]["task"]["delegation_max_turns"] == 250
        assert continued["turn"]["task"]["active_seconds_limit"] is None
        assert continued["turn"]["thinking"] is False

        channel.send({"action": "configure", "delegation_max_turns": 7})
        assert channel.receive() == {"event": "configured", "approval_mode": "request_approval", "trusted_projects": [], "configured": True, "provider_config": source, "request_limit": None, "delegation_max_turns": 7, "active_seconds_limit": None, "thinking": False}
        channel.send({"action": "configure", "provider_config": target})
        assert channel.receive()["event"] == "error"
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert snapshot["provider_config"] == source
        assert snapshot["request_limit"] is None
        assert snapshot["delegation_max_turns"] == 7
        assert snapshot["active_seconds_limit"] is None
        assert snapshot["thinking"] is False
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0


def test_desktop_connection_streams_saves_and_keeps_credentials_in_memory(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-secret-never-save"})
        assert channel.receive() == {"event": "configured", "approval_mode": "request_approval", "trusted_projects": [], "configured": True, "provider_config": ProviderConfig().model_dump(), "request_limit": None, "delegation_max_turns": 250, "active_seconds_limit": None, "thinking": False}
        channel.send({"action": "send", "prompt": "你好"})
        assert channel.receive()["event"] == "started"
        chunks: list[str] = []
        while True:
            event = channel.receive()
            if event["event"] == "text":
                chunks.append(event["text"])
            if event["event"] == "finished":
                break
        assert chunks == ["第一段", "第二段"]
        assert event["turn"]["status"] == "completed"
        assert event["saved"] is True
        record = Conversation.open(Path(event["path"]))
        assert [(turn.prompt, turn.answer) for turn in record.turns] == [("你好", "第一段第二段")]
        assert "synthetic-secret-never-save" not in record.path.read_text(encoding="utf-8")
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0


def test_desktop_on_demand_skills_delegation_real_check_and_main_verification(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    skill = folder / ".myrk" / "skills" / "edit-notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: edit-notes\ndescription: 精确修改后读回\n---\n只改获准的一处，并核验。", encoding="utf-8")
    (skill / "details.txt").write_text("保留原件。", encoding="utf-8")
    (folder / "notes.txt").write_text("苹果 3\n", encoding="utf-8")
    script = folder / "check.py"
    script.write_text("from pathlib import Path\nassert Path('notes.txt').read_text(encoding='utf-8') == '苹果 5\\n'\nprint('real-delegation-check')", encoding="utf-8")
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "delegation-controlled", "delegation_max_turns": 5})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "普通问题"})
        while (simple := channel.receive())["event"] != "finished":
            pass
        assert simple["turn"]["answer"] == "普通问题直接回答。" and simple["turn"]["actions"] == []
        channel.send({"action": "send", "prompt": "分工修改后核验", "scope": {"root": str(folder), "allowed_actions": ["read", "edit", "check"], "provider": "deepseek", "recipients": ["deepseek"], "checks": [{"name": "指定检查", "argv": [sys.executable, str(script)]}]}})
        progress = []
        while (event := channel.receive())["event"] != "finished":
            progress.append(event)
        assert event["turn"]["status"] == "completed", event["turn"]["error"]
        assert any(item["event"] == "plan" and item.get("plan") for item in progress)
        assert any(item["event"] == "delegation" and item.get("delegation", {}).get("status") == "running" for item in progress)
        catalog_index = next(index for index, item in enumerate(progress) if item["event"] == "skills")
        selected_index = next(index for index, item in enumerate(progress) if item["event"] == "action" and item["action"]["kind"] == "skill")
        assert catalog_index < selected_index
        assert progress[catalog_index]["skills"][0]["description"] == "精确修改后读回"
        assert event["turn"]["task"]["requests"] == 7
        assert event["turn"]["task"]["total_requests"] == 10
        assert event["turn"]["task"]["delegation_max_turns"] == 5
        assert event["turn"]["delegations"][0]["max_turns"] == 5
        assert event["turn"]["delegations"][0]["turns_used"] == event["turn"]["delegations"][0]["requests"] == 3
        assert event["turn"]["delegations"][0]["verification_status"] == "passed"
        assert event["turn"]["checks_passed"] is True
        assert event["turn"]["delivery_status"] == "awaiting_acceptance"
        assert (folder / "notes.txt").read_text(encoding="utf-8") == "苹果 5\n"
        assert Conversation.open(Path(event["path"])).turns[-1].delegations[0].verification_status == "passed"


def test_increasing_task_budget_after_reopen_verifies_completed_delegation_without_repeating_it(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    skill = folder / ".myrk" / "skills" / "edit-notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: edit-notes\ndescription: 精确修改后读回\n---\n只改获准的一处，并核验。", encoding="utf-8")
    (skill / "details.txt").write_text("保留原件。", encoding="utf-8")
    notes = folder / "notes.txt"
    notes.write_text("苹果 3\n", encoding="utf-8")
    script = folder / "check.py"
    script.write_text("from pathlib import Path\nassert Path('notes.txt').read_text(encoding='utf-8') == '苹果 5\\n'\np=Path('runs.txt')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nprint('real-delegation-check')", encoding="utf-8")
    directory = tmp_path / "records"
    scope = {"root": str(folder), "allowed_actions": ["read", "edit", "check"], "provider": "deepseek", "recipients": ["deepseek"], "checks": [{"name": "指定检查", "argv": [sys.executable, str(script)]}]}
    legacy = legacy_queued_conversation(directory / "legacy.json", "分工修改后核验", 7, scope=scope)
    with connection(directory) as channel:
        channel.send({"action": "configure", "key": "delegation-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": str(legacy)})
        assert channel.receive()["event"] == "conversation"
        channel.send({"action": "start_next"})
        while (limited := channel.receive())["event"] != "finished":
            pass
        assert limited["turn"]["status"] == "limited" and limited["turn"]["task"]["requests"] == 7
        work = limited["turn"]["delegations"][0]
        assert work["status"] == "completed" and work["verification_status"] == "pending"
        original_child = Path(work["record_path"]).read_bytes()
        assert notes.read_text(encoding="utf-8") == "苹果 5\n" and not (folder / "runs.txt").exists()
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0
    with connection(directory) as reopened:
        reopened.send({"action": "configure", "key": "delegation-controlled"})
        assert reopened.receive()["event"] == "configured"
        reopened.send({"action": "open", "path": limited["path"]})
        assert reopened.receive()["turns"][-1]["delegations"] == limited["turn"]["delegations"]
        reopened.send({"action": "continue_task", "task_id": limited["task_id"], "request_limit": 10, "active_seconds_limit": 600})
        while (finished := reopened.receive())["event"] != "finished":
            pass
        assert finished["turn"]["status"] == "completed", finished["turn"]["error"]
        assert finished["turn"]["delegations"][0]["id"] == work["id"]
        assert finished["turn"]["delegations"][0]["verification_status"] == "passed"
        assert [action["kind"] for action in finished["turn"]["actions"]] == ["verify", "check"]
        assert finished["turn"]["checks_passed"] is True
        assert finished["task_id"] == limited["task_id"] and finished["turn"]["task"]["requests"] == 10
        assert finished["turn"]["task"]["scope"] == limited["turn"]["task"]["scope"]
        assert finished["turn"]["task"]["active_seconds"] >= limited["turn"]["task"]["active_seconds"]
        assert Path(work["record_path"]).read_bytes() == original_child
        assert (folder / "runs.txt").read_text() == "x"
        reopened.send({"action": "open", "path": limited["path"]})
        record = reopened.receive()
        assert record["turns"][0]["actions"] == limited["turn"]["actions"]
        assert record["turns"][0]["delegations"] == limited["turn"]["delegations"]
        assert record["turns"][0]["status"] == "limited" and record["turns"][0]["error"] == limited["turn"]["error"]
        assert record["turns"][1]["delegations"][0]["verification_status"] == "passed"
        reopened.send({"action": "shutdown"})
        assert reopened.receive()["event"] == "closed"
        assert reopened.process.wait(timeout=5) == 0


def test_desktop_action_approvals_bind_exact_action_and_acceptance_is_separate(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    target = folder / ".agents" / "notes.txt"
    target.parent.mkdir()
    target.write_text("keep-me", encoding="utf-8")
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "actions-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "删除后再提议删除", "scope": {"root": str(folder), "allowed_actions": ["read"], "provider": "deepseek", "recipients": ["deepseek"]}})
        started = channel.receive()
        first = None
        while True:
            event = channel.receive()
            if event["event"] == "action" and event["action"]["status"] == "awaiting_approval":
                first = event["action"]
                break
        assert target.exists() and "删除单个文件" in first["impact"]
        decision = {"action": "approve_action", "path": started["path"], "task_id": started["task_id"], "action_id": first["id"], "approval_id": first["approval_id"], "approved": False}
        channel.send(decision)
        while True:
            event = channel.receive()
            if event["event"] == "action" and event["action"]["status"] == "awaiting_approval":
                second = event["action"]
                break
        assert second["id"] != first["id"] and second["approval_id"] != first["approval_id"]
        channel.send({**decision, "approved": True})
        assert channel.receive()["event"] == "error"
        assert target.exists()
        channel.send({**decision, "action_id": second["id"], "approval_id": second["approval_id"], "approved": False})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert target.read_text(encoding="utf-8") == "keep-me"
        assert event["turn"]["delivery_status"] == "awaiting_acceptance" and event["turn"]["checks_passed"] is None
        channel.send({"action": "accept_delivery", "path": started["path"], "task_id": started["task_id"]})
        accepted = channel.receive()
        assert accepted["event"] == "delivery" and accepted["turn"]["delivery_status"] == "accepted"
        assert Conversation.open(Path(started["path"])).turns[-1].delivery_status == "accepted"


def test_desktop_user_authorization_drives_real_edit_table_check_and_delivery(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("苹果 3\n梨子 2\n", encoding="utf-8")
    (folder / "amounts.csv").write_text("item,amount\na,0.10\nb,0.20\nc,-0.05\n", encoding="utf-8")
    script = folder / "check.py"
    script.write_text("import pathlib,sys\nassert pathlib.Path('notes.txt').read_text(encoding='utf-8') == '苹果 5\\n梨子 2\\n'\nprint('real-check-passed')\nprint('real-warning',file=sys.stderr)\n", encoding="utf-8")
    with connection(tmp_path / "records") as channel:
        channel.send({"action":"configure", "key":"delivery-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action":"send", "prompt":"精确改数量、算表格并检查", "scope":{"root":str(folder), "allowed_actions":["read","edit","calculate","check"], "provider":"deepseek", "recipients":["deepseek"], "checks":[{"name":"指定检查", "argv":[sys.executable,str(script)]}]}})
        actions = []
        while True:
            event = channel.receive()
            if event["event"] == "action":
                actions.append(event["action"])
            if event["event"] == "finished":
                break
        turn = event["turn"]
        assert [action["kind"] for action in actions if action["status"] == "started"] == ["read","edit","calculate","check"]
        assert (folder / "notes.txt").read_text(encoding="utf-8") == "苹果 5\n梨子 2\n"
        assert "0.25" in turn["answer"] and "real-check-passed" in turn["answer"]
        assert turn["checks_passed"] is True and turn["delivery_status"] == "awaiting_acceptance"
        assert turn["actions"][-1]["stderr"].strip() == "real-warning"
        assert Path(turn["actions"][1]["backup_path"]).read_text(encoding="utf-8") == "苹果 3\n梨子 2\n"
        assert Conversation.open(Path(event["path"])).turns[-1].checks_passed is True


def test_desktop_delivered_artifacts_are_live_saved_reopened_and_isolated_from_new_conversations(tmp_path: Path) -> None:
    folder = tmp_path / 'materials'
    folder.mkdir()
    records = tmp_path / 'records'
    with connection(records) as channel:
        channel.send({'action': 'configure', 'key': 'artifacts-controlled'})
        assert channel.receive()['event'] == 'configured'
        channel.send({'action': 'send', 'prompt': '创建报告并明确交付', 'scope': {
            'root': str(folder), 'allowed_actions': ['read', 'edit'], 'provider': 'deepseek', 'recipients': ['deepseek']}})
        delivered = None
        while True:
            event = channel.receive(timeout=15)
            if event['event'] == 'artifacts':
                delivered = event
            if event['event'] == 'finished':
                break
        assert delivered is not None
        path = event['path']
        artifact = event['turn']['artifacts'][0]
        assert delivered['artifacts'] == event['turn']['artifacts']
        assert delivered['path'] == path and delivered['task_id'] == artifact['task_id']
        source = next(item for item in delivered['turn']['actions'] if item['id'] == artifact['source_action_id'])
        assert source['status'] == source['effect'] == 'completed'
        assert source['file_changes'][0]['id'] == artifact['file_change_id']
        assert Path(artifact['path']).read_text(encoding='utf-8') == '实际交付正文\n'
        assert event['saved']
        channel.send({'action': 'new'})
        fresh = channel.receive()
        assert fresh['event'] == 'conversation' and fresh['path'] != path and fresh['turns'] == []
        channel.send({'action': 'open', 'path': path})
        reopened = channel.receive()
        assert reopened['event'] == 'conversation' and reopened['turns'][-1]['artifacts'] == [artifact]
    with connection(records) as channel:
        channel.send({'action': 'open', 'path': path})
        reopened = channel.receive()
        assert reopened['event'] == 'conversation' and reopened['turns'][-1]['artifacts'] == [artifact]


def test_desktop_stop_waits_for_real_check_tree_and_keeps_other_conversation(tmp_path: Path) -> None:
    from time import monotonic, sleep
    script = tmp_path / "long_check.py"
    script.write_text("import pathlib,subprocess,sys,time\nchild=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\npathlib.Path('child.pid').write_text(str(child.pid))\npathlib.Path('partial.txt').write_text('actual-change-before-stop')\nprint('real-check-started',flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    with connection(tmp_path / "records") as channel:
        channel.send({"action":"configure", "key":"check-stop-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action":"send", "prompt":"运行持续检查", "scope":{"root":str(tmp_path),"allowed_actions":["check"],"provider":"deepseek","recipients":["deepseek"],"checks":[{"name":"持续检查","argv":[sys.executable,str(script)]}]}})
        first = channel.receive()
        started = monotonic()
        while not (tmp_path / "child.pid").exists():
            assert monotonic() - started < 5
            sleep(.01)
        channel.send({"action":"new"})
        while (second := channel.receive())["event"] != "conversation":
            pass
        channel.send({"action":"send", "path":second["path"], "prompt":"另一聊天正常办理"})
        while (other := channel.receive())["event"] != "finished":
            pass
        assert other["path"] == second["path"] and other["turn"]["status"] == "completed"
        channel.send({"action":"stop", "path":first["path"]})
        stopping = channel.receive()
        assert stopping["event"] == "stopping" and stopping["path"] == first["path"]
        while (finished := channel.receive())["event"] != "finished":
            pass
        action = finished["turn"]["actions"][-1]
        assert finished["path"] == first["path"] and finished["turn"]["status"] == "cancelled"
        assert action["cleanup_complete"] and action["exit_code"] is not None
        assert "real-check-started" in action["stdout"]
        assert (tmp_path / "partial.txt").read_text() == "actual-change-before-stop"
        channel.send({"action":"shutdown"})
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0


def test_authorized_folder_is_read_on_demand_with_sources_and_saved_action_evidence(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "采购.md").write_text("采购金额800以上需审批。\n", encoding="utf-8")
    (folder / "报销.txt").write_text("报销需在30天内提交。\n", encoding="utf-8")
    (folder / "无关.txt").write_text("unrequested-material-must-not-be-sent", encoding="utf-8")
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "scope-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "比较采购和报销规定", "scope": {
            "root": str(folder), "allowed_actions": ["list", "read", "search"],
            "provider": "deepseek", "recipients": ["deepseek"],
        }})
        assert channel.receive()["event"] == "started"
        actions = []
        while (event := channel.receive())["event"] != "finished":
            if event["event"] == "action":
                actions.append(event["action"])
        turn = event["turn"]
        assert turn["status"] == "completed"
        assert "采购.md:1" in turn["answer"] and "采购金额800以上需审批。" in turn["answer"]
        assert "报销.txt:1" in turn["answer"] and "报销需在30天内提交。" in turn["answer"]
        assert "unrequested-material-must-not-be-sent" not in Path(event["path"]).read_text(encoding="utf-8")
        assert [action["status"] for action in actions] == ["started", "completed"] * 4
        assert {action["kind"] for action in actions} == {"list", "read", "search"}
        reopened = Conversation.open(Path(event["path"]))
        assert reopened.turns[-1].task.scope.recipients == ["deepseek"]
        assert len(reopened.turns[-1].actions) == 4


def test_desktop_refuses_an_additional_data_recipient_before_starting_work(tmp_path: Path) -> None:
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "scope-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "读取资料", "scope": {
            "root": str(tmp_path), "allowed_actions": ["read"],
            "provider": "deepseek", "recipients": ["deepseek", "unapproved-service"],
        }})
        assert channel.receive()["event"] == "error"
        assert list((tmp_path / "records").glob("*.json")) == []
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"


def test_desktop_long_material_keeps_sourced_ends_and_can_retrieve_the_omitted_middle(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    original = "\n".join(f"needle {number}: " + "详细规定" * 25 for number in range(1, 601))
    source = folder / "long.txt"
    source.write_text(original, encoding="utf-8")
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "context-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "搜索并回查第300行，保留未完事项", "scope": {
            "root": str(folder), "allowed_actions": ["read", "search"], "provider": "deepseek", "recipients": ["deepseek"],
        }})
        assert channel.receive()["event"] == "started"
        progress: list[str] = []
        while (event := channel.receive())["event"] != "finished":
            if event["event"] == "status":
                progress.append(event["text"])
        assert event["turn"]["status"] == "completed"
        assert "[long.txt:300]" in event["turn"]["answer"]
        assert any("省略" in text and "全文位置" in text for text in progress)
        saved = json.loads(Path(event["path"]).read_text(encoding="utf-8"))
        raw_results = [part["content"] for message in saved["messages"] for part in message["parts"] if part["part_kind"] == "tool-return"]
        assert "[long.txt:300]" in raw_results[0] and "[long.txt:600]" in raw_results[0]
        assert source.read_text(encoding="utf-8") == original


def test_stop_is_received_while_answer_runs_and_partial_output_is_saved(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-secret-never-save"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请停止"})
        assert channel.receive()["event"] == "started"
        assert channel.receive()["text"] == "第一段"
        channel.send({"action": "stop"})
        assert channel.receive()["event"] == "stopping"
        finished = channel.receive()
        assert finished["event"] == "finished"
        assert (finished["turn"]["status"], finished["turn"]["answer"], finished["saved"]) == (
            "cancelled", "第一段", True
        )
        assert Conversation.open(Path(finished["path"])).turns[-1].status == "cancelled"
        channel.send({"action": "send", "prompt": "现在继续"})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert (event["turn"]["status"], event["path"]) == ("completed", finished["path"])
        assert [turn.status for turn in Conversation.open(Path(event["path"])).turns] == ["cancelled", "completed"]


def test_closing_during_answer_saves_partial_output_and_exits(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-secret-never-save"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请停止并关闭"})
        channel.receive()
        assert channel.receive()["event"] == "text"
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "stopping"
        finished = channel.receive()
        assert finished["event"] == "finished" and finished["turn"]["status"] == "cancelled"
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0
        assert Conversation.open(Path(finished["path"])).turns[-1].answer == "第一段"


def test_failure_and_invalid_command_never_expose_connection_password(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "unknown", "key": "synthetic-secret-never-save"})
        invalid = channel.receive()
        assert invalid["event"] == "error" and "synthetic-secret" not in str(invalid)
        channel.send({"action": "configure", "key": "synthetic-secret-never-save"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请失败"})
        channel.receive()
        assert channel.receive()["event"] == "text"
        finished = channel.receive()
        assert finished["event"] == "finished" and finished["turn"]["status"] == "failed"
        assert finished["turn"]["answer"] == "第一段"
        assert "synthetic-secret" not in str(finished)
        assert "synthetic-secret" not in Path(finished["path"]).read_text(encoding="utf-8")


def test_record_write_failure_reports_unsaved_result(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("existing content", encoding="utf-8")
    with connection(blocked) as channel:
        channel.send({"action": "configure", "key": "synthetic-secret-never-save"})
        channel.receive()
        channel.send({"action": "send", "prompt": "你好"})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert event["saved"] is False
        assert event["turn"]["status"] == "failed"
        assert "未能保存" in event["turn"]["error"]
        assert blocked.read_text(encoding="utf-8") == "existing content"


def test_settings_reach_the_answer_service_and_can_change_without_reentering_key(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-secret-never-save", "delegation_max_turns": 3, "thinking": True})
        assert channel.receive() == {"event": "configured", "approval_mode": "request_approval", "trusted_projects": [], "configured": True, "provider_config": ProviderConfig().model_dump(), "request_limit": None, "delegation_max_turns": 3, "active_seconds_limit": None, "thinking": True}
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert snapshot["provider_config"] == ProviderConfig().model_dump()
        assert snapshot["request_limit"] is None
        assert snapshot["delegation_max_turns"] == 3
        assert snapshot["active_seconds_limit"] is None
        assert snapshot["thinking"] is True
        channel.send({"action": "send", "prompt": "检查思考设置"})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert (event["turn"]["answer"], event["turn"]["thinking"], event["saved"]) == ("额外思考已开启", True, True)
        channel.send({"action": "configure", "delegation_max_turns": 250, "thinking": False})
        assert channel.receive() == {"event": "configured", "approval_mode": "request_approval", "trusted_projects": [], "configured": True, "provider_config": ProviderConfig().model_dump(), "request_limit": None, "delegation_max_turns": 250, "active_seconds_limit": None, "thinking": False}
        channel.send({"action": "send", "prompt": "检查思考设置"})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert (event["turn"]["answer"], event["turn"]["thinking"], event["saved"]) == ("额外思考已关闭", False, True)
        assert [turn.thinking for turn in Conversation.open(Path(event["path"])).turns] == [True, False]


@pytest.mark.parametrize("key", ["synthetic-connect-failure", "synthetic-close-failure"])
def test_connection_failure_is_saved_and_the_same_conversation_can_continue(tmp_path: Path, key: str) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": key})
        channel.receive()
        channel.send({"action": "send", "prompt": "保留本次连接异常"})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert (event["turn"]["status"], event["saved"]) == ("failed", True)
        first = Conversation.open(Path(event["path"])).turns[-1]
        assert (first.prompt, first.status) == ("保留本次连接异常", "failed")
        assert key not in str(event)
        assert key not in Path(event["path"]).read_text(encoding="utf-8")
        channel.send({"action": "configure", "key": "synthetic-key-for-recovery"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "重新提问"})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert event["turn"]["status"] == "completed"
        assert [turn.status for turn in Conversation.open(Path(event["path"])).turns] == ["failed", "completed"]


def test_request_failure_evidence_survives_a_save_failure_and_user_can_retry(tmp_path: Path) -> None:
    directory = tmp_path / "record-location"
    with connection(directory) as channel:
        channel.send({"action": "configure", "key": "synthetic-secret-never-save"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请求后保存失败"})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert event["saved"] is False
        assert "RuntimeError" in event["turn"]["error"]
        assert "未能保存" in event["turn"]["error"]
        assert event["turn"]["answer"] == "第一段"
        assert "synthetic-secret" not in str(event)
        Path(event["path"]).chmod(0o600)
        channel.send({"action": "send", "prompt": "重新提问"})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert (event["saved"], event["turn"]["status"]) == (True, "completed")
        first, second = Conversation.open(Path(event["path"])).turns
        assert (first.status, first.answer, second.prompt) == ("failed", "第一段", "重新提问")
        assert first.error is not None and "RuntimeError" in first.error and "未能保存" in first.error


def test_saved_conversation_is_searchable_after_restart_and_opens_for_continuation(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        channel.receive()
        channel.send({"action": "send", "prompt": "采购规则：这是首次问题，标题只应使用开头的简短文字而不是整段长问题。"})
        while (finished := channel.receive())["event"] != "finished":
            pass
        original_path = finished["path"]
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0

    with connection(tmp_path) as channel:
        channel.send({"action": "list", "query": "采购"})
        listed = channel.receive()
        assert listed == {"event": "conversations", "items": [{
            "path": original_path, "title": "采购规则：这是首次问题，标题只应使用开头的简短文",
        }], "unreadable": 0}
        channel.send({"action": "list", "query": "不存在的标题"})
        assert channel.receive() == {"event": "conversations", "items": [], "unreadable": 0}
        channel.send({"action": "open", "path": original_path})
        opened = channel.receive()
        assert opened["event"] == "conversation" and opened["path"] == original_path
        assert opened["turns"][0]["answer"] == "第一段第二段"
        channel.send({"action": "configure", "key": "test-only"})
        channel.receive()
        channel.send({"action": "send", "prompt": "继续解释"})
        while (continued := channel.receive())["event"] != "finished":
            pass
        assert continued["path"] == original_path
        assert len(Conversation.open(Path(original_path)).turns) == 2
        channel.send({"action": "new"})
        created = channel.receive()
        assert created["event"] == "conversation" and created["turns"] == []
        assert created["path"] != original_path


def test_import_copies_old_history_and_continuation_preserves_tools_thinking_and_source(tmp_path: Path) -> None:
    source = tmp_path / "旧记录.json"
    record = {
        "version": 1,
        "messages": [
            {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "旧采购资料"}]},
            {"kind": "response", "provider_name": "deepseek", "provider_response_id": "old-response",
             "provider_details": {"finish_reason": "tool_calls"}, "parts": [
                {"part_kind": "thinking", "content": "先核对以前资料。", "provider_name": "deepseek"},
                {"part_kind": "tool-call", "tool_name": "read_authorized_text", "args": "{}", "tool_call_id": "read-1"},
            ]},
            {"kind": "request", "parts": [{"part_kind": "tool-return", "tool_name": "read_authorized_text",
                "content": "采购金额800以上需审批。", "tool_call_id": "read-1"}]},
            {"kind": "response", "provider_name": "deepseek", "parts": [{"part_kind": "text", "content": "800以上需审批。"}]},
        ],
        "turns": [{"prompt": "旧采购资料", "answer": "800以上需审批。", "status": "completed", "thinking": True}],
    }
    original = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    source.write_bytes(original)
    directory = tmp_path / "new-user-data"
    with connection(directory) as channel:
        channel.send({"action": "import", "path": str(source)})
        imported = channel.receive()
        assert imported["event"] == "conversation"
        copied_path = Path(imported["path"])
        assert copied_path.parent == directory and copied_path != source
        assert copied_path.read_bytes() == original
        assert imported["turns"][0]["answer"] == "800以上需审批。"
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0

    with connection(directory) as channel:
        channel.send({"action": "open", "path": str(copied_path)})
        assert channel.receive()["event"] == "conversation"
        channel.send({"action": "configure", "key": "test-only"})
        channel.receive()
        channel.send({"action": "send", "prompt": "检查旧资料"})
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["answer"] == "旧工具结果与思考都保留"
        assert len(Conversation.open(copied_path).turns) == 2
        copied_record = json.loads(copied_path.read_text(encoding="utf-8"))
        assert copied_record["messages"][1]["provider_response_id"] == "old-response"
        assert copied_record["messages"][1]["provider_details"] == {"finish_reason": "tool_calls"}
        assert copied_record["messages"][1]["parts"][1]["tool_call_id"] == "read-1"
    assert source.is_file() and source.read_bytes() == original


@pytest.mark.parametrize("original", [b"not-json", b'{"version":99,"messages":[],"turns":[]}', b'{"version":1,"messages":[],"turns":[{"prompt":"old","answer":"done","status":"completed"}]}'])
def test_import_rejects_broken_or_unsupported_records_without_changing_source(tmp_path: Path, original: bytes) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(original)
    directory = tmp_path / "data"
    with connection(directory) as channel:
        channel.send({"action": "import", "path": str(source)})
        error = channel.receive()
        assert error["event"] == "error" and "文件损坏、格式不受支持或无法读取" in error["message"]
        channel.send({"action": "list", "query": ""})
        assert channel.receive() == {"event": "conversations", "items": [], "unreadable": 0}
    assert source.is_file() and source.read_bytes() == original


def test_running_work_survives_failed_history_open_and_import(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请停止"})
        assert channel.receive()["event"] == "started"
        assert channel.receive()["event"] == "text"
        for command in [{"action": "open", "path": str(tmp_path / "absent.json")}, {"action": "import", "path": str(tmp_path / "absent.json")}]:
            channel.send(command)
            rejected = channel.receive()
            assert rejected["event"] == "error" and "无法打开" in rejected["message"]
        channel.send({"action": "stop"})
        assert channel.receive()["event"] == "stopping"
        finished = channel.receive()
        assert finished["event"] == "finished"
        channel.send({"action": "open", "path": finished["path"]})
        assert channel.receive()["event"] == "conversation"
        channel.send({"action": "import", "path": finished["path"]})
        imported = channel.receive()
        assert imported["event"] == "conversation" and imported["path"] != finished["path"]
        channel.send({"action": "new"})
        assert channel.receive()["turns"] == []


def test_unreadable_local_record_is_reported_without_hiding_good_history(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("not-json", encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text('{"version":1,"messages":[],"turns":[]}', encoding="utf-8")
    with connection(tmp_path) as channel:
        channel.send({"action": "list", "query": ""})
        assert channel.receive() == {"event": "conversations", "items": [{"path": str(good), "title": "新会话"}], "unreadable": 1}
        channel.send({"action": "open", "path": str(broken)})
        assert channel.receive()["event"] == "error"
    assert broken.read_text(encoding="utf-8") == "not-json"


def test_failed_import_copy_explains_failure_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    original = b'{"version":1,"messages":[],"turns":[]}'
    source.write_bytes(original)
    blocked = tmp_path / "blocked-directory"
    blocked.write_text("existing unrelated file", encoding="utf-8")
    with connection(blocked) as channel:
        channel.send({"action": "import", "path": str(source)})
        rejected = channel.receive()
        assert rejected["event"] == "error" and "未能复制导入记录" in rejected["message"]
    assert source.read_bytes() == original
    assert blocked.read_text(encoding="utf-8") == "existing unrelated file"


def test_selected_document_reaches_the_real_read_tool_and_saved_history(tmp_path: Path) -> None:
    document = tmp_path / "采购规则.md"
    original = "单笔采购金额超过800元，需要负责人审批。\n单笔采购金额为800元或更少，无需审批。"
    document.write_text(original, encoding="utf-8")
    unrelated = tmp_path / "不允许读取.txt"
    unrelated.write_text("不得发送的其他资料", encoding="utf-8")
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "document-controlled"})
        channel.receive()
        channel.send({"action": "send", "prompt": "800元及801元需要审批吗？", "document": str(document)})
        assert channel.receive()["event"] == "started"
        events = []
        while True:
            event = channel.receive()
            events.append(event)
            if event["event"] == "finished":
                break
        assert (event["turn"]["status"], event["turn"]["answer"], event["saved"]) == ("completed", original, True)
        saved = Path(event["path"]).read_text(encoding="utf-8")
        assert "read_authorized_text" in saved and original.splitlines()[0] in saved
        assert "不得发送的其他资料" not in saved
        assert any(item["event"] == "status" and item["text"] == "正在读取授权文本" for item in events)
        assert document.read_text(encoding="utf-8") == original
        assert unrelated.read_text(encoding="utf-8") == "不得发送的其他资料"


@pytest.mark.parametrize("kind", ["missing.txt", "unsupported.pdf", "directory.md"])
def test_invalid_document_is_rejected_clearly_without_starting_a_reply(tmp_path: Path, kind: str) -> None:
    document = tmp_path / kind
    if kind == "unsupported.pdf":
        document.write_bytes(b"unsupported-original")
    elif kind == "directory.md":
        document.mkdir()
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "document-controlled"})
        channel.receive()
        channel.send({"action": "send", "prompt": "读取资料", "document": str(document)})
        event = channel.receive()
        assert event["event"] == "error" and "所选资料" in event["message"]
        assert ".txt" in event["message"] and ".md" in event["message"]
        if kind == "unsupported.pdf":
            assert document.read_bytes() == b"unsupported-original"


def test_invalid_text_encoding_preserves_question_and_original_file(tmp_path: Path) -> None:
    document = tmp_path / "旧格式.txt"
    original = "采购规则需要审批。".encode("gbk")
    document.write_bytes(original)
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "document-controlled"})
        channel.receive()
        channel.send({"action": "send", "prompt": "这份规则怎么规定？", "document": str(document)})
        assert channel.receive()["event"] == "started"
        finished = channel.receive()
        assert (finished["event"], finished["turn"]["status"], finished["saved"]) == ("finished", "failed", True)
        assert "UTF-8" in finished["turn"]["error"]
        assert Conversation.open(Path(finished["path"])).turns[-1].prompt == "这份规则怎么规定？"
        assert document.read_bytes() == original


def test_attached_document_uses_the_existing_stop_and_save_flow(tmp_path: Path) -> None:
    document = tmp_path / "规则.txt"
    document.write_text("这是授权资料。", encoding="utf-8")
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "synthetic-secret-never-save"})
        channel.receive()
        channel.send({"action": "send", "prompt": "读取资料后请停止", "document": str(document)})
        assert channel.receive()["event"] == "started"
        assert channel.receive()["text"] == "第一段"
        channel.send({"action": "stop"})
        assert channel.receive()["event"] == "stopping"
        finished = channel.receive()
        assert (finished["turn"]["status"], finished["turn"]["answer"], finished["saved"]) == ("cancelled", "第一段", True)
        assert Conversation.open(Path(finished["path"])).turns[-1].status == "cancelled"
        assert document.read_text(encoding="utf-8") == "这是授权资料。"


@pytest.mark.parametrize("request_limit", [None, 1, 3])
def test_limit_stops_real_tool_rounds_until_user_sends_the_next_message(tmp_path: Path, request_limit: int | None) -> None:
    document = tmp_path / "规则.txt"
    document.write_text("800元以内无需审批。", encoding="utf-8")
    expected = 10 if request_limit is None else request_limit
    legacy = legacy_queued_conversation(tmp_path / "records" / "legacy.json", "反复读取", expected, document=document)
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "limit-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": str(legacy)})
        assert channel.receive()["event"] == "conversation"
        channel.send({"action": "start_next"})
        reads = 0
        while True:
            event = channel.receive()
            if event["event"] == "status":
                reads += 1
            if event["event"] == "finished":
                break
        assert (event["turn"]["status"], event["turn"]["requests"], reads, event["saved"]) == ("limited", expected, expected, True)
        assert event["turn"]["thinking"] is False
        assert f"{expected}次请求尝试上限" in event["turn"]["error"]
        first_path = Path(event["path"])
        assert Conversation.open(first_path).turns[-1].status == "limited"
        channel.send({"action": "send", "prompt": "现在继续"})
        refused = channel.receive()
        assert refused["event"] == "error" and "旧记录" in refused["message"]
        assert len(Conversation.open(first_path).turns) == 1
        channel.send({"action": "send", "prompt": "明确另办新工作", "new_task": True})
        while True:
            event = channel.receive()
            if event["event"] == "finished":
                break
        assert (event["turn"]["status"], event["turn"]["answer"], Path(event["path"])) == ("completed", "由你决定后继续了。", first_path)
        assert [turn.status for turn in Conversation.open(first_path).turns] == ["limited", "completed"]
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0

def test_two_conversations_run_independently_and_running_snapshot_keeps_ownership(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请停止"})
        first = channel.receive()
        assert first["event"] == "started"
        assert channel.receive()["event"] == "text"
        first_path = first["path"]
        channel.send({"action": "new"})
        second = channel.receive()
        assert second["event"] == "conversation" and second["path"] != first_path
        channel.send({"action": "send", "prompt": "另一聊天"})
        assert channel.receive()["event"] == "started"
        channel.send({"action": "stop", "path": first_path})
        seen = []
        while len([event for event in seen if event["event"] == "finished"]) < 2:
            seen.append(channel.receive())
        finished = {event["path"]: event for event in seen if event["event"] == "finished"}
        assert finished[first_path]["turn"]["status"] == "cancelled"
        assert finished[first_path]["turn"]["answer"] == "第一段"
        assert finished[second["path"]]["turn"]["answer"] == "第一段第二段"
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        assert snapshot["event"] == "snapshot"
        assert len(snapshot["conversations"]) == 2
        assert "key" not in str(snapshot)

def test_supplements_are_acknowledged_then_adopted_and_new_work_waits_in_a_saved_queue(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        channel.receive()
        channel.send({"action": "send", "prompt": "请停止"})
        started = channel.receive()
        assert started["event"] == "started"
        assert channel.receive()["event"] == "text"
        channel.send({"action": "send", "prompt": "只补充这一项"})
        received = channel.receive()
        assert received["event"] == "supplement" and received["state"] == "received"
        channel.send({"action": "send", "prompt": "后续新工作", "new_task": True})
        queued = channel.receive()
        assert queued["event"] == "queue" and queued["queue"][0]["task"]["goal"] == "后续新工作"
        channel.send({"action": "stop"})
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["task"]["supplements"][0]["applied_request"] is None
        channel.send({"action": "open", "path": finished["path"]})
        opened = channel.receive()
        assert opened["queue"][0]["task"]["goal"] == "后续新工作"
        channel.send({"action": "start_next"})
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["prompt"] == "后续新工作"
        assert finished["turn"]["status"] == "completed"


def test_steering_queued_text_preserves_queue_on_save_failure_and_saves_once(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "请停止"})
        started = channel.receive()
        assert started["event"] == "started"
        assert channel.receive()["event"] == "text"
        channel.send({"action": "send", "prompt": "等本项结束再办", "new_task": True})
        original_queue = channel.receive()["queue"]
        guidance = "先核对已收到的文字"
        channel.send({"action": "send", "prompt": guidance, "new_task": True})
        queued = channel.receive()["queue"]
        queued_id = queued[1]["task"]["id"]
        path = Path(started["path"])
        previous_record = path.read_bytes()
        command = {"action": "steer_queued", "path": started["path"], "task_id": started["task_id"], "queued_task_id": queued_id}

        blocked_write = path.with_suffix(".json.tmp")
        blocked_write.mkdir()
        try:
            channel.send(command)
            assert channel.receive()["event"] == "error"
            channel.send({"action": "snapshot"})
            snapshot = channel.receive()["conversations"][0]
            assert snapshot["queue"] == queued
            assert snapshot["turns"][-1]["task"]["supplements"] == []
            assert path.read_bytes() == previous_record
        finally:
            blocked_write.rmdir()

        channel.send(command)
        events = {event["event"]: event for event in (channel.receive(), channel.receive())}
        assert events["supplement"]["task_id"] == started["task_id"]
        assert events["supplement"]["supplement"]["id"] == queued_id
        assert events["supplement"]["supplement"]["text"] == guidance
        assert events["supplement"]["saved"] is True
        assert events["queue"]["queue"] == original_queue

        channel.send(command)
        returned = channel.receive()
        assert returned == events["supplement"]
        channel.send({"action": "snapshot"})
        while (snapshot_event := channel.receive())["event"] == "queue":
            assert snapshot_event["queue"] == original_queue
        snapshot = snapshot_event["conversations"][0]
        assert snapshot["running"] is True
        assert snapshot["queue"] == original_queue
        assert len(snapshot["turns"]) == 1
        assert [item["id"] for item in snapshot["turns"][-1]["task"]["supplements"]] == [queued_id]

        record = ConversationRecord.model_validate_json(path.read_bytes())
        assert [item.task.id for item in record.queue] == [original_queue[0]["task"]["id"]]
        assert len(record.turns) == 1 and record.turns[-1].task is not None
        assert [(item.id, item.text) for item in record.turns[-1].task.supplements] == [(queued_id, guidance)]


@pytest.mark.parametrize("blocked_by", ["stale-work", "different-folder"])
def test_steering_refuses_stale_work_or_different_folder_with_the_queue_intact(tmp_path: Path, blocked_by: str) -> None:
    folder = tmp_path / "another-folder"
    folder.mkdir()
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "请停止"})
        started = channel.receive()
        assert started["event"] == "started"
        assert channel.receive()["event"] == "text"
        send: dict[str, object] = {"action": "send", "prompt": "保留在队列的新工作", "new_task": True}
        if blocked_by == "different-folder":
            send["scope"] = {"root": str(folder), "allowed_actions": ["read"], "provider": "deepseek", "recipients": ["deepseek"]}
        channel.send(send)
        queue = channel.receive()["queue"]
        path = Path(started["path"])
        previous_record = path.read_bytes()
        task_id = started["task_id"] if blocked_by == "different-folder" else "stale-work"
        channel.send({"action": "steer_queued", "path": started["path"], "task_id": task_id, "queued_task_id": queue[0]["task"]["id"]})
        error = channel.receive()
        assert error["event"] == "error" and "引导" in error["message"]

        channel.send({"action": "snapshot"})
        snapshot = channel.receive()["conversations"][0]
        assert snapshot["queue"] == queue
        assert snapshot["turns"][-1]["task"]["supplements"] == []
        assert path.read_bytes() == previous_record


def test_steering_applies_on_the_current_tasks_next_request(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    target = folder / ".agents" / "notes.txt"
    target.parent.mkdir(parents=True)
    target.write_text("keep-me", encoding="utf-8")
    scope = {"root": str(folder), "allowed_actions": ["read"], "provider": "deepseek", "recipients": ["deepseek"]}
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "actions-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "删除后再提议删除", "scope": scope})
        started = channel.receive()
        while (event := channel.receive())["event"] != "action" or event["action"]["status"] != "awaiting_approval":
            pass
        first = event["action"]

        channel.send({"action": "send", "path": started["path"], "prompt": "核对原件，请保留文件", "scope": scope, "new_task": True})
        queued = channel.receive()
        queued_id = queued["queue"][0]["task"]["id"]
        channel.send({"action": "steer_queued", "path": started["path"], "task_id": started["task_id"], "queued_task_id": queued_id})
        received = channel.receive()
        assert received["event"] == "supplement" and received["state"] == "received"
        assert channel.receive()["queue"] == []

        def deny(action: dict) -> None:
            channel.send({"action": "approve_action", "path": started["path"], "task_id": started["task_id"],
                "action_id": action["id"], "approval_id": action["approval_id"], "approved": False})

        deny(first)
        while (event := channel.receive())["event"] != "action" or event["action"]["status"] != "awaiting_approval":
            assert event["event"] != "started"
        deny(event["action"])
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["task_id"] == started["task_id"]
        supplement, = finished["turn"]["task"]["supplements"]
        assert supplement["id"] == queued_id and supplement["applied_request"] == 2
        assert target.read_text(encoding="utf-8") == "keep-me"


def test_supplement_retries_and_revocations_keep_exact_identity_across_reopen(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "请停止"})
        started = channel.receive()
        assert started["event"] == "started"
        assert channel.receive()["event"] == "text"
        address = {"path": started["path"], "task_id": started["task_id"]}
        first = {**address, "supplement_id": "first"}
        second = {**address, "supplement_id": "second"}
        submit = {"action": "submit_supplement", **first, "prompt": "同一段补充"}
        channel.send(submit)
        received = channel.receive()
        assert (received["event"], received["state"], received["saved"], received["supplement"]["id"]) == (
            "supplement", "received", True, "first")
        channel.send(submit)
        assert channel.receive() == received
        channel.send({**submit, "prompt": "同一段补充 "})
        conflict = channel.receive()
        assert (conflict["event"], conflict["supplement_id"], conflict["task_id"]) == ("error", "first", address["task_id"])
        channel.send({**submit, "path": str(tmp_path / "wrong.json")})
        wrong = channel.receive()
        assert wrong["event"] == "error" and wrong["supplement_id"] == "first"
        channel.send({**submit, "task_id": "a-different-task"})
        stale = channel.receive()
        assert (stale["event"], stale["task_id"], stale["supplement_id"]) == ("error", "a-different-task", "first")

        channel.send({**submit, **second})
        assert channel.receive()["supplement"]["id"] == "second"
        revoke = {"action": "revoke_supplement", **first}
        channel.send(revoke)
        revoked = channel.receive()
        assert revoked["state"] == "revoked" and revoked["saved"] is True
        assert revoked["supplement"]["revoked_at"]
        channel.send(revoke)
        assert channel.receive() == revoked
        channel.send({"action": "stop", "path": address["path"]})
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["status"] == "cancelled"
        assert finished["turn"]["task"]["requests"] == 1
        assert [item["id"] for item in finished["turn"]["task"]["supplements"]] == ["first", "second"]
        assert all(item["applied_request"] is None for item in finished["turn"]["task"]["supplements"])
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"

    with connection(tmp_path) as reopened:
        reopened.send({"action": "open", "path": address["path"]})
        assert reopened.receive()["event"] == "conversation"
        reopened.send(submit)
        assert reopened.receive() == revoked
        reopened.send({"action": "revoke_supplement", **second})
        second_revoked = reopened.receive()
        assert second_revoked["state"] == "revoked" and second_revoked["saved"] is True
        reopened.send({**submit, "supplement_id": "late"})
        late = reopened.receive()
        assert (late["event"], late["supplement_id"]) == ("error", "late")
        reopened.send({"action": "shutdown"})
        assert reopened.receive()["event"] == "closed"

    conversation = Conversation.open(Path(address["path"]))
    assert len(conversation.turns) == 1
    assert conversation.turns[0].task is not None
    assert [item.id for item in conversation.turns[0].task.supplements] == ["first", "second"]
    assert all(item.revoked_at and item.applied_request is None for item in conversation.turns[0].task.supplements)


def test_failed_supplement_saves_leave_the_last_persisted_state(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "请停止"})
        started = channel.receive()
        assert started["event"] == "started"
        assert channel.receive()["event"] == "text"
        address = {"path": started["path"], "task_id": started["task_id"], "supplement_id": "one"}
        pending_write = Path(started["path"]).with_suffix(".json.tmp")
        submit = {"action": "submit_supplement", **address, "prompt": "待保存的补充"}
        pending_write.mkdir()
        try:
            channel.send(submit)
            failure = channel.receive()
            assert (failure["event"], failure["supplement_id"], failure["task_id"]) == ("error", "one", address["task_id"])
            channel.send({"action": "snapshot"})
            assert channel.receive()["conversations"][0]["turns"][-1]["task"]["supplements"] == []
        finally:
            pending_write.rmdir()

        channel.send(submit)
        received = channel.receive()
        assert received["state"] == "received" and received["saved"] is True
        revoke = {"action": "revoke_supplement", **address}
        pending_write.mkdir()
        try:
            channel.send(revoke)
            failure = channel.receive()
            assert (failure["event"], failure["supplement_id"], failure["task_id"]) == ("error", "one", address["task_id"])
            channel.send(submit)
            assert channel.receive() == received
        finally:
            pending_write.rmdir()

        channel.send(revoke)
        assert channel.receive()["state"] == "revoked"
        channel.send({"action": "stop", "path": address["path"]})
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["task"]["supplements"][0]["revoked_at"]
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"


def test_user_increases_a_limited_task_budget_without_resetting_used_requests(tmp_path: Path) -> None:
    document = tmp_path / "rule.txt"
    document.write_text("真实资料", encoding="utf-8")
    legacy = legacy_queued_conversation(tmp_path / "records" / "legacy.json", "反复读取", 1, document=document)
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "limit-controlled"})
        channel.receive()
        channel.send({"action": "open", "path": str(legacy)})
        assert channel.receive()["event"] == "conversation"
        channel.send({"action": "start_next"})
        while (limited := channel.receive())["event"] != "finished":
            pass
        assert limited["turn"]["task"]["requests"] == 1
        identity = limited["turn"]["task"]["id"]
        channel.send({"action": "continue_task", "task_id": identity, "request_limit": 1})
        rejected = channel.receive()
        assert rejected["event"] == "error" and "须大于已用次数" in rejected["message"]
        channel.send({"action": "continue_task", "task_id": identity, "request_limit": 2, "active_seconds_limit": 600})
        first = channel.receive()
        assert first["event"] == "started"
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["task"]["id"] == identity
        assert finished["turn"]["task"]["requests"] == 2
        assert finished["turn"]["answer"] == "由你决定后继续了。"

def test_a_connection_failure_keeps_the_task_authorization_and_identity_without_actions(tmp_path: Path) -> None:
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "configure", "key": "synthetic-connect-failure"})
        channel.receive()
        scope = {"root": str(tmp_path), "allowed_actions": ["read"], "provider": "deepseek", "recipients": ["deepseek"]}
        channel.send({"action": "send", "prompt": "连接前也保留允许范围", "scope": scope})
        started = channel.receive()
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["status"] == "failed"
        task = finished["turn"]["task"]
        assert task["id"] == started["task_id"] and task["scope"] == {**scope, "checks": []}
        assert task["requests"] == 0 and finished["turn"]["actions"] == []
        channel.send({"action": "open", "path": finished["path"]})
        opened = channel.receive()
        assert opened["turns"][-1]["task"] == task
        assert "synthetic-connect-failure" not in Path(finished["path"]).read_text(encoding="utf-8")

def test_saved_queue_and_stopped_progress_reopen_after_service_restart_without_restarting_work(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "test-only", "active_seconds_limit": 0.12})
        channel.receive()
        channel.send({"action": "send", "prompt": "请停止"})
        assert channel.receive()["event"] == "started"
        assert channel.receive()["event"] == "text"
        channel.send({"action": "send", "prompt": "仍需办理", "new_task": True})
        assert channel.receive()["event"] == "queue"
        channel.send({"action": "stop"})
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["status"] == "cancelled"
        original = finished["turn"]["task"]
        path = finished["path"]
        channel.send({"action": "shutdown"})
        assert channel.receive()["event"] == "closed"
        assert channel.process.wait(timeout=5) == 0
    with connection(tmp_path) as channel:
        channel.send({"action": "open", "path": path})
        opened = channel.receive()
        assert opened["running"] is False
        assert opened["queue"][0]["task"]["goal"] == "仍需办理"
        assert opened["turns"][-1]["task"] == original
        assert opened["turns"][-1]["status"] == "cancelled"
        assert original["requests"] == 1 and original["active_seconds_limit"] is None
