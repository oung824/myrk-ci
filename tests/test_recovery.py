"""Recovery is exercised through a new service process and real saved files."""

from pathlib import Path
import asyncio
import json
import os
import signal
import sys
from time import monotonic, sleep
import pytest

from tests.test_desktop_service import connection
from myrk.conversation import Conversation
from pydantic_ai.models.test import TestModel


def test_interrupted_generated_command_uses_its_exit_receipt_without_rerunning(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "notes.txt").write_text("苹果 3\n", encoding="utf-8")
    (tmp_path / "check.py").write_text("from pathlib import Path\np=Path('runs.txt')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nprint('completed-generated-command-proof')\n", encoding="utf-8")
    marker = tmp_path / "command-interrupted.pid"
    monkeypatch.setenv("MYRK_RECOVERY_POINT", "after_command")
    monkeypatch.setenv("MYRK_RECOVERY_MARKER", str(marker))
    monkeypatch.setenv("MYRK_RECOVERY_GENERATED", "1")
    with connection(tmp_path / "records", "tests.recovery_fixture") as channel:
        channel.send({"action": "configure", "key": "controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "改写后运行自拟命令", "scope": {"root": str(tmp_path), "allowed_actions": [], "provider": "deepseek", "recipients": ["deepseek"]}})
        started = channel.receive()
        deadline = monotonic() + 10
        while not marker.exists():
            assert monotonic() < deadline
            sleep(.01)
        channel.crash()
    conversation = Conversation.open(Path(started["path"]))
    assert conversation.recovery.actions[-1].effect == "unknown"
    verified = conversation.verify_recovery(started["task_id"])
    assert verified.can_resume and verified.actions[-1].effect == "completed"
    assert "completed-generated-command-proof" in verified.actions[-1].observation
    resumed = asyncio.run(conversation.resume_task(started["task_id"], model=TestModel(call_tools=[], custom_output_text="核查后继续，未重做命令。")))
    assert resumed.status == "completed" and (tmp_path / "runs.txt").read_text() == "x"
    assert not any(action.kind == "command" for action in resumed.actions)


def test_verified_unknown_edit_can_be_reviewed_for_a_new_receiver_without_replaying(tmp_path: Path, monkeypatch) -> None:
    from myrk.provider import ProviderConfig
    target = tmp_path / "notes.txt"
    target.write_text("苹果 3\n", encoding="utf-8")
    marker = tmp_path / "interrupted.pid"
    monkeypatch.setenv("MYRK_RECOVERY_POINT", "after_edit")
    monkeypatch.setenv("MYRK_RECOVERY_MARKER", str(marker))
    directory = tmp_path / "records"
    with connection(directory, "tests.recovery_fixture") as channel:
        channel.send({"action": "configure", "key": "controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "先改写", "scope": {"root": str(tmp_path), "allowed_actions": ["read", "edit"], "provider": "deepseek", "recipients": ["deepseek"]}})
        started = channel.receive()
        deadline = monotonic() + 7
        while not marker.exists():
            assert monotonic() < deadline
            sleep(.01)
        channel.crash()
    conversation = Conversation.open(Path(started["path"]))
    config = ProviderConfig(name="另一地址", base_url="http://127.0.0.1:9001/v1", model="controlled", protocol="openai-chat")
    refused = conversation.preview_provider_change(started["task_id"], config)
    assert not refused.compatible and "未知" in refused.reason
    with pytest.raises(ValueError):
        conversation.decide_provider_change(refused.id, True)
    assert conversation.verify_recovery(started["task_id"]).can_resume
    change = conversation.preview_provider_change(started["task_id"], config)
    assert change.compatible, change.reason
    assert "中断后核查记录" in change.materials
    assert target.read_text(encoding="utf-8") == "苹果 5\n"


def test_restart_keeps_unstarted_approval_and_waits_for_user(tmp_path: Path) -> None:
    target = tmp_path / ".agents" / "notes.txt"
    target.parent.mkdir()
    target.write_text("original evidence", encoding="utf-8")
    directory = tmp_path / "records"
    with connection(directory) as channel:
        channel.send({"action": "configure", "key": "actions-controlled"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "删除需批准", "scope": {
            "root": str(tmp_path), "allowed_actions": ["read"],
            "provider": "deepseek", "recipients": ["deepseek"],
        }})
        started = channel.receive()
        while (event := channel.receive())["event"] != "action":
            pass
        assert event["action"]["status"] == "awaiting_approval"
        original_action = event["action"]
        channel.crash()
    with connection(directory) as reopened:
        reopened.send({"action": "open", "path": started["path"]})
        snapshot = reopened.receive()
        assert snapshot["event"] == "conversation" and snapshot["running"] is False
        assert snapshot["recovery"]["task_id"] == started["task_id"]
        assert snapshot["recovery"]["required"] is True
        assert snapshot["recovery"]["actions"][0]["effect"] == "not_started"
        assert snapshot["turns"][0]["actions"][0] == original_action
        assert target.read_text(encoding="utf-8") == "original evidence"
        reopened.send({"action": "approve_action", "path": started["path"],
                       "task_id": started["task_id"], "action_id": original_action["id"],
                       "approval_id": original_action["approval_id"], "approved": True})
        assert reopened.receive()["event"] == "error"
        assert target.exists()


@pytest.mark.parametrize("point", ["after_edit", "after_check", "after_result", "changed_file", "exhausted"])
def test_interrupted_real_edit_is_verified_then_resumed_without_repeating(tmp_path: Path, monkeypatch, point: str) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("苹果 3\n", encoding="utf-8")
    check = tmp_path / "check.py"
    check.write_text("from pathlib import Path\np=Path('runs.txt')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nassert Path('notes.txt').read_text(encoding='utf-8') == '苹果 5\\n'\nprint('verified-real-edit')\n", encoding="utf-8")
    marker = tmp_path / "interrupted.pid"
    monkeypatch.setenv("MYRK_RECOVERY_POINT", "after_edit" if point in {"changed_file", "exhausted"} else point)
    monkeypatch.setenv("MYRK_RECOVERY_MARKER", str(marker))
    directory = tmp_path / "records"
    with connection(directory, "tests.recovery_fixture") as channel:
        channel.send({"action": "configure", "key": "controlled", "request_limit": 2 if point == "exhausted" else 10, "active_seconds_limit": 100})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "改写再检查", "scope": {
            "root": str(tmp_path), "allowed_actions": ["read", "edit", "check"],
            "provider": "deepseek", "recipients": ["deepseek"],
            "checks": [{"name": "指定检查", "argv": [sys.executable, str(check)]}],
        }})
        started = channel.receive()
        if point == "after_result":
            while True:
                event = channel.receive()
                if event["event"] == "action" and event["action"]["kind"] == "check" and event["action"]["status"] == "completed":
                    break
        else:
            deadline = monotonic() + 7
            while not marker.exists():
                assert monotonic() < deadline
                sleep(.01)
            assert int(marker.read_text()) > 0
        assert target.read_text(encoding="utf-8") == "苹果 5\n"
        channel.crash()
    monkeypatch.delenv("MYRK_RECOVERY_POINT")
    if point == "exhausted":
        path = Path(started["path"])
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy["version"] = 5
        for turn in legacy["turns"]:
            for field in ("request_owner_id", "request_account", "execution_scope", "execution_facts"):
                turn.pop(field, None)
            for field in ("request_policy", "total_requests", "delegation_max_turns", "max_turns", "turns_used"):
                turn["task"].pop(field, None)
            turn["task"]["request_limit"] = 2
            turn["task"]["active_seconds_limit"] = 100
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    with connection(directory, "tests.recovery_fixture") as reopened:
        reopened.send({"action": "open", "path": started["path"]})
        snapshot = reopened.receive()
        assert snapshot["recovery"]["actions"][-1]["effect"] == ("completed" if point == "after_result" else "unknown")
        original = snapshot["turns"][0]
        original_record = json.loads(Path(started["path"]).read_text(encoding="utf-8"))
        if point == "changed_file":
            target.write_text("different external change", encoding="utf-8")
        reopened.send({"action": "configure", "key": "controlled"})
        assert reopened.receive()["event"] == "configured"
        if point != "after_result":
            reopened.send({"action": "resume_task", "path": started["path"], "task_id": started["task_id"]})
            assert reopened.receive()["event"] == "error"
        reopened.send({"action": "verify_recovery", "path": started["path"], "task_id": started["task_id"]})
        verified = reopened.receive()
        assert verified["event"] == "recovery" and verified["saved"] is True
        if point == "changed_file":
            assert verified["recovery"]["can_resume"] is False
            reopened.send({"action": "resume_task", "path": started["path"], "task_id": started["task_id"]})
            assert reopened.receive()["event"] == "error"
            assert target.read_text() == "different external change" and not (tmp_path / "runs.txt").exists()
            return
        assert verified["recovery"]["can_resume"] is True
        assert verified["recovery"]["actions"][-1]["effect"] == "completed"
        if point == "exhausted":
            assert original["task"]["request_policy"] == "shared"
            assert original["task"]["requests"] == original["task"]["request_limit"] == 2
            reopened.send({"action": "resume_task", "path": started["path"], "task_id": started["task_id"]})
            assert "上限" in reopened.receive()["message"]
            reopened.send({"action": "continue_task", "path": started["path"], "task_id": started["task_id"], "request_limit": 5, "active_seconds_limit": 100})
        else:
            reopened.send({"action": "resume_task", "path": started["path"], "task_id": started["task_id"]})
        while (finished := reopened.receive())["event"] != "finished":
            pass
        assert finished["turn"]["status"] == "completed"
        assert finished["turn"]["task"]["id"] == started["task_id"]
        assert finished["turn"]["task"]["requests"] > original["task"]["requests"]
        assert finished["turn"]["task"]["active_seconds"] >= original["task"]["active_seconds"]
        assert finished["turn"]["task"]["scope"] == original["task"]["scope"]
        assert [action["kind"] for action in finished["turn"]["actions"]] == (["check"] if point in {"after_edit", "exhausted"} else [])
        assert finished["turn"]["checks_passed"] is True
        assert (tmp_path / "runs.txt").read_text() == "x"
        final_record = json.loads(Path(started["path"]).read_text(encoding="utf-8"))
        assert final_record["messages"][:len(original_record["messages"])] == original_record["messages"]
        assert final_record["turns"][0]["actions"] == original_record["turns"][0]["actions"]
        assert any(item["phase"] == "started" for item in final_record["action_evidence"])
        assert any(item["phase"] == "result" for item in final_record["action_evidence"])


def test_running_check_is_observed_without_retry_until_its_receipt_exists(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("苹果 3\n", encoding="utf-8")
    script = tmp_path / "check.py"
    release = tmp_path / "release"
    script.write_text("from pathlib import Path\nimport os,time\nPath('check.pid').write_text(str(os.getpid()))\nprint('partial-real-output',flush=True)\nwhile not Path('release').exists(): time.sleep(.03)\nprint('normal-real-end',flush=True)\n", encoding="utf-8")
    directory = tmp_path / "records"
    try:
        with connection(directory, "tests.recovery_fixture") as channel:
            channel.send({"action": "configure", "key": "controlled"})
            assert channel.receive()["event"] == "configured"
            channel.send({"action": "send", "prompt": "改写再检查", "scope": {"root": str(tmp_path), "allowed_actions": ["read", "edit", "check"], "provider": "deepseek", "recipients": ["deepseek"], "checks": [{"name": "指定检查", "argv": [sys.executable, str(script)]}]}})
            started = channel.receive()
            deadline = monotonic() + 7
            while not (tmp_path / "check.pid").exists():
                assert monotonic() < deadline
                sleep(.01)
            service_pid = int((directory / "service.pid").read_text())
            os.kill(service_pid, signal.SIGTERM)
            channel.process.wait(timeout=5)
        stored = Conversation.open(Path(started["path"]))
        report = stored.verify_recovery(started["task_id"])
        assert report.actions[-1].effect == "unknown"
        assert "partial-real-output" in report.actions[-1].observation
        assert "个程序" in report.actions[-1].observation
        with pytest.raises(ValueError, match="未知"):
            asyncio.run(stored.resume_task(started["task_id"], model=TestModel(custom_output_text="不应发送")))
        release.write_text("finish this owned check", encoding="utf-8")
        deadline = monotonic() + 7
        while True:
            report = stored.verify_recovery(started["task_id"])
            if report.actions[-1].effect == "completed":
                break
            assert monotonic() < deadline
            sleep(.03)
        assert "normal-real-end" in report.actions[-1].observation
    finally:
        release.write_text("finish this owned check", encoding="utf-8")


def test_save_failure_retains_last_reliable_location_after_repair(tmp_path: Path) -> None:
    path = tmp_path / "records" / "conversation.json"
    conversation = Conversation(path)
    asyncio.run(conversation.ask("已有内容", document=None, model=TestModel(custom_output_text="部分依据")))
    last_saved = conversation.last_saved_at
    blocker = path.with_suffix(".json.tmp")
    blocker.mkdir()
    with pytest.raises(OSError):
        conversation.save()
    assert conversation.last_saved_at == last_saved
    assert str(path) in conversation.save_failures[-1]
    blocker.rmdir()
    conversation.save()
    reopened = Conversation.open(path)
    assert reopened.save_failures and "进展未能保存" in reopened.save_failures[-1]
    assert reopened.turns[0].answer == "部分依据"
