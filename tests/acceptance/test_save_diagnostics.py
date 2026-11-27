"""Save observation preserves the actual failure and excludes sensitive payloads."""

import json
from pathlib import Path

import pytest

from myrk.conversation import Conversation
from tests.support.agent_acceptance.save_diagnostics import observe_saves


def test_real_write_obstruction_preserves_exception_and_original_record(tmp_path):
    conversation = Conversation(tmp_path / "conversation.json")
    conversation.save()
    original = conversation.path.read_bytes()
    pending = conversation.path.with_suffix(".json.tmp")
    pending.mkdir()
    original_method = Conversation.save
    report = tmp_path / "diagnostics.json"
    with observe_saves(tmp_path, report):
        with pytest.raises(PermissionError) as failure:
            conversation.save()
    assert Conversation.save is original_method
    assert conversation.path.read_bytes() == original
    assert failure.value.filename == str(pending)
    saved = json.loads(report.read_bytes())
    assert saved["save_calls"] == len(saved["errors"]) == 1
    error = saved["errors"][0]
    assert error["exception_type"] == "PermissionError"
    assert error["filename"] == "conversation.json.tmp"
    assert error["operation"] == "open"
    assert any(frame["function"] == "_save" for frame in error["stack"])


def test_observer_rethrows_same_error_without_recording_message_or_locals(tmp_path, monkeypatch):
    secret = "synthetic-credential-must-not-appear"
    failure = OSError(secret)

    def broken(_conversation):
        raise failure

    monkeypatch.setattr(Conversation, "save", broken)
    report = tmp_path / "diagnostics.json"
    with observe_saves(tmp_path, report):
        with pytest.raises(OSError) as observed:
            Conversation(tmp_path / "record.json").save()
    assert observed.value is failure
    assert Conversation.save is broken
    assert secret not in report.read_text(encoding="utf-8")


def test_successful_save_and_existing_report_are_not_changed(tmp_path):
    report = tmp_path / "diagnostics.json"
    original_method = Conversation.save
    with observe_saves(tmp_path, report):
        Conversation(tmp_path / "record.json").save()
    assert Conversation.save is original_method
    assert json.loads(report.read_bytes())["save_calls"] == 1
    assert json.loads(report.read_bytes())["errors"] == []
    original = report.read_bytes()
    with pytest.raises(FileExistsError):
        with observe_saves(tmp_path, report):
            pytest.fail("Existing diagnostics must never be overwritten")
    assert Conversation.save is original_method
    assert report.read_bytes() == original


@pytest.mark.parametrize("save_fails", [False, True])
def test_diagnostic_write_failure_never_replaces_an_original_save_error(tmp_path, monkeypatch, save_fails):
    failure = OSError("Synthetic original persistence failure")
    if save_fails:
        def broken(_conversation):
            raise failure
        monkeypatch.setattr(Conversation, "save", broken)
    original_method = Conversation.save
    report = tmp_path / "diagnostics.json"
    original_open = Path.open
    report_failure = PermissionError("Synthetic diagnostic output failure")

    class BrokenReport:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def write(self, _text):
            raise report_failure

        def __exit__(self, *_exception):
            self.stream.close()

    def open_report(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return BrokenReport(stream) if path == report else stream

    monkeypatch.setattr(Path, "open", open_report)
    with pytest.raises(OSError) as observed:
        with observe_saves(tmp_path, report):
            Conversation(tmp_path / "record.json").save()
    assert Conversation.save is original_method
    if save_fails:
        assert observed.value is failure
        assert failure.__cause__ is None
        assert "PermissionError" in " ".join(failure.__notes__)
    else:
        assert observed.value is report_failure
        assert (tmp_path / "record.json").is_file()
