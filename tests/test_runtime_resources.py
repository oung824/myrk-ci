"""Runtime copies stay with their source and report unfinished cleanup."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def runtime_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    package = source / "myrk"
    package.mkdir(parents=True)
    (source / ".git").mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("supervisor_runtime.py", "project_paths.py"):
        shutil.copyfile(Path(__file__).resolve().parents[1] / "myrk" / name, package / name)
    return source


def run_runtime(source: Path, script: str, **settings: str) -> subprocess.CompletedProcess[str]:
    temporary = source.parent / "system-temporary"
    temporary.mkdir(exist_ok=True)
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                   "MYRK_EXECUTOR_PATH": str(source / "missing-executor.exe"), "TEMP": str(temporary), "TMP": str(temporary)}
    environment.pop("MYRK_RUNTIME_ROOT", None)
    environment.update(settings)
    return subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]);\n" + script, str(source)],
                          cwd=source.parent, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20,
                          creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.mark.parametrize("system_temporary", ["project", "c"])
def test_default_runtime_stays_with_its_source_instead_of_system_temporary(runtime_source: Path, system_temporary: str) -> None:
    settings = {"TEMP": r"C:\Myrk-runtime-test-must-not-be-created", "TMP": r"C:\Myrk-runtime-test-must-not-be-created"} if system_temporary == "c" else {}
    result = run_runtime(runtime_source, """from myrk.supervisor_runtime import SUPERVISOR_RUNTIME as runtime
import json
directory = runtime.directory
runtime.close()
print(json.dumps({"directory": str(directory), "removed": not directory.exists()}))
""", **settings)
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    assert Path(facts["directory"]).parent == runtime_source / ".scratch" / "runtime"
    assert facts["removed"] is True


def test_runtime_persists_its_actual_process_identity_before_preparation(runtime_source: Path) -> None:
    result = run_runtime(runtime_source, """from myrk.supervisor_runtime import SUPERVISOR_RUNTIME as runtime
import ctypes, json, os
from ctypes import wintypes
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
times = [wintypes.FILETIME() for _ in range(4)]
assert kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(value) for value in times))
owner = json.loads((runtime.directory / "owner.json").read_text(encoding="utf-8"))
print(json.dumps({"owner": owner, "pid": os.getpid(), "created_filetime": str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime),
                  "directory": str(runtime.directory), "preparation_failed": runtime.error is not None}))
""")
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    owner = facts["owner"]
    assert owner["pid"] == facts["pid"]
    assert owner["created_filetime"] == facts["created_filetime"]
    assert owner["project_root"] == str(runtime_source)
    assert owner["runtime_directory"] == facts["directory"]
    assert owner["purpose"] == "supervisor-runtime"
    assert owner["created_at"]
    assert facts["preparation_failed"] is True


def test_runtime_can_use_a_project_owned_override_when_system_temporary_points_to_c(runtime_source: Path) -> None:
    destination = runtime_source / ".scratch" / "owned-runtime"
    result = run_runtime(runtime_source, """from myrk.supervisor_runtime import SUPERVISOR_RUNTIME as runtime
import json
print(json.dumps({"directory": str(runtime.directory), "host_roots": [str(path) for path in runtime.host_roots]}))
""", MYRK_RUNTIME_ROOT=str(destination), TEMP=r"C:\Myrk-runtime-test-must-not-be-created", TMP=r"C:\Myrk-runtime-test-must-not-be-created")
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    assert Path(facts["directory"]).parent == destination
    assert str(destination) not in facts["host_roots"]


def test_source_copy_inside_a_repository_accepts_that_repositorys_runtime_root(runtime_source: Path) -> None:
    (runtime_source / ".git").rmdir()
    destination = runtime_source.parent / "repository-runtime"
    result = run_runtime(runtime_source, """from myrk.supervisor_runtime import SUPERVISOR_RUNTIME as runtime
