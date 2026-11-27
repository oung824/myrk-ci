import json
import os
from pathlib import Path

from pytest import MonkeyPatch

from tests.support import process_receipt
from tests.support.process_observation import OpenResult, SystemError


def test_fixture_receipt_preserves_a_denied_self_identity_read(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    owner = tmp_path / "owner.json"
    owner.write_text(json.dumps({
        "pid": os.getppid(), "state": "running", "ownership": "owned",
        "identity": {"pid": os.getppid(), "created_filetime": "134380628991022596", "image": "C:\\controlled\\parent.exe"},
    }), encoding="utf-8")
    stop = tmp_path / "normal-stop.flag"
    stop.touch()
    monkeypatch.setenv("MYRK_PROCESS_OWNER_FILE", str(owner))
    monkeypatch.setenv("MYRK_TEST_NORMAL_EXIT_FLAG", str(stop))

    class DeniedSystem:
        def open(self, pid: int) -> OpenResult:
            return OpenResult(None, SystemError("OpenProcess", 5, (("pid", pid),)))

    monkeypatch.setattr(process_receipt, "WindowsProcesses", DeniedSystem)
    path = process_receipt.write_process_receipt(tmp_path / "receipts", __file__)
    assert path is not None
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["errors"] == [
        {"operation": "OpenProcess", "code": 5, "parameters": [["pid", os.getpid()]]},
        {"operation": "OpenProcess", "code": 5, "parameters": [["pid", os.getppid()]]},
    ]
    assert receipt["records"][0]["expected"] == {"pid": os.getpid(), "created_filetime": None, "image": None}
