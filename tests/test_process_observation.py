"""Exercise the shared observer at its caller-visible boundary."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from tests.support.process_observation import (
    ExitResult, IdentityResult, OpenResult, ParentEvidence, ProcessIdentity,
    ProcessObserver, SpawnEvidence, SystemError, WaitResult,
)


class WindowsResult:
    """Fixed results only at the operating-system boundary."""

    def open(self, pid: int) -> OpenResult:
        return OpenResult(None, SystemError("OpenProcess", 5))

    def wait(self, handle: int, timeout_ms: int) -> WaitResult:
        return WaitResult(False)

    def exit_code(self, handle: int) -> ExitResult:
        return ExitResult(259)

    def close(self, handle: int) -> None:
        pass


class LiveWindowsResult(WindowsResult):
    def open(self, pid: int) -> OpenResult:
        return OpenResult(101)

    def identity(self, handle: int, pid: int) -> IdentityResult:
        return IdentityResult(ProcessIdentity(pid, 100, "C:/fixture.exe"))


def test_access_denied_is_retained_as_unknown_in_the_shared_entry() -> None:
    result = ProcessObserver(WindowsResult()).capture(234)
    assert result.state == "unknown"
    assert result.ownership == "unknown"
    assert result.errors == (SystemError("OpenProcess", 5),)


def test_missing_creation_time_is_unknown_and_keeps_the_original_read_error() -> None:
    class MissingTime(WindowsResult):
        def open(self, pid: int) -> OpenResult:
            return OpenResult(101)

        def identity(self, handle: int, pid: int) -> IdentityResult:
            return IdentityResult(ProcessIdentity(pid, None, "C:/fixture.exe"),
                                  (SystemError("GetProcessTimes", 5),))

    result = ProcessObserver(MissingTime()).capture(234)
    assert result.state == "unknown"
    assert result.identity == ProcessIdentity(234, None, "C:/fixture.exe")
    assert result.errors == (SystemError("GetProcessTimes", 5),)


def test_unsignaled_held_process_is_running() -> None:
    result = ProcessObserver(LiveWindowsResult()).capture(234)
    assert result.state == "running"
    assert result.completed is False
    assert result.exit_code == 259


def test_wait_failure_keeps_unknown_even_when_exit_query_returns_a_code() -> None:
    class FailedWait(LiveWindowsResult):
        def wait(self, handle: int, timeout_ms: int) -> WaitResult:
            return WaitResult(None, SystemError("WaitForSingleObject", 6))

        def exit_code(self, handle: int) -> ExitResult:
            return ExitResult(0)

    result = ProcessObserver(FailedWait()).capture(234)
    assert result.state == "unknown"
    assert result.completed is None
    assert result.exit_code == 0
    assert result.errors == (SystemError("WaitForSingleObject", 6),)


def test_signaled_held_process_can_have_exited_with_259() -> None:
    class Exited259(LiveWindowsResult):
        def wait(self, handle: int, timeout_ms: int) -> WaitResult:
            return WaitResult(True)

    result = ProcessObserver(Exited259()).capture(234)
    assert result.state == "exited"
    assert result.completed is True
    assert result.exit_code == 259


def test_child_older_than_the_exact_parent_is_excluded_as_parent_pid_reuse() -> None:
    parent = ParentEvidence(ProcessIdentity(123, 200, "C:/parent.exe"), 123)
    result = ProcessObserver(LiveWindowsResult()).capture(234, parent=parent)
    assert result.state == "running"
    assert result.ownership == "excluded"
    assert result.parent == parent
    assert "child_predates_exact_parent" in result.reasons


def test_exact_direct_spawn_receipt_proves_ownership_independently_of_running() -> None:
    identity = ProcessIdentity(234, 100, "C:/fixture.exe")
    receipt = SpawnEvidence(identity, 123, ("C:/fixture.exe",), "C:/case", "caller_spawn")
    result = ProcessObserver(LiveWindowsResult()).capture(234, expected=identity, direct_spawn=receipt)
    assert result.ownership == "owned"
    assert result.direct_spawn == receipt


def test_reused_pid_is_excluded_instead_of_inheriting_the_original_spawn() -> None:
    original = ProcessIdentity(234, 50, "C:/fixture.exe")
    receipt = SpawnEvidence(original, 123, ("C:/fixture.exe",), "C:/case", "caller_spawn")
    result = ProcessObserver(LiveWindowsResult()).capture(234, expected=original, direct_spawn=receipt)
    assert result.ownership == "excluded"
    assert result.identity == ProcessIdentity(234, 100, "C:/fixture.exe")
    assert result.expected == original
    assert "identity_mismatch" in result.reasons


@pytest.mark.parametrize("pid", [0, 234, -1, 0x100000000])
def test_error_87_does_not_prove_exit_for_a_pid_or_invalid_parameters(pid: int) -> None:
    class InvalidParameter(WindowsResult):
        def open(self, pid: int) -> OpenResult:
            return OpenResult(None, SystemError("OpenProcess", 87, (("access", 0x101000), ("inherit", False), ("pid", pid))))

    result = ProcessObserver(InvalidParameter()).capture(pid)
    assert result.state == "unknown"
    assert result.errors[0].code == 87
    assert dict(result.errors[0].parameters) == {"access": 0x101000, "inherit": False, "pid": pid}


def test_later_child_and_exact_identity_without_a_spawn_receipt_stay_unknown() -> None:
    parent = ParentEvidence(ProcessIdentity(123, 50, "C:/parent.exe"), 123)
    result = ProcessObserver(LiveWindowsResult()).capture(234, expected=ProcessIdentity(234, 100, "C:/fixture.exe"), parent=parent)
    assert result.ownership == "unknown"
    assert result.parent == parent


def test_missing_image_cannot_become_an_identity_exclusion() -> None:
    class MissingImage(LiveWindowsResult):
        def identity(self, handle: int, pid: int) -> IdentityResult:
            return IdentityResult(ProcessIdentity(pid, 100, None),
                                  (SystemError("QueryFullProcessImageNameW", 5),))

    result = ProcessObserver(MissingImage()).capture(234, expected=ProcessIdentity(234, 100, "C:/fixture.exe"))
    assert (result.state, result.ownership) == ("unknown", "unknown")
    assert result.errors == (SystemError("QueryFullProcessImageNameW", 5),)


def test_retained_handle_keeps_its_original_identity_when_pid_query_would_change() -> None:
    class ReusedLater(LiveWindowsResult):
        current_time = 100

        def identity(self, handle: int, pid: int) -> IdentityResult:
            return IdentityResult(ProcessIdentity(pid, self.current_time, "C:/fixture.exe"))

    system = ReusedLater()
    observer = ProcessObserver(system)
    before = observer.capture(234)
    system.current_time = 900
    after = observer.status()[0]
    assert after.identity == before.identity == ProcessIdentity(234, 100, "C:/fixture.exe")


def test_cli_reports_every_unknown_without_stopping_the_observed_process() -> None:
    command = [sys.executable, "-B", "-m", "tests.support.process_observation"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    commands = [{"action": "capture", "pid": os.getpid()}, {"action": "capture", "pid": 0},
                {"action": "status"}, {"action": "wait", "timeout_ms": 0}, {"action": "close"}]
    output, errors = process.communicate("".join(json.dumps(item) + "\n" for item in commands), timeout=10)
    assert process.returncode == 0, errors
    reports = [json.loads(line) for line in output.splitlines()]
    assert len(reports) == 5
    for report in reports[1:]:
        assert report["complete"] is False
        assert {item["pid"]: item["state"] for item in report["processes"]} == {os.getpid(): "running", 0: "unknown"}
        assert all(item["ownership"] == "unknown" for item in report["processes"])


def test_cli_preserves_full_windows_creation_time_as_decimal_text() -> None:
    observer = ProcessObserver()
    identity = observer.capture(os.getpid()).identity
    observer.close()
    assert identity is not None
    wire_identity = {**asdict(identity), "created_filetime": str(identity.created_filetime)}
    command = {"action": "capture", "pid": os.getpid(), "expected": wire_identity,
               "direct_spawn": {"identity": wire_identity, "launcher_pid": os.getppid(),
                                "argv": [sys.executable], "cwd": str(Path.cwd()), "source": "fixture_receipt"}}
    process = subprocess.run([sys.executable, "-B", "-m", "tests.support.process_observation"],
                             input=json.dumps(command) + '\n{"action":"close"}\n',
                             capture_output=True, text=True, encoding="utf-8", timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout.splitlines()[0])["processes"][0]
    assert result["ownership"] == "owned"
    assert result["identity"]["created_filetime"] == str(identity.created_filetime)


def test_cli_rejects_numeric_creation_time_before_it_can_become_rounded_identity() -> None:
    command = {"action": "capture", "pid": os.getpid(), "expected":
               {"pid": os.getpid(), "created_filetime": 134380628785009820, "image": sys.executable}}
    process = subprocess.run([sys.executable, "-B", "-m", "tests.support.process_observation"],
                             input=json.dumps(command) + "\n", capture_output=True,
                             text=True, encoding="utf-8", timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode != 0
    assert "created_filetime" in process.stderr


def test_import_has_no_native_process_or_file_write_actions() -> None:
    script = """