import json
print(json.dumps({"project_root": str(runtime.project_root), "directory": str(runtime.directory)}))
""", MYRK_RUNTIME_ROOT=str(destination))
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True, encoding="utf-8",
    )
    assert Path(facts["project_root"]) == Path(common.stdout.strip()).parent
    assert Path(facts["directory"]).parent == destination


def test_git_worktree_uses_its_shared_repository_for_runtime_ownership(runtime_source: Path) -> None:
    (runtime_source / ".git").rmdir()
    primary = runtime_source.parent / "primary"
    registration = primary / ".git" / "worktrees" / "implementation"
    registration.mkdir(parents=True)
    (registration / "commondir").write_text("../..\n", encoding="utf-8")
    (runtime_source / ".git").write_text("gitdir: " + str(registration) + "\n", encoding="utf-8")
    destination = primary / ".scratch" / "owned-runtime"
    result = run_runtime(runtime_source, """from myrk.supervisor_runtime import SUPERVISOR_RUNTIME as runtime
import json
print(json.dumps({"project_root": str(runtime.project_root), "directory": str(runtime.directory)}))
""", MYRK_RUNTIME_ROOT=str(destination))
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    assert Path(facts["project_root"]) == primary
    assert Path(facts["directory"]).parent == destination


def test_git_worktree_rejects_a_runtime_outside_its_common_repository(runtime_source: Path) -> None:
    (runtime_source / ".git").rmdir()
    primary = runtime_source.parent / "primary"
    registration = primary / ".git" / "worktrees" / "implementation"
    registration.mkdir(parents=True)
    (registration / "commondir").write_text("../..\n", encoding="utf-8")
    (runtime_source / ".git").write_text("gitdir: " + str(registration) + "\n", encoding="utf-8")
    destination = runtime_source / "outside-common-project"
    result = run_runtime(runtime_source, """import json
try:
    from myrk.supervisor_runtime import SUPERVISOR_RUNTIME
except ValueError:
    print(json.dumps({"rejected": True}))
else:
    print(json.dumps({"rejected": False}))
""", MYRK_RUNTIME_ROOT=str(destination))

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["rejected"] is True
    assert not destination.exists()


@pytest.mark.parametrize("location", ["relative-runtime", r"C:\Myrk-runtime-test-must-not-be-created", "outside-source"])
def test_runtime_rejects_an_override_outside_its_actual_source(runtime_source: Path, location: str) -> None:
    destination = str(runtime_source.parent / "outside-source") if location == "outside-source" else location
    result = run_runtime(runtime_source, """import json
try:
    from myrk.supervisor_runtime import SUPERVISOR_RUNTIME
except ValueError:
    print(json.dumps({"rejected": True}))
else:
    print(json.dumps({"rejected": False}))
""", MYRK_RUNTIME_ROOT=destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["rejected"] is True


def test_failed_cleanup_warns_and_persists_a_small_receipt_then_recovers(runtime_source: Path) -> None:
    result = run_runtime(runtime_source, """from myrk.supervisor_runtime import SUPERVISOR_RUNTIME as runtime
import ctypes, json, warnings
from ctypes import wintypes
held = runtime.directory / "held.txt"
held.write_text("synthetic-private-content-not-for-cleanup-receipts", encoding="utf-8")
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
handle = kernel.CreateFileW(str(held), 0x80000000, 1, None, 3, 0, None)
assert handle != wintypes.HANDLE(-1).value
receipt = runtime.project_root / ".scratch" / "runtime" / "cleanup-failures" / (runtime.directory.name + ".json")
try:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        runtime.close()
    facts = {"warnings": [str(item.message) for item in caught], "remaining": runtime.directory.exists(),
             "receipt": receipt.read_text(encoding="utf-8") if receipt.exists() else ""}
finally:
    kernel.CloseHandle(handle)
runtime.close()
facts.update({"recovered": not runtime.directory.exists(), "receipt_removed": not receipt.exists()})
print(json.dumps(facts))
""")
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    assert any("未能删除" in warning for warning in facts["warnings"])
    assert facts["remaining"] is True
    receipt = json.loads(facts["receipt"])
    assert receipt["owner"]["purpose"] == "supervisor-runtime"
    assert receipt["error_type"] == "PermissionError"
    assert receipt["winerror"] == 32
    assert "synthetic-private-content" not in facts["receipt"]
    assert len(facts["receipt"]) < 4096
    assert facts["recovered"] is True
    assert facts["receipt_removed"] is True
