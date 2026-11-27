"""Run backend checks with disposable, project-owned runtime directories."""

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import io
import os
from pathlib import Path
import shutil
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
from myrk.check_outputs import report_path, retain_reports, validate_pytest_arguments

ROOT = project_root(SOURCE_ROOT)


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [(name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]


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


def confine_outputs(check: str, arguments: list[str], run: Path) -> list[str]:
    """Resolve supported tool output switches inside this command's owned run."""
    rewritten = list(arguments)
    if check == "pytest":
        validate_pytest_arguments([*rewritten, *shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))])
        config_file = None
        for index, argument in enumerate(rewritten):
            if argument in {"-c", "--config-file"}:
                if index + 1 == len(rewritten):
                    raise ValueError("Pytest configuration requires a project-local path.")
                config_file = rewritten[index + 1]
            elif argument.startswith("--config-file="):
                config_file = argument.partition("=")[2]
            elif argument.startswith("-c"):
                config_file = argument[2:]
        configuration = Path(config_file) if config_file is not None else SOURCE_ROOT / "pyproject.toml"
        if not configuration.is_absolute():
            configuration = SOURCE_ROOT / configuration
        if ".." in configuration.parts or any(part.is_symlink() or part.is_junction() for part in (configuration, *configuration.parents)) or not configuration.resolve().is_relative_to(ROOT):
            raise ValueError("Pytest configuration must be an ordinary project-local path.")
        rewritten.append("--config-file=" + str(configuration.resolve()))
    if check == "mypy":
        if any(argument.startswith("@") for argument in rewritten):
            raise ValueError("Mypy response files can hide output paths; pass their arguments directly.")
        from mypy.config_parser import parse_config_file
        from mypy.main import define_options
        from mypy.options import Options

        option_parser, _, _ = define_options(stdout=io.StringIO(), stderr=io.StringIO())
        for index, argument in enumerate(rewritten):
            flag, separator, value = argument.partition("=")
            if flag == "--":
                break
            if not flag.startswith("--") or flag in option_parser._option_string_actions:
                continue
            matches = [name for name in option_parser._option_string_actions if name.startswith(flag)]
            if len(matches) == 1:
                rewritten[index] = matches[0] + ("=" + value if separator else "")
        config_file = None
        for index, argument in enumerate(rewritten):
            if argument == "--config-file" and index + 1 < len(rewritten):
                config_file = rewritten[index + 1]
            elif argument.startswith("--config-file="):
                config_file = argument.partition("=")[2]
        if config_file is None:
            config_file = next((str(SOURCE_ROOT / name) for name in ("mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg") if (SOURCE_ROOT / name).is_file()), "")
            rewritten.append("--config-file=" + config_file)
        if config_file:
            configured = Options()
            filename = Path(config_file)
            if not filename.is_absolute():
                filename = SOURCE_ROOT / filename
            parse_config_file(configured, lambda: None, str(filename), stdout=io.StringIO(), stderr=io.StringIO())
            existing = {argument.partition("=")[0] for argument in rewritten}
            for kind, location in configured.report_dirs.items():
                flag = "--" + kind + "-report"
                if flag not in existing:
                    rewritten.extend([flag, location])
            if configured.junit_xml and "--junit-xml" not in existing:
                rewritten.extend(["--junit-xml", configured.junit_xml])
    index = 0
    while index < len(rewritten):
        argument = rewritten[index]
        flag, separator, value = argument.partition("=")
        if check == "pytest" and flag == "--debug" and not separator and (index + 1 == len(rewritten) or rewritten[index + 1].startswith("-")):
            separator, value = "=", "pytestdebug.log"
        if flag in {"-o", "--override-ini"}:
            override = value if separator else (rewritten[index + 1] if index + 1 < len(rewritten) else "")
            if override.startswith("log_file="):
                destination = report_path(override.partition("=")[2], kind="log_file", run=run, project=ROOT, source=SOURCE_ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                rewritten[index if separator else index + 1] = (flag + "=" if separator else "") + "log_file=" + str(destination)
            index += 1 if separator else 2
            continue
        output_flag = flag in {"--junitxml", "--junit-xml", "--log-file", "--html", "--json-report-file", "--output-file"}
        output_flag = output_flag or (check == "pytest" and flag == "--debug")
        output_flag = output_flag or (check == "mypy" and flag.startswith("--") and flag.endswith("-report"))
        coverage = flag == "--cov-report"
        if not output_flag and not coverage:
            index += 1
            continue
        if not separator:
            index += 1
            if index >= len(rewritten):
                raise ValueError(f"Output option {flag} requires a path.")
            value = rewritten[index]
        prefix = ""
        if coverage:
            kind, colon, location = value.partition(":")
            if kind in {"term", "term-missing", "annotate"} and not colon:
                if kind != "annotate":
                    index += 1
                    continue
            if kind == "" or kind.startswith("term"):
                index += 1
                continue
            prefix = kind + ":"
            value = location if colon else {"xml": "coverage.xml", "json": "coverage.json", "lcov": "coverage.lcov"}.get(kind, "coverage-" + kind)
        destination = report_path(value, kind=flag, run=run, project=ROOT, source=SOURCE_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rewritten[index] = (flag + "=" if separator else "") + prefix + str(destination)
        index += 1
    return rewritten


def run_check(
    check: str, arguments: list[str], *, stop: threading.Event | None = None,
    below_normal: bool = False,
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
    owner = {
        "owner": f"backend-check:{run.name}", "pid": os.getpid(),
        "invocation_id": uuid4().hex,
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
        "runtime_removed": False, "failure_logs": failure_logs,
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
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
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
                        command.extend(confine_outputs(check, arguments, run))
                        if check == "pytest":
                            command += ["-p", "myrk.check_plugin"]
                            command += ["--basetemp", str(run / "fixtures"), "-o", f"cache_dir={run / 'cache/pytest'}", "-o", f"pythonpath={shlex.quote(SOURCE_ROOT.as_posix())}"]
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
                if not kernel.TerminateJobObject(job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                accounting = _Accounting()
                deadline = monotonic() + 5
                while True:
                    if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.active == 0:
                        owned_job["entry"]["job_active_processes"] = 0
                        break
                    if monotonic() >= deadline:
                        raise OSError(f"Owned check job still contains {accounting.active} processes.")
                    sleep(.01)
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
        try:
            for check, entry in zip(checks, commands):
                stdout_path, stderr_path = run / f"{check}.stdout.txt", run / f"{check}.stderr.txt"
                entry["output_tail"] = stdout_path.read_text(encoding="utf-8", errors="replace")[-2000:] if stdout_path.exists() else ""
                if entry["exit_code"]:
                    for source in (stdout_path, stderr_path):
                        if source.is_file():
                            destination = results / f"{run.name}.{source.name}"
                            source.replace(destination)
                            failure_logs.append(str(destination))
        except OSError as error:
            stop_errors.append(f"Cannot save the check output: {error}")
        if stop_errors:
            result["cleanup_error"] = "; ".join(stop_errors)
        else:
            try:
                retained_reports = retain_reports(run, result_path, owner=str(owner["owner"]))
                if retained_reports is not None:
                    result["retained_reports"] = retained_reports
            except (OSError, ValueError) as error:
                result["cleanup_error"] = str(error)
        if "cleanup_error" in result:
            pass
        elif any(path.is_symlink() or path.is_junction() for path in (run, *run.parents)) or run.parent != parent:
            result["cleanup_error"] = "The check runtime path changed; automatic removal was refused."
        else:
            try:
                shutil.rmtree(run)
                result["runtime_removed"] = not run.exists()
                if not result["runtime_removed"]:
                    result["cleanup_error"] = "The check runtime still exists after removal."
            except OSError as error:
                result["cleanup_error"] = str(error)
    if failure_logs:
        result["retained"] = {
            "owner": owner["owner"], "reason": "Original output of a failed backend check.",
            "delete_when": "The failure is resolved or its necessary evidence is saved with the task.",
            "paths": failure_logs,
        }
    if not result["runtime_removed"]:
        result["exit_code"] = result["exit_code"] or 1
        result["retained_runtime"] = {
            "path": str(run), "owner": owner["owner"],
            "reason": result["cleanup_error"],
            "delete_when": "The recorded process has exited and the path is ordinary and no longer locked.",
        }
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