import ctypes, importlib, os, subprocess, sys
def audit(event, args):
    if event in {"subprocess.Popen", "ctypes.dlopen", "os.mkdir", "os.remove", "os.rename", "socket.__new__"}:
        raise AssertionError(event)
    if event == "open":
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(letter in mode for letter in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise AssertionError("file write during import")
sys.addaudithook(audit)
importlib.import_module("tests.support.process_observation")
print("safe-import")
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "safe-import"


def test_cli_command_and_path_fields_do_not_substitute_for_a_spawn_receipt() -> None:
    command = {"action": "capture", "pid": os.getpid(), "direct_spawn":
               {"launcher_pid": os.getppid(), "argv": [sys.executable], "cwd": str(Path.cwd()), "source": "caller_spawn"}}
    result = subprocess.run([sys.executable, "-B", "-m", "tests.support.process_observation"],
                            input=json.dumps(command) + "\n", capture_output=True, text=True,
                            encoding="utf-8", timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode != 0
    assert "direct_spawn.identity" in result.stderr


def test_cli_keeps_a_proven_exclusion_in_the_report_without_blocking_completion() -> None:
    observer = ProcessObserver()
    identity = observer.capture(os.getpid()).identity
    observer.close()
    assert identity is not None and identity.created_filetime is not None
    expected = {**asdict(identity), "created_filetime": str(identity.created_filetime - 1)}
    command = {"action": "capture", "pid": os.getpid(), "expected": expected}
    result = subprocess.run([sys.executable, "-B", "-m", "tests.support.process_observation"],
                            input=json.dumps(command) + "\n", capture_output=True, text=True,
                            encoding="utf-8", timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["complete"] is True
    assert report["processes"][0]["state"] == "running"
    assert report["processes"][0]["ownership"] == "excluded"
    assert report["processes"][0]["reasons"] == ["identity_mismatch"]


def test_real_self_finishing_process_keeps_exact_identity_and_completion_receipt(tmp_path: Path) -> None:
    finish = tmp_path / "finish.flag"
    script = """
import ctypes, json, os, sys, time
from ctypes import wintypes
from pathlib import Path
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
handle = kernel.GetCurrentProcess()
kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
times = [wintypes.FILETIME() for _ in range(4)]
assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
size = wintypes.DWORD(32768)
image = ctypes.create_unicode_buffer(size.value)
assert kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size))
print(json.dumps({"pid": os.getpid(), "parent_pid": os.getppid(), "created_filetime": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime, "image": image.value}), flush=True)
deadline = time.monotonic() + 15
while not Path(sys.argv[1]).exists() and time.monotonic() < deadline:
    sum(range(10000))
    time.sleep(.01)
print(json.dumps({"self_exit": True, "pid": os.getpid(), "code": 259}), flush=True)
sys.exit(259)
"""
    command = [sys.executable, "-B", "-c", script, str(finish)]
    child = subprocess.Popen(command, cwd=Path.cwd(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    observer = ProcessObserver()
    try:
        assert child.stdout is not None
        child_receipt = json.loads(child.stdout.readline())
        parent_pid = child_receipt.pop("parent_pid")
        identity = ProcessIdentity(**child_receipt)
        captured_root = observer.capture(child.pid)
        assert captured_root.identity is not None and child.poll() is None
        root_receipt = SpawnEvidence(captured_root.identity, os.getpid(), tuple(command), str(Path.cwd()), "caller_spawn")
        root_running = observer.capture(child.pid, expected=captured_root.identity, direct_spawn=root_receipt)
        receipt = SpawnEvidence(identity, parent_pid, tuple(command), str(Path.cwd()), "fixture_receipt")
        running = observer.capture(identity.pid, expected=identity, direct_spawn=receipt,
                                   parent=ParentEvidence(captured_root.identity, parent_pid))
        (tmp_path / "running.json").write_text(json.dumps(asdict(running), indent=2), encoding="utf-8")
        assert root_running.ownership == "owned"
        assert (running.state, running.ownership, running.completed, child.poll()) == ("running", "owned", False, None)
        finish.write_text("This case-owned program may finish itself", encoding="utf-8")
        completed = observer.wait(timeout_ms=5000)
        exited = next(item for item in completed if item.pid == identity.pid)
        child.wait(timeout=5)
        assert (exited.state, exited.ownership, exited.completed, exited.exit_code) == ("exited", "owned", True, 259)
        assert exited.identity == identity
        self_exit = json.loads(child.stdout.readline())
        assert self_exit == {"self_exit": True, "pid": identity.pid, "code": 259}
        assert all(item.state == "exited" and item.ownership == "owned" for item in completed)
        (tmp_path / "receipts.json").write_text(json.dumps({"running": asdict(running), "exited": [asdict(item) for item in completed], "self_exit": self_exit}, indent=2), encoding="utf-8")
    finally:
        finish.write_text("This case-owned program may finish itself", encoding="utf-8")
        child.wait(timeout=20)
        assert child.stderr is not None
        (tmp_path / "child-stderr.txt").write_text(child.stderr.read(), encoding="utf-8")
        observer.close()
