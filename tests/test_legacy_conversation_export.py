import json
import subprocess
import sys
from pathlib import Path

import pytest

from myrk.conversation import Conversation


ROOT = Path(__file__).resolve().parents[1]


def current_record(version: int = 7) -> dict:
    approval = {
        "mode": "request_approval", "revision": "current-authorization",
        "access": {"read_only": True, "denied_paths": ["C:/private"],
                   "network": {"allowed_hosts": ["allowed.invalid"], "allow_local_binding": False}},
    }
    task = {
        "id": "original-task", "goal": "继续原工作", "scope": None,
        "request_policy": "independent", "request_limit": 12, "requests": 10,
        "total_requests": 16, "max_turns": 10, "turns_used": 10,
        "review_requests": 2, "review_unmeasured_requests": 1, "review_seconds": 3.5,
        "review_input_tokens": 7, "review_output_tokens": None, "approval": approval,
    }
    return {
        "version": version, "messages": [],
        "turns": [{
            "prompt": "继续原问题", "answer": "已有部分结果", "status": "limited",
            "error": "已到原轮数上限", "requests": 10, "input_tokens": None, "output_tokens": 5,
            "task": task, "request_owner_id": "child-id",
            "request_account": {"requests": 4, "turns_used": 4, "max_turns": 4,
                                "review_requests": 1, "review_unmeasured_requests": 1},
            "approval_settings": approval, "completed_reviews": ["completed-review"],
            "delegations": [{
                "id": "child-id", "goal": "原分工", "context": "保留部分结果",
                "scope": {"root": "C:/authorized", "allowed_actions": ["read"],
                          "provider": "deepseek", "recipients": ["deepseek"]},
                "evidence_requirements": "真实原文", "expected_files": ["result.txt"],
                "record_path": "child.json", "status": "limited", "verification_status": "gaps",
                "requests": 4, "turns_used": 4, "max_turns": 4,
                "review_requests": 1, "review_unmeasured_requests": 1,
                "review_seconds": 1.5, "error": "子助理已到原轮数上限",
            }],
            "request_attempts": [{"number": 16, "own_number": 10, "model": "offline",
                                  "provider": "deepseek", "actor": "action_review", "status": "failed"}],
            "context_notes": ["保留原失败证据"],
        }],
        "queue": [{"prompt": "待办补充", "task": dict(task, id="queued-task")}],
        "approval_settings": approval,
        "save_failures": ["原保存失败"], "last_saved_at": "2026-11-09T00:00:00+00:00",
        "request_layouts": {"main": {"task_id": "original-task", "instructions": "缓存布局证据",
                                       "version": 3, "observed_version": 3}},
    }


def run_export(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.export_legacy_conversation", str(source), str(destination)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )


@pytest.mark.parametrize("version", [6, 7])
def test_export_keeps_current_counts_authorization_and_evidence_without_touching_sources(tmp_path: Path, version: int) -> None:
    source = tmp_path / "current.json"
    destination = tmp_path / "continue-with-legacy.json"
    record = current_record(version)
    original = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    source.write_bytes(original)
    backup = tmp_path / "backups" / "current-v6-original.json"
    backup.parent.mkdir()
    backup_original = b'{"version":6,"requests":8,"purpose":"read-only evidence"}'
    backup.write_bytes(backup_original)

    result = run_export(source, destination)

    assert result.returncode == 0, result.stderr
    assert source.read_bytes() == original
    assert backup.read_bytes() == backup_original
    exported = json.loads(destination.read_text(encoding="utf-8"))
    assert exported["version"] == 6
    assert "request_layouts" not in exported
    assert set(exported) == {"version", "messages", "turns", "queue", "approval_settings",
                             "save_failures", "last_saved_at"}
    for name in ("messages", "turns", "queue", "approval_settings", "save_failures", "last_saved_at"):
        assert exported[name] == record[name], name
    reopened = Conversation.open(destination)
    task = reopened.turns[0].task
    assert task is not None
    assert (task.requests, task.total_requests, task.turns_used, task.max_turns,
            task.review_requests, task.review_unmeasured_requests) == (10, 16, 10, 10, 2, 1)
    assert task.approval.revision == "current-authorization"
    assert task.approval.access.read_only
    assert reopened.turns[0].status == reopened.turns[0].delegations[0].status == "limited"
    assert reopened.turns[0].request_account is not None
    assert reopened.turns[0].request_account.requests == 4
    assert reopened.turns[0].delegations[0].requests == 4
    assert "不含请求布局" in result.stdout


@pytest.mark.parametrize("same_source", [False, True])
def test_export_refuses_to_overwrite_an_existing_target_or_the_source(tmp_path: Path, same_source: bool) -> None:
    source = tmp_path / "current.json"
    original = json.dumps(current_record(), ensure_ascii=False).encode("utf-8")
    source.write_bytes(original)
    destination = source if same_source else tmp_path / "existing.json"
    previous = original if same_source else b"existing target evidence"
    destination.write_bytes(previous)

    result = run_export(source, destination)

    assert result.returncode != 0
    assert source.read_bytes() == original
    assert destination.read_bytes() == previous


@pytest.mark.parametrize("version", [1, 5])
def test_export_rejects_old_shared_policy_formats_without_creating_a_target(tmp_path: Path, version: int) -> None:
    source = tmp_path / "old.json"
    destination = tmp_path / "legacy.json"
    record = current_record(version)
    for item in [*record["turns"], *record["queue"]]:
        item["task"]["request_policy"] = "shared"
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    source.write_bytes(original)
    Conversation.open(source)

    result = run_export(source, destination)

    assert result.returncode != 0
    assert source.read_bytes() == original
    assert not destination.exists()


@pytest.mark.parametrize("version", [6, 7])
@pytest.mark.parametrize("field", ["request_policy", "total_requests", "approval"])
def test_export_rejects_missing_current_counters_or_authorization(
    tmp_path: Path, version: int, field: str,
) -> None:
    source = tmp_path / "invalid.json"
    destination = tmp_path / "legacy.json"
    record = current_record(version)
    record["turns"][0]["task"].pop(field)
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    source.write_bytes(original)

    result = run_export(source, destination)

    assert result.returncode != 0
    assert source.read_bytes() == original
    assert not destination.exists()


def test_export_refuses_to_erase_new_reasoning_choices_for_a_legacy_reader(tmp_path: Path) -> None:
    source = tmp_path / "reasoning.json"
    destination = tmp_path / "legacy.json"
    record = current_record(8)
    record["model_selection"] = {"provider_config": {"model": "deepseek-flash"}, "reasoning": "max"}
    record["turns"][0]["task"]["reasoning"] = "max"
    original = json.dumps(record, ensure_ascii=False).encode("utf-8")
    source.write_bytes(original)

    result = run_export(source, destination)

    assert result.returncode != 0
    assert "推理档位" in result.stderr
    assert source.read_bytes() == original
    assert not destination.exists()
