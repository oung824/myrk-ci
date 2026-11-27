import json
from pathlib import Path

import pytest

from myrk.conversation import Conversation, ConversationRecord


def v6_record() -> dict:
    approval = {
        "mode": "request_approval",
        "revision": "original-approval",
        "access": {
            "read_only": True,
            "denied_paths": ["C:/private"],
            "network": {
                "allowed_hosts": ["allowed.invalid"],
                "denied_hosts": ["denied.invalid"],
                "allow_local_binding": False,
            },
        },
    }
    return {
        "version": 6,
        "messages": [
            {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "原问题"}]},
            {"kind": "response", "parts": [{"part_kind": "text", "content": "已保存的部分答复"}]},
        ],
        "turns": [{
            "prompt": "原问题", "answer": "已保存的部分答复", "status": "failed",
            "task": {
                "id": "original-task", "goal": "续办原工作", "scope": None,
                "request_policy": "independent", "request_limit": 12, "requests": 8,
                "max_turns": 10, "turns_used": 6, "total_requests": 13,
                "approval": approval,
            },
        }],
        "queue": [{"prompt": "已排队的补充", "task": {
            "id": "queued-task", "goal": "排队工作", "scope": None,
            "request_policy": "independent", "request_limit": 15, "requests": 3,
            "max_turns": 9, "turns_used": 2, "total_requests": 5,
            "approval": approval,
        }}],
    }


