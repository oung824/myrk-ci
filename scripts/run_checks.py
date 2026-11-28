"""Run backend checks with disposable, project-owned runtime directories."""

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
from typing import Any
from uuid import uuid4
from time import monotonic, sleep


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(SOURCE_ROOT))
from myrk.project_paths import project_root
from myrk.check_outputs import CheckLaunch, configuration_path, confine_outputs, finalize_check_outputs, settle_backend_launch
from myrk.windows_jobs import job_exit_poll_delays, terminate_job

ROOT = project_root(SOURCE_ROOT)


BOOTSTRAP = """import ctypes,json,os,runpy,sys
from ctypes import wintypes
from pathlib import Path
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.GetCurrentProcess.restype=wintypes.HANDLE
kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
times=[wintypes.FILETIME() for _ in range(4)]
if not kernel.GetProcessTimes(kernel.GetCurrentProcess(),*(ctypes.byref(value) for value in times)):
    raise ctypes.WinError(ctypes.get_last_error())
ready=Path(os.environ['MYRK_CHECK_STARTUP'])
pending=ready.with_suffix('.pending')
pending.write_text(json.dumps({'pid':os.getpid(),'parent_pid':os.getppid(),'created_filetime':str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime),'invocation_id':os.environ['MYRK_CHECK_RUN_ID']}),encoding='utf-8')
pending.replace(ready)
if sys.stdin.buffer.readline()!=b'go\\n':
    sys.exit(125)
module=sys.argv[1]
sys.argv=sys.argv[1:]
runpy.run_module(module,run_name='__main__',alter_sys=True)
"""


