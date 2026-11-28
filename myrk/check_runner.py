"""Owned receipt writer; arbitrary commands only enter the official executor."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import asyncio
import sys
import stat
import tempfile
from pathlib import Path
from typing import Any

from myrk.executor import OfficialExecutor
from myrk.permissions import AccessSettings


async def main() -> int:
    command = json.loads(sys.stdin.readline())
    receipt = Path(command["receipt_path"])
    network_counter = 0
    async def network_decision(request: dict[str, Any]) -> bool:
        nonlocal network_counter
        network_counter += 1
        path = receipt.parent / f"{command['action_id']}.network-{network_counter}.json"
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(request), encoding="utf-8")
        pending.replace(path)
        while not path.with_suffix(".decision").exists():
            if receipt.with_suffix(".stop").exists():
                return False
            await asyncio.sleep(.01)
        return json.loads(path.with_suffix(".decision").read_text(encoding="utf-8"))["allowed"] is True
    private_runtime = Path(sys.executable).parent
    state = tempfile.TemporaryDirectory(prefix="myrk-exec-", dir=private_runtime.parent)
    executor = OfficialExecutor(Path(state.name), binary=private_runtime / "executor.exe", protected_paths=[*(Path(path) for path in command["protected_paths"]), Path(state.name)], runtime_roots=[private_runtime, *(Path(path) for path in command["runtime_roots"])], network_decision=network_decision)
    def output(stream: str, block: bytes) -> None:
        with Path(command[stream + "_path"]).open("ab") as destination:
            destination.write(block)
    def started(sandbox_type: str) -> None:
        receipt.with_suffix(".started").write_text(sandbox_type, encoding="utf-8")
    try:
        cwd = Path(command["cwd"])
        access = AccessSettings.model_validate(command["access"])
        if access.read_only and command["extra_write_paths"]:
            raise PermissionError("只读检查不能申请或取得额外写入位置。")
        if access.sandbox != "full_access" and any(Path(path).resolve().is_relative_to(root) for path in command["extra_write_paths"] for root in executor.runtime_roots):
            raise PermissionError("应用正在使用的运行资料不能作为任务的额外写入位置。")
        sandbox = await executor.verify_scope(cwd, access)
        if sandbox is not None:
            entries = sandbox["permissions"]["file_system"]["entries"]
            denied = [entry for entry in entries if entry["access"] == "deny"]
            entries[:] = [entry for entry in entries if entry["access"] != "deny"]
            entries.extend({"path": {"type": "path", "path": Path(path).as_uri()}, "access": "write"} for path in command["extra_write_paths"])
            entries.extend(denied)
        environment = {"PYTHONIOENCODING": "utf-8", "PYTHONPATH": command["application_root"]}
        inputs = list(dict.fromkeys(candidate.absolute() for argument in command["argv"]
            if (candidate := cwd / argument).is_file() and candidate.resolve().is_relative_to(cwd)))
        async def observe(path: Path) -> dict[str, Any] | None:
            try:
                resolved = path.resolve(strict=True)
                if (not resolved.is_relative_to(cwd) or not stat.S_ISREG(path.lstat().st_mode)
                        or any(resolved.is_relative_to(Path(root).resolve()) for root in [*command["protected_paths"], *access.denied_paths])):
                    return None
                raw = await executor.read_bytes(resolved, cwd, access)
                return {"sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)}
            except (OSError, ValueError):
                return None
        before = {str(path): await observe(path) for path in inputs}
        started_at = datetime.now(timezone.utc).isoformat()
        running = asyncio.create_task(executor.run(command["argv"], cwd, sandbox=sandbox, timeout=None, output=output, environment=environment, started=started, network=access.network))
        while not running.done():
            if receipt.with_suffix(".stop").exists():
                running.cancel()
                break
            await asyncio.sleep(.01)
        result = await running
        pending = receipt.with_suffix(".tmp")
        after = {str(path): await observe(path) for path in inputs}
        pending.write_text(json.dumps({"action_id": command["action_id"], "exit_code": result["exitCode"], "sandbox_type": result["sandboxType"],
            "argv": command["argv"], "cwd": str(cwd), "input_files_before": before, "input_files_after": after,
            "started_at": started_at, "completed_at": datetime.now(timezone.utc).isoformat(),
            **{stream + "_sha256": sha256(path.read_bytes() if path.exists() else b"").hexdigest()
                for stream in ("stdout", "stderr") for path in [Path(command[stream + "_path"])]}}), encoding="utf-8")
        pending.replace(receipt)
        return int(result["exitCode"])
    finally:
        await executor.close()
        state.cleanup()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
