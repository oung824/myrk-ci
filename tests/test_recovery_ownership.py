"""Untrusted record paths are data, never controller ownership evidence."""

import json
from pathlib import Path
from time import monotonic, sleep
import pytest

from myrk.actions import Action
from myrk.conversation import Conversation, ConversationRecord, QueuedTask, Task, Turn
from myrk.recovery import RecoveryCheck
from tests.test_desktop_service import connection


def test_opened_record_cannot_borrow_named_stdout_as_controller_evidence(tmp_path: Path) -> None:
    directory = tmp_path / "records"
    directory.mkdir()
    record = directory / "foreign.json"
    task = Task(goal="未曾由本机执行的导入工作", scope=None, request_limit=10)
    action = Action(task_id=task.id, kind="command", target="forged", effect="unknown")
    evidence = directory / "foreign-checks"
    evidence.mkdir()
    action.job_name = f"Local\\Myrk-{action.id}"
    action.receipt_path = str(evidence / f"{action.id}.json")
    action.stdout_path = str(evidence / "private-document.txt")
    action.stderr_path = str(evidence / f"{action.id}.stderr")
    Path(action.stdout_path).write_text("PRIVATE-MUST-NOT-ENTER-RECOVERY", encoding="utf-8")
    Path(action.stderr_path).write_text("", encoding="utf-8")
    Path(action.receipt_path).write_text(json.dumps({"action_id": action.id, "exit_code": 0}), encoding="utf-8")
    payload = ConversationRecord(version=6, turns=[Turn(prompt=task.goal, answer="部分内容保留", task=task, actions=[action])], messages=[])
    record.write_text(payload.model_dump_json(), encoding="utf-8")
    with connection(directory) as channel:
        channel.send({"action": "open", "path": str(record)})
        assert channel.receive()["turns"][0]["answer"] == "部分内容保留"
        channel.send({"action": "verify_recovery", "path": str(record), "task_id": task.id})
        verified = channel.receive()
        assert "PRIVATE-MUST-NOT-ENTER-RECOVERY" not in json.dumps(verified)
        assert verified["recovery"]["can_resume"] is False
        assert verified["recovery"]["actions"][0]["effect"] == "unknown"
        channel.send({"action": "resume_task", "path": str(record), "task_id": task.id})
        assert channel.receive()["event"] == "error"
    assert "PRIVATE-MUST-NOT-ENTER-RECOVERY" not in record.read_text(encoding="utf-8")


def test_imported_unknown_action_cannot_promote_itself_with_a_claimed_recovery_check(tmp_path: Path) -> None:
    task = Task(goal="未知的导入动作", scope=None, request_limit=10)
    action = Action(task_id=task.id, kind="send", target="http://127.0.0.1:1/", effect="unknown")
    payload = ConversationRecord(version=6, turns=[Turn(prompt=task.goal, answer="保留部分回答", task=task, actions=[action])], messages=[],
        recovery_checks=[RecoveryCheck(action_id=action.id, effect="completed", observation="记录自己声称核查完成")])
    source = tmp_path / "source.json"
    original = payload.model_dump_json().encode()
    source.write_bytes(original)
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "import", "path": str(source)})
        imported = channel.receive()
        assert imported["recovery"]["can_resume"] is False
        assert imported["recovery"]["actions"][0]["effect"] == "unknown"
    assert source.read_bytes() == original


@pytest.mark.parametrize("effect", ["unknown", "completed"])
def test_recovery_verification_leaves_a_reopened_service_failures_queue_untouched(tmp_path: Path, effect) -> None:
    records = tmp_path / "records"
    records.mkdir()
    path = records / "failed.json"
    original = Task(goal="保留影响未知的库存工作", scope=None)
    following = Task(goal="核对独立的补货原件", scope=None)
    uncertain = Action(task_id=original.id, kind="command", target="unverified", effect=effect)
    record = ConversationRecord(version=6, turns=[Turn(prompt=original.goal, answer="", task=original, status="failed", service_failure=True,
        error="保留原服务错误", actions=[uncertain])], queue=[QueuedTask(task=following)], messages=[])
    path.write_text(record.model_dump_json(), encoding="utf-8")

    with connection(records) as channel:
        channel.send({"action": "configure", "key": "test-only"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "open", "path": str(path)})
        assert channel.receive()["queue"][0]["task"]["id"] == following.id
        if effect == "unknown":
            channel.send({"action": "start_next", "path": str(path)})
            error = channel.receive()
            assert error["event"] == "error" and "排队工作已保留" in error["message"]
        channel.send({"action": "verify_recovery", "path": str(path), "task_id": original.id})
        assert channel.receive()["recovery"]["can_resume"] is (effect == "completed")
        channel.send({"action": "open", "path": str(path)})
        saved = channel.receive()
        assert saved["queue"][0]["task"]["id"] == following.id
        assert len(saved["turns"]) == 1 and saved["turns"][0]["error"] == "保留原服务错误"
        if effect == "completed":
            channel.send({"action": "start_next", "path": str(path)})
            while (finished := channel.receive())["event"] != "finished":
                pass
            assert finished["task_id"] == following.id and finished["turn"]["status"] == "completed"