def run_check(
    check: str, arguments: list[str], *, stop: threading.Event | None = None,
    below_normal: bool = False, invocation_id: str | None = None,
    parent_invocation_id: str | None = None, group: str | None = None,
    version: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run an owned backend check and save its complete result before returning."""
    if check not in {"pytest", "mypy", "all"}:
        raise ValueError(f"Unsupported backend check: {check}")
    if check == "all" and arguments:
        raise ValueError("Pass check-specific arguments to pytest or mypy, not all.")
    parent = ROOT / ".scratch/check-runs/runtime-tmp"
    results = ROOT / ".scratch/check-runs/results"
    for directory in (parent, results):
        if any(path.is_symlink() or path.is_junction() for path in (directory, *directory.parents)):
            raise ValueError(f"Check directories must not use links: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="b-", dir=parent))
    result_path = results / f"{run.name}.json"
    invocation_id = invocation_id or uuid4().hex
    launch_record = CheckLaunch(ROOT, SOURCE_ROOT, result_path, invocation_id, check, arguments,
                               parent=parent_invocation_id, group=group, version=version)
    owner = {
        "owner": f"backend-check:{run.name}", "pid": os.getpid(),
        "invocation_id": invocation_id,
        "project_root": str(ROOT), "source_root": str(SOURCE_ROOT), "runtime_directory": str(run),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "reason": "Backend check runtime; the command owns all generated files.",
        "delete_when": "The check process has exited and its result and failure logs are saved.",
    }
    if task_id := os.environ.get("MYRK_TASK_ID"):
        owner["task_id"] = task_id
    commands: list[dict[str, Any]] = []
    failure_logs: list[str] = []
    result: dict[str, Any] = {
        **owner, "commands": commands, "exit_code": 0, "result_path": str(result_path),
        "runtime_removed": False, "failure_logs": failure_logs, "mode": check,
    }
    processes: list[subprocess.Popen[Any]] = []
    jobs: list[dict[str, Any]] = []
    interpreters: list[tuple[int, dict[str, Any]]] = []
    checks = ("pytest", "mypy") if check == "all" else (check,)
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        if below_normal:
            kernel.GetPriorityClass.argtypes, kernel.GetPriorityClass.restype = [wintypes.HANDLE], wintypes.DWORD
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        owner["created_filetime"] = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        result.update(owner)
        (run / "owner.json").write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
        temporary = run / "tmp"
        temporary.mkdir()
        environment = dict(os.environ)
        environment.update({
            "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
            "MYPY_CACHE_DIR": str(run / "cache/mypy"), "COVERAGE_FILE": str(run / "cache/coverage"),
            "NPM_CONFIG_CACHE": str(run / "cache/npm"),
            "MYRK_RUNTIME_ROOT": str(run / "runtime"), "MYRK_CHECK_RUN_DIRECTORY": str(run),
            "MYRK_CHECK_RUN_ID": str(owner["invocation_id"]),
            "MYRK_TEST_RUN_PARENT": str(run / "desktop"), "PYTHONPATH": str(SOURCE_ROOT),
            "LOCALAPPDATA": str(run / "profile/local"), "APPDATA": str(run / "profile/roaming"),
            "XDG_CACHE_HOME": str(run / "cache/xdg"), "XDG_CONFIG_HOME": str(run / "profile/config"),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        })
        for check in checks:
            if stop is not None and stop.is_set():
                raise KeyboardInterrupt()
            command = [sys.executable, "-B", "-m", check]
            entry: dict[str, Any] = {"argv": command, "started_at": datetime.now(timezone.utc).isoformat(), "exit_code": 1}
            commands.append(entry)
            stdout_path, stderr_path = run / f"{check}.stdout.txt", run / f"{check}.stderr.txt"
            try:
                with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
                    try:
                        command.extend(confine_outputs(check, arguments, run, source=SOURCE_ROOT, project=ROOT))
                        if check == "pytest":
                            launch_record.bind_pytest_config(configuration_path(arguments, SOURCE_ROOT))
                            command += ["--config-file=" + launch_record.record["source_version"]["pytest_config"]["path"]]
                            command += ["-p", "myrk.check_plugin"]
                            command += ["--basetemp", str(run / "fixtures"), "-o", f"pythonpath={shlex.quote(SOURCE_ROOT.as_posix())}"]
                        else:
                            command += ["--cache-dir", str(run / "cache/mypy")]
                        job = kernel.CreateJobObjectW(None, None)
                        if not job:
                            raise ctypes.WinError(ctypes.get_last_error())
                        owned_job = {"handle": job, "entry": entry, "bootstrap_pending": False}
                        jobs.append(owned_job)
                        startup = run / f"{check}.startup.json"
                        launch = [sys.executable, "-B", "-c", BOOTSTRAP, *command[3:]]
                        if stop is not None and stop.is_set():
                            raise KeyboardInterrupt()
                        process = subprocess.Popen(
                            launch, cwd=SOURCE_ROOT, env={**environment, "MYRK_CHECK_STARTUP": str(startup)},
                            stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                            creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS if below_normal else 0,
                        )
                        processes.append(process)
                        entry["pid"] = process.pid
                        if process.poll() is None:
                            owned_job["bootstrap_pending"] = True
                            launcher_handle = int(getattr(process, "_handle"))
                            launcher_times = [wintypes.FILETIME() for _ in range(4)]
                            if not kernel.GetProcessTimes(launcher_handle, *(ctypes.byref(value) for value in launcher_times)):
                                raise ctypes.WinError(ctypes.get_last_error())
                            launcher_created = (launcher_times[0].dwHighDateTime << 32) | launcher_times[0].dwLowDateTime
                            if not kernel.AssignProcessToJobObject(job, launcher_handle):
                                raise ctypes.WinError(ctypes.get_last_error())
                            if below_normal:
                                priority = kernel.GetPriorityClass(launcher_handle)
                                if not priority:
                                    raise ctypes.WinError(ctypes.get_last_error())
                                entry["launcher_priority_class"] = int(priority)
                            deadline = monotonic() + 10
                            while not startup.exists() and process.poll() is None and monotonic() < deadline:
                                if stop is not None and stop.is_set():
                                    raise KeyboardInterrupt()
                                sleep(.01)
                            if stop is not None and stop.is_set():
                                raise KeyboardInterrupt()
                            if not startup.exists():
                                raise OSError("The owned check interpreter did not disclose its startup identity.")
                            actual = json.loads(startup.read_text(encoding="utf-8"))
                            if actual["invocation_id"] != owner["invocation_id"] or (actual["pid"] != process.pid and actual["parent_pid"] != process.pid):
                                raise OSError("The check startup receipt is not a child of this invocation's held launcher.")
                            handle = kernel.OpenProcess(0x101101, False, actual["pid"])
                            if not handle:
                                raise ctypes.WinError(ctypes.get_last_error())
                            try:
                                actual_times = [wintypes.FILETIME() for _ in range(4)]
                                if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in actual_times)):
                                    raise ctypes.WinError(ctypes.get_last_error())
                                created = (actual_times[0].dwHighDateTime << 32) | actual_times[0].dwLowDateTime
                                if str(created) != actual["created_filetime"] or created < launcher_created:
                                    raise OSError("The check interpreter creation time does not match its startup receipt.")
                                interpreters.append((int(handle), entry))
                                included = wintypes.BOOL()
                                if not kernel.IsProcessInJob(handle, job, ctypes.byref(included)):
                                    raise ctypes.WinError(ctypes.get_last_error())
                                if not included.value and not kernel.AssignProcessToJobObject(job, handle):
                                    raise ctypes.WinError(ctypes.get_last_error())
                                if below_normal:
                                    priority = kernel.GetPriorityClass(handle)
                                    if not priority:
                                        raise ctypes.WinError(ctypes.get_last_error())
                                    entry["interpreter_priority_class"] = int(priority)
                                    actual["priority_class"] = int(priority)
                                entry["interpreter"] = actual
                                owned_job["bootstrap_pending"] = False
                            finally:
                                if not any(held == int(handle) for held, _ in interpreters):
                                    kernel.CloseHandle(handle)
                            if stop is not None and stop.is_set():
                                raise KeyboardInterrupt()
                            assert process.stdin is not None
                            process.stdin.write(b"go\n")
                            process.stdin.flush()
                            process.stdin.close()
                        if stop is None:
                            entry["exit_code"] = process.wait()
                        else:
                            exit_code = process.poll()
                            while exit_code is None:
                                if stop.wait(.05):
                                    raise KeyboardInterrupt()
                                exit_code = process.poll()
                            if stop.is_set():
                                raise KeyboardInterrupt()
                            entry["exit_code"] = exit_code
                    except (OSError, ValueError) as error:
                        entry["startup_error"] = str(error)
                        entry["exit_code"] = 127
                        stderr.write(str(error) + "\n")
            finally:
                entry["finished_at"] = datetime.now(timezone.utc).isoformat()
    except BaseException as error:
        result["interrupted"] = type(error).__name__
        result["interruption_error"] = str(error)
        result["exit_code"] = 130 if isinstance(error, KeyboardInterrupt) else 1
        if commands:
            commands[-1]["exit_code"] = result["exit_code"]
    finally:
        stop_errors = []
        for process in processes:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError as error:
                    stop_errors.append(f"Cannot close the check startup gate: {error}")
        for owned_job in jobs:
            job = owned_job["handle"]
            try:
                terminate_job(kernel, job)
                for delay in job_exit_poll_delays(kernel, job, timeout=5):
                    sleep(delay)
                owned_job["entry"]["job_active_processes"] = 0
                if owned_job["bootstrap_pending"]:
                    raise OSError("Startup was interrupted before the real interpreter's job membership was confirmed; its runtime is retained.")
            except (OSError, KeyboardInterrupt) as error:
                stop_errors.append(str(error))
            finally:
                kernel.CloseHandle(job)
        for handle, entry in interpreters:
            try:
                if kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise OSError("The exact owned check interpreter has not confirmed exit.")
                entry["interpreter_exit_confirmed"] = True
            except (OSError, KeyboardInterrupt) as error:
                stop_errors.append(str(error))
            finally:
                kernel.CloseHandle(handle)
        for process in processes:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if process.poll() is None:
                    raise OSError(f"Child process {process.pid} has not confirmed exit.")
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                stop_errors.append(f"Child process {process.pid}: {error}")
        result["exit_code"] = result["exit_code"] or next((entry["exit_code"] for entry in commands if entry["exit_code"]), 0)
        if "pytest" in checks:
            launch_record.collect_backend_pytest_coverage(run, result)
        finalize_check_outputs(run, parent, result_path, result, checks, commands, stop_errors)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    settle_backend_launch(launch_record, result, checks, commands)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=("pytest", "mypy", "all", "pytest-parallel"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    if options.check == "all" and options.arguments:
        parser.error("Pass check-specific arguments to pytest or mypy, not all.")
    if options.check == "pytest-parallel":
        from scripts.parallel_checks import run_parallel

        result = run_parallel(options.arguments, run_check, source=SOURCE_ROOT, project=ROOT)
    else:
        try:
            result = run_check(options.check, options.arguments)
        except ValueError as error:
            parser.error(str(error))
    print(json.dumps(result))
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
