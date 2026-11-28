import json
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
from myrk.project_paths import project_root

ROOT = project_root(SOURCE_ROOT)


def test_interrupted_virtualenv_check_confirms_the_real_interpreter_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import run_checks

    main = run_checks.main
    observation = tmp_path / "actual-interpreter.json"
    probe = tmp_path / "test_interrupt_actual.py"
    probe.write_text(
        "import ctypes, json, os, time\nfrom ctypes import wintypes\nfrom pathlib import Path\n"
        "def test_interrupt_actual():\n"
        "    kernel = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "    kernel.GetCurrentProcess.restype = wintypes.HANDLE\n"
        "    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4\n"
        "    times = [wintypes.FILETIME() for _ in range(4)]\n"
        "    assert kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(value) for value in times))\n"
        "    destination = Path(os.environ['MYRK_INTERRUPT_OBSERVATION'])\n"
        "    pending = destination.with_suffix('.pending')\n"
        "    pending.write_text(json.dumps(\n"
        "        {'pid': os.getpid(), 'created_filetime': str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)}))\n"
        "    pending.replace(destination)\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )
    unrelated = subprocess.Popen([sys._base_executable, "-B", "-c", "import time; time.sleep(60)"])
    launched: list[subprocess.Popen[bytes]] = []
    handles: list[int] = []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def interrupt_real_check(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(*args, **kwargs)
        launched.append(process)
        original_wait = process.wait
        interrupted = False

        def wait(timeout: float | None = None) -> int:
            nonlocal interrupted
            if not interrupted and timeout is None:
                deadline = monotonic() + 10
                while not observation.exists() and monotonic() < deadline:
                    sleep(.01)
                assert observation.exists(), "The real pytest interpreter never reached its probe"
                actual = json.loads(observation.read_text(encoding="utf-8"))
                handle = kernel.OpenProcess(0x101001, False, actual["pid"])
                assert handle
                handles.append(int(handle))
                times = [wintypes.FILETIME() for _ in range(4)]
                assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
                assert str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) == actual["created_filetime"]
                assert actual["pid"] != process.pid, "This regression must exercise the Windows virtualenv launcher"
                interrupted = True
                raise KeyboardInterrupt()
            return original_wait(timeout=timeout)

        monkeypatch.setattr(process, "wait", wait)
        return process

    monkeypatch.setattr(run_checks, "subprocess", SimpleNamespace(
        Popen=interrupt_real_check,
        PIPE=subprocess.PIPE,
        BELOW_NORMAL_PRIORITY_CLASS=subprocess.BELOW_NORMAL_PRIORITY_CLASS,
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    monkeypatch.setenv("MYRK_INTERRUPT_OBSERVATION", str(observation))
    monkeypatch.setattr(sys, "argv", ["run_checks.py", "pytest", str(probe), "-q"])
    try:
        assert main() == 130
        result = json.loads(capsys.readouterr().out)
        assert handles and kernel.WaitForSingleObject(handles[0], 500) == 0
        assert result["commands"][0]["job_active_processes"] == 0
        assert result["commands"][0]["interpreter_exit_confirmed"] is True
        assert result["runtime_removed"] is True
        assert not Path(result["runtime_directory"]).exists()
        assert unrelated.poll() is None
    finally:
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        for process in [*launched, unrelated]:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_resource_command_verifies_removed_and_retained_paths_without_writing(tmp_path: Path) -> None:
    project = tmp_path / "isolated-project"
    (project / "scripts").mkdir(parents=True)
    (project / "myrk").mkdir()
    for relative in ("scripts/check_resources.py", "myrk/project_paths.py", "myrk/runtime_ownership.py"):
        (project / relative).write_bytes((SOURCE_ROOT / relative).read_bytes())
    (project / "myrk/__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", "--template=", str(project)], check=True, capture_output=True)
    retained = project / "retained.txt"
    retained.write_text("needed evidence", encoding="utf-8")
    manifest = project / "resources.json"
    document = {
        "project_root": str(project),
        "paths": [
            {"path": str(project / "removed"), "status": "removed"},
            {"path": str(retained), "status": "retained", "owner": "current-task",
             "reason": "Original failure output needed for the current fix.",
             "delete_when": "The fix is reviewed and its result recorded."},
        ],
        "branches": [{"name": "task/nonexistent-cleanup-probe", "status": "removed"}],
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")
    before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    process = subprocess.run(
        [sys.executable, "-B", str(project / "scripts/check_resources.py"), str(manifest)],
        cwd=project, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads(process.stdout)["ok"] is True
    assert {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()} == before


def test_resource_command_reports_runtime_cleanup_failure_receipts_not_in_the_manifest(tmp_path: Path) -> None:
    receipt_root = ROOT / ".scratch/runtime/cleanup-failures"
    assert not any(path.is_symlink() or path.is_junction() for path in (receipt_root, *receipt_root.parents))
    receipt_root.mkdir(parents=True, exist_ok=True)
    receipt = receipt_root / f"cleanup-command-probe-{uuid4().hex}.json"
    original = json.dumps({"runtime_directory": str(tmp_path / "runtime"), "error_type": "SyntheticCleanupFailure"})
    manifest = tmp_path / "resources.json"
    manifest.write_text(json.dumps({"project_root": str(ROOT), "paths": []}), encoding="utf-8")
    try:
        with receipt.open("x", encoding="utf-8") as destination:
            destination.write(original)
        process = subprocess.run(
            [sys.executable, str(SOURCE_ROOT / "scripts/check_resources.py"), str(manifest)],
            cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
        )

        assert process.returncode != 0
        result = json.loads(process.stdout)
        failure = next(item for item in result["cleanup_failures"] if item["path"] == str(receipt))
        assert "owning process has exited" in failure["delete_when"]
        assert str(receipt) in "\n".join(result["errors"])
        assert receipt.read_text(encoding="utf-8") == original
    finally:
        receipt.unlink(missing_ok=True)


def test_resource_command_finds_unlisted_exited_owners_but_preserves_active_tasks(tmp_path: Path) -> None:
    parent = ROOT / ".scratch/check-runs/runtime-tmp"
    parent.mkdir(parents=True, exist_ok=True)
    directories = [parent / ("b-audit-" + uuid4().hex) for _ in range(2)]
    child = subprocess.Popen([sys.executable, "-B", "-c", "pass"])
    child.wait(timeout=5)
    manifest = tmp_path / "empty-resources.json"
    manifest.write_text(json.dumps({"project_root": str(ROOT), "paths": []}), encoding="utf-8")
    before = {}
    try:
        for directory, pid in zip(directories, (child.pid, os.getpid())):
            directory.mkdir()
            marker = directory / "owner.json"
            marker.write_text(json.dumps({
                "owner": "resource-audit-test", "pid": pid, "project_root": str(ROOT),
                "runtime_directory": str(directory), "started_at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
            before[marker] = marker.read_bytes()
        simulated = directories[1] / "fixtures" / "simulated-runtime"
        simulated.mkdir(parents=True)
        simulated_marker = simulated / "owner.json"
        simulated_marker.write_text(json.dumps({
            "owner": "synthetic-fixture", "pid": child.pid, "project_root": str(ROOT),
            "runtime_directory": str(simulated), "started_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
        before[simulated_marker] = simulated_marker.read_bytes()
        process = subprocess.run(
            [sys.executable, str(SOURCE_ROOT / "scripts/check_resources.py"), str(manifest)],
            cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
        )

        assert process.returncode != 0
        result = json.loads(process.stdout)
        discovered = {item["path"]: item for item in result["discovered_resources"]}
        assert discovered[str(directories[0])]["state"] == "exited"
        assert discovered[str(directories[1])]["state"] == "active"
        assert str(simulated) not in discovered
        assert str(directories[0]) in "\n".join(result["errors"])
        assert str(directories[1]) not in "\n".join(result["errors"])
        assert {marker: marker.read_bytes() for marker in before} == before
    finally:
        simulated_marker = directories[1] / "fixtures" / "simulated-runtime" / "owner.json"
        if simulated_marker.exists():
            simulated_marker.unlink()
            simulated_marker.parent.rmdir()
            simulated_marker.parent.parent.rmdir()
        for directory in directories:
            (directory / "owner.json").unlink(missing_ok=True)
            directory.rmdir()


@pytest.mark.parametrize("invalid", ["leftover", "missing-reason", "outside", "widened-root", "junction"])
def test_resource_command_rejects_unfinished_or_unsafe_cleanup(tmp_path: Path, invalid: str) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    resource = {"path": str(ordinary), "status": "retained", "owner": "current-task",
                "reason": "Failure evidence pending review.", "delete_when": "Review is complete."}
    document = {"project_root": str(ROOT), "paths": [resource]}
    if invalid == "leftover":
        resource["status"] = "removed"
    elif invalid == "missing-reason":
        resource.pop("reason")
    elif invalid == "outside":
        resource["path"] = str(ROOT.parent / "external-check-resource")
        resource["status"] = "removed"
    elif invalid == "widened-root":
        document["project_root"] = str(ROOT.parent)
    else:
        link = tmp_path / "linked"
        subprocess.run(
            ["C:/Program Files/PowerShell/7/pwsh.exe", "-NoProfile", "-Command",
             "New-Item -ItemType Junction -Path $env:MYRK_LINK_SOURCE -Target $env:MYRK_LINK_TARGET | Out-Null"],
            env=dict(os.environ, MYRK_LINK_SOURCE=str(link), MYRK_LINK_TARGET=str(ordinary)),
            check=True, capture_output=True,
        )
        resource["path"] = str(link / "missing-child")
        resource["status"] = "removed"
    manifest = tmp_path / "resources.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/check_resources.py"), str(manifest)],
        cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode != 0
    assert json.loads(process.stdout)["errors"]
    assert ordinary.is_dir()


@pytest.mark.parametrize("explicit_base", [False, True])
def test_direct_pytest_owns_its_directory_and_closes_the_loaded_runtime(tmp_path: Path, explicit_base: bool) -> None:
    probe = SOURCE_ROOT / ".scratch" / ("test_direct_" + uuid4().hex + ".py")
    probe.parent.mkdir(exist_ok=True)
    observation = tmp_path / "direct.json"
    probe.write_text(
        "import json, os, tempfile\nfrom pathlib import Path\n"
        "from myrk.supervisor_runtime import SUPERVISOR_RUNTIME\n"
        "def test_direct(tmp_path, request, cache):\n"
        "    assert SUPERVISOR_RUNTIME.error is None\n"
        "    run = Path(os.environ['MYRK_CHECK_RUN_DIRECTORY'])\n"
        "    assert Path(tempfile.gettempdir()).is_relative_to(run)\n"
        "    cache_root = Path(request.config.getini('cache_dir'))\n"
        "    assert cache_root.is_relative_to(run)\n"
        "    cache.set('resource_probe/value', 'checked')\n"
        "    assert (cache_root / 'v/resource_probe/value').is_file()\n"
        "    Path(os.environ['MYRK_CHECK_PROBE']).write_text(json.dumps(\n"
        "        {'run': str(run), 'runtime': str(SUPERVISOR_RUNTIME.directory), 'fixtures': str(tmp_path)}))\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-B", "-m", "pytest", str(probe), "-q"]
    if explicit_base:
        command += ["--basetemp", str(tmp_path / "explicit-fixtures")]
    release = json.loads((SOURCE_ROOT / "myrk/executor-release.json").read_text(encoding="utf-8"))
    binary = ROOT / "myrk/runtime" / release["release"] / release["executable"]
    try:
        process = subprocess.run(
            command, cwd=SOURCE_ROOT,
            env=dict(os.environ, PYTHONPATH=str(SOURCE_ROOT), MYRK_CHECK_PROBE=str(observation), MYRK_EXECUTOR_PATH=str(binary)),
            capture_output=True, text=True, encoding="utf-8",
        )
    finally:
        probe.unlink(missing_ok=True)

    assert process.returncode == 0, process.stdout + process.stderr
    observed = json.loads(observation.read_text(encoding="utf-8"))
    assert not Path(observed["run"]).exists()
    assert not Path(observed["runtime"]).exists()
    assert not Path(observed["fixtures"]).exists()
    summary = ROOT / ".scratch/check-runs/results" / (Path(observed["run"]).name + ".json")
    assert json.loads(summary.read_text(encoding="utf-8"))["runtime_removed"] is True


@pytest.mark.parametrize("location", ["outside-base", "outside-cache", "existing-base"])
def test_direct_pytest_rejects_unowned_output_locations(tmp_path: Path, location: str) -> None:
    probe = SOURCE_ROOT / ".scratch" / ("test_never_" + uuid4().hex + ".py")
    probe.parent.mkdir(exist_ok=True)
    probe.write_text("def test_never():\n    assert False, 'must not execute'\n", encoding="utf-8")
    command = [sys.executable, "-B", "-m", "pytest", str(probe), "-q"]
    important = tmp_path / "important.txt"
    important.write_text("preserve", encoding="utf-8")
    if location == "outside-base":
        command += ["--basetemp", str(ROOT.parent / "unsafe-pytest-base")]
    elif location == "outside-cache":
        command += ["-o", f"cache_dir={ROOT.parent / 'unsafe-pytest-cache'}"]
    else:
        command += ["--basetemp", str(tmp_path)]
    try:
        process = subprocess.run(
            command, cwd=SOURCE_ROOT, env=dict(os.environ, PYTHONPATH=str(SOURCE_ROOT)),
            capture_output=True, text=True, encoding="utf-8",
        )
    finally:
        probe.unlink(missing_ok=True)

    assert process.returncode != 0
    assert "must not execute" not in process.stdout
    expected = "An explicit pytest base must be new or empty" if location == "existing-base" else "Test output must be inside the project's .scratch"
    assert expected in process.stderr
    assert important.read_text(encoding="utf-8") == "preserve"
