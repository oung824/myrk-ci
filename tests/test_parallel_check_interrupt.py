"""Interrupting a parallel backend check stops its own Windows process tree."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
from textwrap import dedent
from time import monotonic, sleep

import pytest

from tests.test_parallel_checks import synthetic_check_source


@pytest.mark.parametrize("scheduling", ["legacy", "duration"])
def test_parallel_cli_ctrl_c_stops_both_interpreters_and_ignoring_descendants(
    synthetic_check_source: Path, scheduling: str,
) -> None:
    source = synthetic_check_source
    if scheduling == "duration":
        formal = source / "tests/acceptance/test_formal_scenarios.py"
        formal.write_text(formal.read_text(encoding="utf-8").partition("def test_regular_check_in_the_formal_file")[0], encoding="utf-8")
        (source / "tests/test_regular.py").write_text(
            'def test_regular_check(lane_barrier):\n    lane_barrier("other")\n', encoding="utf-8")
    timing_index = source / ".scratch/check-runs/timings/backend.json"
    timing_index.parent.mkdir(parents=True)
    timing_index.write_text('{"before_cancel": true}', encoding="utf-8")
    reference = source / ".scratch/reference.json"
    if scheduling == "duration":
        prefix = "tests/acceptance/test_formal_scenarios.py::test_selected_formal_scenario_traverses_real_main_service_and_sdk"
        reference.write_text(json.dumps({"node_seconds": {
            prefix + "[alpha]": 3, prefix + "[beta]": 1, "tests/test_regular.py::test_regular_check": 2,
        }}), encoding="utf-8")
    probe = source / ".scratch/probe"
    probe.mkdir(parents=True)
    (source / "process_probe.py").write_text(
        dedent("""\
            import ctypes
            from ctypes import wintypes
            import json
            import os
            from pathlib import Path
            import signal
            import subprocess
            import sys
            from time import monotonic, sleep


            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
            kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]


            def publish(destination, **facts):
                times = [wintypes.FILETIME() for _ in range(4)]
                assert kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(value) for value in times))
                pending = destination.with_suffix(".pending")
                pending.write_text(json.dumps({
                    "pid": os.getpid(),
                    "created_filetime": str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime),
                    **facts,
                }), encoding="utf-8")
                pending.replace(destination)


            if __name__ == "__main__" and sys.argv[1] == "descendant":
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGBREAK, signal.SIG_IGN)
                assert kernel.SetConsoleCtrlHandler(None, True)
                publish(Path(sys.argv[2]), ignores_ctrl_c=True)
                sleep(120)

            if __name__ == "__main__" and sys.argv[1] == "console":
                source = Path(__file__).resolve().parent
                probe = source / ".scratch/probe"
                deadline = monotonic() + 30
                while not (probe / "start.request").exists():
                    assert monotonic() < deadline, "The parent never assigned the console helper to its cleanup job"
                    sleep(.01)
                assert kernel.SetConsoleCtrlHandler(None, True)
                # Reset the inherited ignore property before entering the public CLI.
                adapter = (
                    "import ctypes,runpy,sys;"
                    "kernel=ctypes.WinDLL('kernel32',use_last_error=True);"
                    "kernel.SetConsoleCtrlHandler.argtypes=[ctypes.c_void_p,ctypes.c_int];"
                    "assert kernel.SetConsoleCtrlHandler(None,False);"
                    "sys.argv=sys.argv[1:];"
                    "runpy.run_path(sys.argv[0],run_name='__main__')"
                )
                with (probe / "cli.stdout.txt").open("w", encoding="utf-8") as stdout, (probe / "cli.stderr.txt").open("w", encoding="utf-8") as stderr:
                    coordinator = subprocess.Popen(
                        [sys.argv[2], "-B", "-c", adapter, "scripts/run_checks.py", "pytest-parallel", "-q"],
                        cwd=source, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    )
                    try:
                        deadline = monotonic() + 30
                        while not (probe / "signal.request").exists():
                            assert coordinator.poll() is None, "The CLI exited before the parent requested Ctrl+C"
                            assert monotonic() < deadline, "The parent never requested Ctrl+C"
                            sleep(.01)
                        assert kernel.GenerateConsoleCtrlEvent(0, 0)
                        publish(probe / "signal.sent.json", event=0, group=0)
                        returncode = coordinator.wait(timeout=30)
                        (probe / "console.result.json").write_text(json.dumps({"returncode": returncode}), encoding="utf-8")
                    finally:
                        if coordinator.poll() is None:
                            coordinator.kill()
                        coordinator.wait(timeout=5)
            """),
        encoding="utf-8",
    )
    (source / "tests/conftest.py").write_text(
        dedent("""\
            import os
            from pathlib import Path
            import subprocess
            import sys
            from time import monotonic, sleep

            import pytest

            from process_probe import publish


            @pytest.fixture
            def lane_barrier():
                def hold_with_descendant(lane):
                    probe = Path(os.environ["MYRK_PARALLEL_PROBE"])
                    helper = Path(__file__).resolve().parents[1] / "process_probe.py"
                    ready = probe / (lane + ".descendant.json")
                    descendant = subprocess.Popen([sys._base_executable, "-B", str(helper), "descendant", str(ready)])
                    deadline = monotonic() + 20
                    while not ready.exists():
                        assert descendant.poll() is None, "The descendant exited before ignoring Ctrl+C"
                        assert monotonic() < deadline, "The descendant never became ready"
                        sleep(.01)
                    publish(probe / (lane + ".interpreter.json"),
                        runtime_directory=os.environ["MYRK_CHECK_RUN_DIRECTORY"], descendant_pid=descendant.pid)
                    sleep(120)
                return hold_with_descendant
            """),
        encoding="utf-8",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]

    class JobAccounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
            (name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")
        ]

    handles: list[int] = []
    controller = None
    unrelated = None
    cleanup_job = kernel.CreateJobObjectW(None, None)
    assert cleanup_job

    def hold_identity(identity: dict[str, object]) -> int:
        handle = kernel.OpenProcess(0x101001, False, identity["pid"])
        assert handle, f"Cannot hold the observed process: {identity}"
        handles.append(int(handle))
        times = [wintypes.FILETIME() for _ in range(4)]
        assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
        assert str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) == identity["created_filetime"]
        assert kernel.WaitForSingleObject(handle, 0) == 258
        return int(handle)

    try:
        unrelated = subprocess.Popen(
            [sys._base_executable, "-B", "-c", "from time import sleep; sleep(120)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        controller = subprocess.Popen(
            [sys._base_executable, "-B", str(source / "process_probe.py"), "console", sys.executable],
            cwd=source,
            env=dict(os.environ, PYTHONPATH=str(source), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", MYRK_PARALLEL_PROBE=str(probe),
                MYRK_TEST_SCHEDULING=scheduling, MYRK_TEST_TIMING_REFERENCE=str(reference)),
            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        assert kernel.AssignProcessToJobObject(cleanup_job, int(getattr(controller, "_handle")))
        (probe / "start.request").touch()
        ready_paths = [probe / f"{lane}.{kind}.json" for lane in ("formal", "other") for kind in ("interpreter", "descendant")]
        deadline = monotonic() + 30
        while not all(path.is_file() for path in ready_paths) and controller.poll() is None and monotonic() < deadline:
            sleep(.01)
        assert all(path.is_file() for path in ready_paths), {
            "error": "Both groups and their signal-ignoring descendants must be ready before Ctrl+C",
            "ready_files": sorted(path.name for path in probe.iterdir()),
            "plans": [json.loads(path.read_text(encoding="utf-8"))["lanes"]
                for path in (source / ".scratch/check-runs/results").glob("*.reports/pytest-shards/collection.json")],
        }
        observed = {}
        for lane in ("formal", "other"):
            interpreter = json.loads((probe / f"{lane}.interpreter.json").read_text(encoding="utf-8"))
            descendant = json.loads((probe / f"{lane}.descendant.json").read_text(encoding="utf-8"))
            assert descendant["ignores_ctrl_c"] is True
            assert interpreter["descendant_pid"] == descendant["pid"]
            observed[lane] = interpreter
            hold_identity(interpreter)
            hold_identity(descendant)
        assert len(set(identity["pid"] for identity in observed.values())) == 2
        assert kernel.WaitForSingleObject(int(getattr(unrelated, "_handle")), 0) == 258
        (probe / "signal.request").touch()
        helper_stdout, helper_stderr = controller.communicate(timeout=45)

        assert controller.returncode == 0, helper_stdout + helper_stderr
        sent = json.loads((probe / "signal.sent.json").read_text(encoding="utf-8"))
        assert sent["event"] == 0 and sent["group"] == 0
        assert json.loads((probe / "console.result.json").read_text(encoding="utf-8"))["returncode"] == 130
        stdout = (probe / "cli.stdout.txt").read_text(encoding="utf-8")
        stderr = (probe / "cli.stderr.txt").read_text(encoding="utf-8")
        aggregate = json.loads(stdout.strip().splitlines()[-1])
        assert aggregate["exit_code"] == 130, stdout + stderr
        assert aggregate["runtime_removed"] is True
        assert aggregate["collection_receipt"]["runtime_removed"] is True
        for lane in ("formal", "other"):
            lane_name = lane if scheduling == "legacy" else "lane1" if lane == "formal" else "lane2"
            receipt = aggregate["lanes"][lane_name]["receipt"]
            assert receipt["runtime_removed"] is True
            assert not Path(receipt["runtime_directory"]).exists()
            assert receipt["runtime_directory"] == observed[lane]["runtime_directory"]
            for command in receipt["commands"]:
                assert command["interpreter"]["pid"] == observed[lane]["pid"]
                assert command["interpreter"]["created_filetime"] == observed[lane]["created_filetime"]
                assert command["interpreter_exit_confirmed"] is True
                assert command["job_active_processes"] == 0
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
        assert not list((source / ".scratch/check-runs/runtime-tmp").iterdir())
        assert timing_index.read_text(encoding="utf-8") == '{"before_cancel": true}'
        assert unrelated.poll() is None
        assert kernel.WaitForSingleObject(int(getattr(unrelated, "_handle")), 0) == 258
    finally:
        assert kernel.TerminateJobObject(cleanup_job, 1)
        accounting = JobAccounting()
        deadline = monotonic() + 5
        while True:
            assert kernel.QueryInformationJobObject(cleanup_job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None)
            if accounting.active == 0 or monotonic() >= deadline:
                break
            sleep(.01)
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        for process in (controller, unrelated):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
        kernel.CloseHandle(cleanup_job)
        assert accounting.active == 0, "The test console still has owned descendant processes"
