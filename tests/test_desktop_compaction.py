from pathlib import Path

from myrk.provider import ProviderConfig
from tests.test_desktop_service import connection
from tests.test_model_selection import receive_event


def test_plain_desktop_work_keeps_its_existing_event_order_without_empty_compaction_updates(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "offline-only"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "普通问题"})
        assert channel.receive()["event"] == "started"
        assert channel.receive()["event"] == "text"
        events = []
        while not events or events[-1]["event"] != "finished":
            events.append(channel.receive())
        assert all(event["event"] != "compactions" for event in events)
        channel.send({"action": "snapshot"})
        assert channel.receive()["conversations"][0]["compactions"] == []


def test_desktop_snapshot_exposes_only_summary_dto_and_reads_registered_originals(tmp_path: Path) -> None:
    with connection(tmp_path, module="tests.desktop_compaction_fixture") as channel:
        path = str(tmp_path / "compacted.json")
        channel.send({"action": "open", "path": path})
        opened = receive_event(channel, "conversation")
        summary = opened["compactions"][0]
        assert summary["status"] == "completed"
        assert "view" not in summary and "authorization" not in summary and "messages" not in summary
        assert "compactions" not in opened["turns"][0]
        assert opened["turns"][0]["request_attempts"][0]["purpose"] == "compaction"
        before = Path(path).read_bytes()
        command = {"action": "get_compaction_evidence", "path": path, "summary_id": summary["id"], "message_index": 2, "part_index": 0, "start": 8000}
        channel.send(command)
        evidence = receive_event(channel, "compaction_evidence")
        assert "middle-source-refusal-937" in evidence["text"]
        assert evidence["error"] is None and evidence["next_start"] is None
        channel.send({**command, "summary_id": "not-registered"})
        rejected = receive_event(channel, "compaction_evidence")
        assert rejected["text"] == "" and "本会话摘要登记" in rejected["error"]
        channel.send({**command, "path": str(tmp_path / "unopened.json")})
        assert receive_event(channel, "error")["message"] == "会话尚未打开，请先打开对应会话。"
        channel.send({**command, "file_path": "C:\\Windows\\system.ini"})
        assert receive_event(channel, "error")["message"] == "无法识别这条操作，请重新操作。"
        channel.send({"action": "snapshot"})
        snapshot = receive_event(channel, "snapshot")
        current = next(item for item in snapshot["conversations"] if item["path"] == path)
        assert current["compactions"][0]["id"] == summary["id"]
        assert "compactions" not in current["turns"][0]
        assert Path(path).read_bytes() == before


def test_model_change_clears_previous_model_capacity_overrides_and_preserves_work_preferences(tmp_path: Path) -> None:
    config = ProviderConfig(context_window_override=128000, input_token_limit_override=96000,
        output_token_reserve=4096, context_safety_margin=512, auto_compaction=False)
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "offline-only", "provider_config": config.model_dump()})
        receive_event(channel, "configured")
        channel.send({"action": "select_model", "model": "deepseek-flash", "reasoning": "low"})
        same = receive_event(channel, "model_controls")
        assert same["error"] is None
        assert same["selection"]["provider_config"]["context_window_override"] == 128000
        channel.send({"action": "select_model", "model": "deepseek-v4-pro", "reasoning": "default"})
        changed = receive_event(channel, "model_controls")
        assert changed["error"] is None
        selected = changed["selection"]["provider_config"]
        assert selected.get("context_window_override") is None and selected.get("input_token_limit_override") is None
        assert selected["output_token_reserve"] == 4096 and selected["context_safety_margin"] == 512 and selected["auto_compaction"] is False
        channel.send({"action": "open", "path": changed["path"]})
        reopened = receive_event(channel, "conversation")
        assert reopened["model_selection"]["provider_config"] == selected