def test_v6_upgrade_keeps_one_byte_exact_readable_original_and_saved_limits(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    original = (json.dumps(v6_record(), ensure_ascii=False, indent=1) + "\r\n").encode("utf-8")
    path.write_bytes(original)

    conversation = Conversation.open(path)
    conversation.save()
    conversation.save()

    backups = list((tmp_path / "backups").glob("conversation-v6-*.json"))
    assert len(backups) == 1, "升级含请求布局的格式前必须保留唯一的v6原件"
    assert backups[0].read_bytes() == original
    assert Conversation.open(backups[0]).turns[0].answer == "已保存的部分答复"
    saved = ConversationRecord.model_validate_json(path.read_bytes())
    assert saved.version == 10
    assert saved.request_layouts == {}
    reopened = Conversation.open(path)
    assert reopened.turns[0].task is not None
    task = reopened.turns[0].task
    assert (task.id, task.request_policy, task.request_limit, task.requests,
            task.max_turns, task.turns_used, task.total_requests) == (
                "original-task", "independent", 12, 8, 10, 6, 13)
    queued = reopened.queue[0].task
    assert (queued.id, queued.request_policy, queued.request_limit, queued.requests,
            queued.max_turns, queued.turns_used, queued.total_requests) == (
                "queued-task", "independent", 15, 3, 9, 2, 5)
    for restored in (task, queued):
        assert restored.approval.mode == "request_approval"
        assert restored.approval.revision == "original-approval"
        assert restored.approval.access.read_only
        assert restored.approval.access.denied_paths == ["C:/private"]
        assert restored.approval.access.network.allowed_hosts == ["allowed.invalid"]
        assert restored.approval.access.network.denied_hosts == ["denied.invalid"]
        assert not restored.approval.access.network.allow_local_binding


def test_backup_failure_leaves_the_v6_original_untouched_and_retry_can_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    record = v6_record()
    record["last_saved_at"] = "2026-11-05T00:00:00+00:00"
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)
    backup_directory = tmp_path / "backups"
    backup_directory.write_text("已有同名文件，不能建立备份目录", encoding="utf-8")
    conversation = Conversation.open(path)

    with pytest.raises(OSError):
        conversation.save()

    assert path.read_bytes() == original
    assert not path.with_suffix(".json.tmp").exists()
    assert conversation.last_saved_at == "2026-11-05T00:00:00+00:00"
    assert conversation.save_failures
    assert ConversationRecord.model_validate_json(path.read_bytes()).version == 6
    backup_directory.unlink()
    conversation.save()
    backups = list(backup_directory.glob("conversation-v6-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert ConversationRecord.model_validate_json(path.read_bytes()).version == 10


def test_transitional_v6_layout_is_readable_and_backed_up_before_format_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    record = v6_record()
    record["request_layouts"] = {"main": {
        "task_id": "original-task", "version": 3, "observed_version": 3,
        "instructions": "已经发送的固定指令",
        "changes": [{"version": 1, "reasons": ["new_task"]}],
        "previews": {"result-1": {"content": "首次发送的预览", "sent": True}},
    }}
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)
    before = ConversationRecord.model_validate_json(original).request_layouts["main"]

    Conversation.open(path).save()

    backups = list((tmp_path / "backups").glob("conversation-v6-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    backup = ConversationRecord.model_validate_json(backups[0].read_bytes())
    assert backup.version == 6 and backup.request_layouts["main"] == before
    Conversation.open(backups[0])
    reopened = Conversation.open(path)
    reopened.save()
    saved = ConversationRecord.model_validate_json(path.read_bytes())
    assert saved.version == 10 and saved.request_layouts["main"] == before
    assert len(list((tmp_path / "backups").glob("conversation-v6-*.json"))) == 1


def test_new_conversation_uses_format_10_and_reopens_without_migration_backup(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    conversation = Conversation(path)
    conversation.record_failure("保留原问题", thinking=False, error="离线失败证据")
    conversation.save()

    record = ConversationRecord.model_validate_json(path.read_bytes())
    assert record.version == 10
    assert record.request_layouts == {}
    reopened = Conversation.open(path)
    assert reopened.turns[0].prompt == "保留原问题"
    assert reopened.turns[0].error == "离线失败证据"
    reopened.save()
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_versions_1_to_5_keep_the_original_shared_request_cap(tmp_path: Path, version: int) -> None:
    path = tmp_path / "conversation.json"
    record = v6_record()
    record["version"] = version
    for item in [*record["turns"], *record["queue"]]:
        task = item["task"]
        for name in ("request_policy", "total_requests", "max_turns", "turns_used"):
            task.pop(name)
        if version < 5:
            task.pop("approval")
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)

    Conversation.open(path).save()

    reopened = Conversation.open(path)
    assert reopened.turns[0].task is not None
    task = reopened.turns[0].task
    queued = reopened.queue[0].task
    assert (task.request_policy, task.request_limit, task.requests, task.total_requests,
            task.max_turns, task.turns_used) == ("shared", 12, 8, 8, None, 0)
    assert (queued.request_policy, queued.request_limit, queued.requests, queued.total_requests,
            queued.max_turns, queued.turns_used) == ("shared", 15, 3, 3, None, 0)
    for restored in (task, queued):
        if version < 5:
            assert restored.approval.mode == "request_approval"
            assert restored.approval.access.legacy_scope
        else:
            assert restored.approval.revision == "original-approval"
            assert restored.approval.access.read_only
    backups = list((tmp_path / "backups").glob(f"conversation-v{version}-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert Conversation.open(backups[0]).turns[0].answer == "已保存的部分答复"
    assert ConversationRecord.model_validate_json(path.read_bytes()).version == 10


@pytest.mark.parametrize("version", [6, 7, 8, 9])
@pytest.mark.parametrize("section", ["turns", "queue"])
@pytest.mark.parametrize("field", ["request_policy", "total_requests"])
def test_modern_record_missing_saved_policy_or_total_is_rejected(
    tmp_path: Path, version: int, section: str, field: str,
) -> None:
    path = tmp_path / "conversation.json"
    record = v6_record()
    record["version"] = version
    record[section][0]["task"].pop(field)
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(ValueError, match="次数政策或累计次数"):
        Conversation.open(path)

    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [5, 6, 7, 8, 9])
@pytest.mark.parametrize("section", ["turns", "queue"])
def test_record_missing_required_saved_approval_is_rejected(
    tmp_path: Path, version: int, section: str,
) -> None:
    path = tmp_path / "conversation.json"
    record = v6_record()
    record["version"] = version
    for item in [*record["turns"], *record["queue"]]:
        if version == 5:
            item["task"].pop("request_policy")
    record[section][0]["task"].pop("approval")
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(ValueError, match="批准设置"):
        Conversation.open(path)

    assert path.read_bytes() == original
