"""Check how both entry points report a Windows job that remains active."""

import asyncio
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from typing import Any

import pytest

from myrk.actions import Action
from myrk.checks import CheckReceipt, execute_check, prepare_check_runtime
from myrk.permissions import AccessSettings
from scripts import run_checks


class JobAccounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_longlong * 4),
        ("page_faults", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("terminated_processes", wintypes.DWORD),
    ]


@pytest.fixture
def hold_job_exit(monkeypatch: pytest.MonkeyPatch) -> tuple[Event, Event]:
    reported_active = Event()
    release = Event()
    load_library = ctypes.WinDLL

    def load(name: str, *args: Any, **kwargs: Any) -> ctypes.WinDLL:
        kernel = load_library(name, *args, **kwargs)
        if name != "kernel32":
            return kernel
        query = kernel.QueryInformationJobObject
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]

        def report(job: int, kind: int, data: Any, size: int, returned: Any) -> int:
            success = query(job, kind, data, size, returned)
            if success and kind == 1 and not release.is_set():
                assert size == ctypes.sizeof(JobAccounting)
                accounting = ctypes.cast(data, ctypes.POINTER(JobAccounting)).contents
                if accounting.active_processes == 0:
                    accounting.active_processes = 1
                    reported_active.set()
            return success

        kernel.QueryInformationJobObject = report
        return kernel

    monkeypatch.setattr(ctypes, "WinDLL", load)
    return reported_active, release


def test_backend_retains_the_runtime_when_job_exit_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    hold_job_exit: tuple[Event, Event],
) -> None:
    reported_active, _ = hold_job_exit
    monkeypatch.setattr(run_checks, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_checks.py", "mypy", "--version"])

    assert run_checks.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert reported_active.is_set()
    assert result["commands"][0]["exit_code"] == 0
    assert result["commands"][0]["interpreter_exit_confirmed"] is True
    assert "job_active_processes" not in result["commands"][0]
    assert result["cleanup_error"] == "Owned check job still contains 1 processes."
    assert result["runtime_removed"] is False
    assert result["retained_runtime"]["path"] == result["runtime_directory"]
    assert Path(result["runtime_directory"]).is_dir()
    assert json.loads(Path(result["result_path"]).read_text(encoding="utf-8")) == result


def test_application_waits_for_job_exit_before_completing_failed_check_cleanup(
    tmp_path: Path, hold_job_exit: tuple[Event, Event],
) -> None:
    prepare_check_runtime()
    reported_active, release = hold_job_exit
    action = Action(task_id="job-exit-test", kind="check", target="进程退出")
    unrelated = subprocess.Popen([sys._base_executable, "-B", "-c", "import time; time.sleep(60)"])

    async def run() -> None:
        running = asyncio.create_task(execute_check(
            [sys.executable, "-c", "import sys; print('retained output'); sys.exit(7)"],
            tmp_path, 5, action,
            access=AccessSettings(writable_roots=[str(tmp_path)]), save=lambda: None,
            evidence_directory=tmp_path / "records" / "checks", protected_paths=[],
        ))
        try:
            async with asyncio.timeout(15):
                while not reported_active.is_set() and not running.done():
                    await asyncio.sleep(.01)
                assert reported_active.is_set()
                await asyncio.sleep(5.1)
                assert action.cleanup_complete is False
                assert not running.done()
        finally:
            release.set()
            with pytest.raises(ValueError, match="退出情况：7"):
                await running

    try:
        asyncio.run(run())
        assert action.cleanup_complete is True
        assert action.exit_code == 7
        assert action.stdout == "retained output\n"
        assert action.receipt_path
        receipt = CheckReceipt.model_validate_json(Path(action.receipt_path).read_text(encoding="utf-8"))
        assert receipt.action_id == action.id
        assert receipt.exit_code == 7
        assert unrelated.poll() is None
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=5)