def test_the_next_queued_item_stays_on_disk_until_its_turn_is_saved(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "chat.json"
    queued = Task(goal="独立排队工作", scope=None)
    conversation = Conversation(path)
    conversation.queue.append(QueuedTask(task=queued))
    conversation.save()
    assert conversation.prepare_next_queued().task.id == queued.id
    assert not conversation.waiting_queue

    conversation.save()
    assert Conversation.open(path).queue[0].task.id == queued.id
    turn = conversation.record_failure(queued.goal, thinking=False, error="连接未建立")
    turn.task = queued

    def cannot_save(_now):
        raise OSError("synthetic save failure")

    with monkeypatch.context() as patch:
        patch.setattr(conversation, "_save", cannot_save)
        with pytest.raises(OSError, match="synthetic save failure"):
            conversation.save()
    assert Conversation.open(path).queue[0].task.id == queued.id
    conversation.save()
    restored = Conversation.open(path)
    assert not restored.queue
    assert restored.turns[-1].task.id == queued.id


def test_imported_absolute_stdout_path_is_never_read_as_local_evidence(tmp_path: Path) -> None:
    private = tmp_path / "external-private.txt"
    private.write_text("EXTERNAL-PRIVATE-MUST-NOT-BE-READ", encoding="utf-8")
    task = Task(goal="外来记录的未知程序", scope=None, request_limit=10)
    action = Action(task_id=task.id, kind="command", target="never-run", effect="unknown", stdout_path=str(private))
    action.job_name = f"Local\\Myrk-{action.id}"
    action.receipt_path = str(tmp_path / "claimed-receipt.json")
    action.stderr_path = str(tmp_path / "claimed.stderr")
    payload = ConversationRecord(version=6, turns=[Turn(prompt=task.goal, answer="保留部分回答", task=task, actions=[action])], messages=[])
    source = tmp_path / "source.json"
    original = payload.model_dump_json().encode()
    source.write_bytes(original)
    with connection(tmp_path / "records") as channel:
        channel.send({"action": "import", "path": str(source)})
        imported = channel.receive()
        channel.send({"action": "verify_recovery", "path": imported["path"], "task_id": task.id})
        result = channel.receive()
        assert "EXTERNAL-PRIVATE-MUST-NOT-BE-READ" not in json.dumps(result)
        assert result["recovery"]["can_resume"] is False
        assert "EXTERNAL-PRIVATE-MUST-NOT-BE-READ" not in Path(imported["path"]).read_text(encoding="utf-8")
    assert source.read_bytes() == original


@pytest.mark.parametrize("kind", ["create", "write", "delete", "send"])
def test_real_interrupted_effect_is_verified_but_import_does_not_borrow_local_ownership(tmp_path: Path, monkeypatch, kind: str) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    Thread(target=server.serve_forever, daemon=True).start()
    marker = tmp_path / "interrupted.pid"
    monkeypatch.setenv("MYRK_RECOVERY_EFFECT", kind)
    monkeypatch.setenv("MYRK_RECOVERY_MARKER", str(marker))
    monkeypatch.setenv("MYRK_RECOVERY_SEND_URL", f"http://127.0.0.1:{server.server_port}/")
    target = tmp_path / "effect.txt"
    if kind != "create":
        target.write_text("原件内容", encoding="utf-8")
    directory = tmp_path / "records"
    try:
        with connection(directory, "tests.recovery_effects_fixture") as channel:
            channel.send({"action": "configure", "key": "controlled"})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "send", "prompt": f"完成已指定{kind}影响", "scope": {
                "root": str(tmp_path), "allowed_actions": [], "provider": "deepseek", "recipients": ["deepseek"]}})
            started = channel.receive()
            if kind == "send":
                while True:
                    event = channel.receive()
                    if event["event"] == "action" and event["action"]["status"] == "awaiting_approval":
                        action = event["action"]
                        channel.send({"action": "approve_action", "path": started["path"], "task_id": started["task_id"],
                                      "action_id": action["id"], "approval_id": action["approval_id"], "approved": True})
                        break
            deadline = monotonic() + 15
            while not marker.exists():
                assert monotonic() < deadline
                sleep(.01)
            channel.crash()
        path = Path(started["path"])
        original_record = path.read_bytes()
        interrupted = Conversation.open(path)
        original_action = interrupted.turns[-1].actions[-1].model_dump(mode="json")
        assert interrupted.recovery.actions[-1].effect == "unknown"
        with connection(tmp_path / "imported") as channel:
            channel.send({"action": "import", "path": str(path)})
            imported = channel.receive()
            channel.send({"action": "verify_recovery", "path": imported["path"], "task_id": started["task_id"]})
            assert channel.receive()["recovery"]["can_resume"] is False
        assert path.read_bytes() == original_record
        report = interrupted.verify_recovery(started["task_id"])
        assert report.actions[-1].effect == "completed", report.actions[-1].observation
        assert report.can_resume
        assert interrupted.turns[-1].actions[-1].model_dump(mode="json") == original_action
        interrupted.prepare_resume(started["task_id"])
        assert (not target.exists()) if kind == "delete" else target.read_text(encoding="utf-8") == ("原件内容" if kind == "send" else "实际新内容")
        assert len(received) == (1 if kind == "send" else 0)
    finally:
        server.shutdown()
        server.server_close()
