"""Run only the selected check and finish this task's Windows process tree."""

import asyncio
from collections.abc import Awaitable, Callable
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from time import monotonic

from myrk.actions import Action
from myrk.permissions import AccessSettings
from myrk.supervisor_runtime import SUPERVISOR_RUNTIME
from pydantic import BaseModel, ConfigDict, Field


class CheckFileVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str
    size_bytes: int


class CheckReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str
    exit_code: int
    sandbox_type: str | None = None
    argv: list[str] | None = None
    cwd: str | None = None
    input_files_before: dict[str, CheckFileVersion | None] = Field(default_factory=dict)
    input_files_after: dict[str, CheckFileVersion | None] = Field(default_factory=dict)
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


def job_active(name: str) -> int:
    """Query only this action's uniquely named owned Windows process group."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes, kernel.OpenJobObjectW.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenJobObjectW(4, False, name)
    if not handle:
        if ctypes.get_last_error() == 2:
            return 0
        raise ctypes.WinError(ctypes.get_last_error())
    accounting = _Accounting()
    try:
        if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.active)
    finally:
        kernel.CloseHandle(handle)


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [(name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]


async def execute_check(argv: list[str], cwd: Path, timeout: float, action: Action, *, access: AccessSettings, save: Callable[[], None], evidence_directory: Path, protected_paths: list[Path], extra_write_paths: list[str] | None = None,
                        network_decision: Callable[[dict[str, Any]], Awaitable[bool]] | None = None) -> str:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    action.job_name = f"Local\\Myrk-{action.id}"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    action.receipt_path = str(evidence_directory / f"{action.id}.json")
    action.stdout_path, action.stderr_path = str(evidence_directory / f"{action.id}.stdout"), str(evidence_directory / f"{action.id}.stderr")
    job = kernel.CreateJobObjectW(None, action.job_name)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process: subprocess.Popen[bytes] | None = None
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None
    assigned = False
    network_tasks: set[asyncio.Task[None]] = set()
    seen_network: set[Path] = set()
    async def decide_network(path: Path) -> None:
        allowed = False
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            if network_decision is not None:
                allowed = await network_decision(request)
        finally:
            pending = path.with_suffix(".decision-tmp")
            pending.write_text(json.dumps({"allowed": allowed}), encoding="utf-8")
            pending.replace(path.with_suffix(".decision"))
    try:
        process = subprocess.Popen(SUPERVISOR_RUNTIME.command(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=SUPERVISOR_RUNTIME.directory,
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                   startupinfo=subprocess.STARTUPINFO(lpAttributeList={"handle_list": SUPERVISOR_RUNTIME.handles}),
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        action.process_id, action.cwd = process.pid, str(cwd)
        action.cleanup_complete = False
        handle = kernel.OpenProcess(0x0101, False, process.pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            assigned = True
        finally:
            kernel.CloseHandle(handle)
        save()
        communication = asyncio.create_task(asyncio.to_thread(process.communicate, (json.dumps({"argv": argv, "cwd": str(cwd), "application_root": str(Path(__file__).parent.parent), "action_id": action.id, "receipt_path": action.receipt_path, "stdout_path": action.stdout_path, "stderr_path": action.stderr_path, "access": access.model_dump(), "runtime_roots": [str(path) for path in SUPERVISOR_RUNTIME.host_roots], "protected_paths": [str(path) for path in [*protected_paths, SUPERVISOR_RUNTIME.directory, *SUPERVISOR_RUNTIME.host_roots]], "extra_write_paths": extra_write_paths or []}) + "\n").encode()))
        prepared = monotonic()
        started: float | None = None
        last_tick = monotonic()
        while process.poll() is None:
            for path in evidence_directory.glob(f"{action.id}.network-*.json"):
                if path not in seen_network:
                    seen_network.add(path)
                    network_task = asyncio.create_task(decide_network(path))
                    network_tasks.add(network_task)
                    network_task.add_done_callback(network_tasks.discard)
            if started is None and Path(action.receipt_path).with_suffix(".started").exists():
                started = monotonic()
            now = monotonic()
            if network_tasks:
                if started is not None:
                    started += now - last_tick
                else:
                    prepared += now - last_tick
            last_tick = now
            if started is not None and monotonic() - started >= timeout:
                action.timed_out = True
                break
            if started is None and monotonic() - prepared >= 30:
                raise ValueError("受限执行准备超时，已停止；没有改用本机账号直接执行。")
            await asyncio.sleep(0.02)
    finally:
        for network_task in list(network_tasks):
            network_task.cancel()
        await asyncio.gather(*network_tasks, return_exceptions=True)
        if assigned and process is not None and process.poll() is None:
            Path(action.receipt_path).with_suffix(".stop").touch()
            deadline = monotonic() + 5
            while process.poll() is None and monotonic() < deadline:
                await asyncio.sleep(.01)
        if process is not None and not assigned and process.poll() is None:
            process.kill()
        if not kernel.TerminateJobObject(job, 1):
            if process is not None and process.poll() is None:
                process.kill()
            kernel.CloseHandle(job)
            raise ctypes.WinError(ctypes.get_last_error())
        accounting = _Accounting()
        while True:
            if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                kernel.CloseHandle(job)
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.active == 0:
                break
            await asyncio.sleep(0.01)
        kernel.CloseHandle(job)
        if process is not None:
            if process.poll() is None:
                await asyncio.to_thread(process.wait)
            action.exit_code = process.returncode
        if communication is not None:
            await asyncio.shield(communication)
        action.stdout = Path(action.stdout_path).read_text(encoding="utf-8", errors="replace") if Path(action.stdout_path).exists() else ""
        action.stderr = Path(action.stderr_path).read_text(encoding="utf-8", errors="replace") if Path(action.stderr_path).exists() else ""
        if communication is not None and communication.done():
            _, recorder_error = communication.result()
            if recorder_error:
                Path(action.receipt_path).with_suffix(".recorder-stderr").write_bytes(recorder_error)
                if not Path(action.receipt_path).with_suffix(".started").exists():
                    action.stderr += recorder_error.decode("utf-8", errors="replace")
        action.cleanup_complete = True
        if Path(action.receipt_path).with_suffix(".started").exists():
            action.sandbox_type = Path(action.receipt_path).with_suffix(".started").read_text(encoding="utf-8")
    execution_label = f"指定检查「{action.target}」" if action.kind == "check" else "命令"
    if action.timed_out:
        raise ValueError(f"{execution_label}已超过{timeout:g}秒；本次拥有的程序已全部收尾。")
    if action.exit_code != 0:
        raise ValueError(f"{execution_label}失败，退出情况：{action.exit_code}。")
    result_label = f"{execution_label}通过" if action.kind == "check" else "命令完成"
    return f"{result_label}，工作位置：{cwd}；退出情况：0。\n正常输出：\n{action.stdout}\n错误输出：\n{action.stderr}"
