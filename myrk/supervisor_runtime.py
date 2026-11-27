"""Capture the supervisor's entire load chain before a task can edit sources.

Only private copies are locked. No shared interpreter or dependency is changed.
The child has zip-only Python search paths and an exact native-module map, so
new loose files, working directories, .pth files and Python environment values
cannot become host imports. Full access still has no general task sandbox.
"""

import atexit
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from time import monotonic
import warnings
import zipfile

from myrk.project_paths import project_root


class SupervisorRuntime:
    def __init__(self) -> None:
        source = Path(__file__).resolve().parents[1]
        self.project_root = project_root(source)
        parent = Path(os.environ.get("MYRK_RUNTIME_ROOT", str(self.project_root / ".scratch" / "runtime")))
        if not parent.is_absolute() or not parent.resolve().is_relative_to(self.project_root):
            raise ValueError("MYRK_RUNTIME_ROOT 必须是本项目目录内的绝对路径。")
        parent = parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="myrk-run-", dir=parent))
        self.cleanup_receipt = self.project_root / ".scratch" / "runtime" / "cleanup-failures" / (self.directory.name + ".json")
        self.binary = self.directory / "executor.exe"
        self.host_roots = list(dict.fromkeys([Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]))
        self.handles: list[int] = []
        self.error: str | None = None
        self.preparation_seconds = 0.0
        self.bytes = 0
        self.files = 0
        self.owner: dict[str, str | int] = {}
        atexit.register(self.close)
        started = monotonic()
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(value) for value in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.owner = {"pid": os.getpid(), "created_filetime": str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime),
                          "project_root": str(self.project_root), "purpose": "supervisor-runtime", "created_at": datetime.now(timezone.utc).isoformat(),
                          "runtime_directory": str(self.directory)}
            (self.directory / "owner.json").write_text(json.dumps(self.owner, ensure_ascii=False, indent=2), encoding="utf-8")
            self.capture()
        except (OSError, ValueError) as error:
            self.error = str(error)
        self.preparation_seconds = monotonic() - started

    def capture(self) -> None:
        package = Path(__file__).parent
        release = json.loads((package / "executor-release.json").read_text(encoding="utf-8"))
        source_binary = Path(os.environ.get("MYRK_EXECUTOR_PATH", str(package / "runtime" / release["release"] / release["executable"])))
        if not source_binary.is_absolute() or not source_binary.is_file():
            raise OSError("缺少固定官方执行程序，未建立可信执行副本。")
        binary = source_binary.read_bytes()
        if sha256(binary).hexdigest() != release["sha256"]:
            raise ValueError("官方执行程序的文件指纹（核对内容的编号）不符，未建立可信执行副本。")
        self.binary.write_bytes(binary)
        base = Path(sys.base_prefix)
        shutil.copyfile(base / "python.exe", self.directory / "python.exe")
        for source in base.glob("*.dll"):
            shutil.copyfile(source, self.directory / source.name)
        native: dict[str, str] = {}
        for source in (base / "DLLs").iterdir():
            if source.suffix.lower() in {".dll", ".pyd"}:
                shutil.copyfile(source, self.directory / source.name)
                if source.suffix.lower() == ".pyd":
                    native[source.name.split(".")[0]] = source.name
        with zipfile.ZipFile(self.directory / "stdlib.zip", "w") as archive:
            for source in (base / "Lib").rglob("*.py"):
                relative = source.relative_to(base / "Lib")
                if not {"site-packages", "__pycache__"}.intersection(relative.parts):
                    archive.write(source, relative.as_posix())
        with zipfile.ZipFile(self.directory / "runtime.zip", "w") as archive:
            for name in ("__init__.py", "check_runner.py", "executor.py", "permissions.py", "executor-release.json"):
                archive.write(package / name, "myrk/" + name)
            for name in ("pydantic", "pydantic_core", "annotated_types", "typing_inspection", "typing_extensions"):
                spec = importlib.util.find_spec(name)
                if spec is None or spec.origin is None:
                    raise OSError("缺少监督程序所需的校验依赖，未建立可信执行副本。")
                origin = Path(spec.origin)
                sources = origin.parent.rglob("*") if spec.submodule_search_locations is not None else [origin]
                for source in sources:
                    if source.suffix == ".py" and "__pycache__" not in source.parts:
                        archive.write(source, source.relative_to(origin.parent.parent).as_posix() if spec.submodule_search_locations is not None else source.name)
                    elif source.suffix == ".pyd":
                        shutil.copyfile(source, self.directory / source.name)
                        native[name + "." + source.name.split(".")[0]] = source.name
        (self.directory / f"python{sys.version_info.major}{sys.version_info.minor}._pth").write_text("stdlib.zip\nruntime.zip\n", encoding="utf-8")
        bootstrap = f'''import sys, importlib.util
from importlib.machinery import ExtensionFileLoader
native = {native!r}
root = sys.executable.rsplit("\\\\", 1)[0]
class NativeModules:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in native:
            location = root + "\\\\" + native[fullname]
            return importlib.util.spec_from_file_location(fullname, location, loader=ExtensionFileLoader(fullname, location))
sys.meta_path.insert(0, NativeModules())
from myrk.check_runner import main
import asyncio
sys.exit(asyncio.run(main()))
'''
        (self.directory / "supervisor.py").write_text(bootstrap, encoding="utf-8")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes, kernel.CreateFileW.restype = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE
        for path in [self.directory, *self.directory.iterdir()]:
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x02000000 if path.is_dir() else 0, None)
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            os.set_handle_inheritable(handle, True)
            self.handles.append(handle)
            if path.is_file():
                self.files += 1
                self.bytes += path.stat().st_size

    def command(self) -> list[str]:
        if self.error is not None:
            raise OSError("可信执行副本准备失败，已停止；没有回退到可改写的应用源码：" + self.error)
        return [str(self.directory / "python.exe"), "-I", "-S", "-B", str(self.directory / "supervisor.py")]

    def close(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for handle in self.handles:
            kernel.CloseHandle(handle)
        self.handles.clear()
        if self.directory.exists():
            try:
                shutil.rmtree(self.directory)
            except OSError as error:
                receipt = {"owner": self.owner, "runtime_directory": str(self.directory), "failed_at": datetime.now(timezone.utc).isoformat(),
                           "error_type": type(error).__name__, "errno": error.errno, "winerror": getattr(error, "winerror", None)}
                try:
                    if not self.cleanup_receipt.parent.resolve().is_relative_to(self.project_root):
                        raise OSError("清理回执目录不在项目内。")
                    self.cleanup_receipt.parent.mkdir(parents=True, exist_ok=True)
                    self.cleanup_receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError:
                    warnings.warn(f"运行副本未能删除，清理回执也未能保存；请检查：{self.directory}", RuntimeWarning, stacklevel=2)
                else:
                    warnings.warn(f"运行副本未能删除，清理失败回执：{self.cleanup_receipt}", RuntimeWarning, stacklevel=2)
                return
        try:
            self.cleanup_receipt.unlink(missing_ok=True)
        except OSError:
            warnings.warn(f"运行副本已删除，但旧清理回执未能删除：{self.cleanup_receipt}", RuntimeWarning, stacklevel=2)


SUPERVISOR_RUNTIME = SupervisorRuntime()
