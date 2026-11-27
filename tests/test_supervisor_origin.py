"""Normal commands keep the program version captured before task edits."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_first_command_uses_startup_version_after_task_updates_its_application_copy(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    copied = tmp_path / "app"
    shutil.copytree(source / "myrk", copied / "myrk", ignore=shutil.ignore_patterns("__pycache__", "runtime"))
    driver = tmp_path / "case.py"
    driver.write_text('''import asyncio, json, sys
from hashlib import sha256
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from myrk.conversation import Conversation, TaskScope
from myrk.checks import CheckReceipt
root = Path(sys.argv[1])
metadata = root / "myrk" / "executor-release.json"
changed = json.loads(metadata.read_text(encoding="utf-8"))
changed["sha256"] = "0" * 64
steps = [("read_text", {"path": str(metadata)}),
         ("write_text", {"path": str(metadata), "content": json.dumps(changed)}),
         ("run_command", {"program": r"C:\\Program Files\\PowerShell\\7\\pwsh.exe", "arguments": ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "[Console]::WriteLine('startup-origin-ok')"]})]
async def model(messages, _info):
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    if len(results) < len(steps):
        name, arguments = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"origin-{len(results)}")}
    else:
        yield "本次正常程序来源检查完成。"
async def main():
    turn = await Conversation(root.parent / "records" / "session.json").ask("更新本任务应用副本中的发布资料后运行普通输出命令", document=None,
        scope=TaskScope(root=str(root), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=model, profile={"context_window": 128000, "supports_json_schema_output": True, "supports_json_object_output": True}))
    assert turn.status == "completed", turn.error
    assert turn.actions[1].status == "completed", turn.actions[1].error
    command = turn.actions[-1]
    assert command.status == "completed", command.error
    assert command.stdout.strip() == "startup-origin-ok", command.model_dump()
    assert command.sandbox_type == "windowsMxc", command.model_dump()
    assert command.cleanup_complete is True
    receipt = CheckReceipt.model_validate_json(Path(command.receipt_path).read_bytes())
    assert receipt.action_id == command.id and receipt.exit_code == command.exit_code == 0
    assert receipt.sandbox_type == command.sandbox_type == "windowsMxc"
    assert receipt.model_fields_set == set(CheckReceipt.model_fields)
    assert receipt.argv == [command.prepared_parameters["program"], *command.prepared_parameters["arguments"]]
    assert receipt.cwd == command.cwd == str(root)
    assert receipt.input_files_before == receipt.input_files_after == {}
    assert receipt.started_at is not None and receipt.completed_at is not None and receipt.started_at <= receipt.completed_at
    assert receipt.stdout_sha256 == sha256(Path(command.stdout_path).read_bytes()).hexdigest()
    assert receipt.stderr_sha256 == sha256(Path(command.stderr_path).read_bytes()).hexdigest()
    print("startup-origin-ok")
asyncio.run(main())
''', encoding="utf-8")
    executable = os.environ.get("MYRK_EXECUTOR_PATH", str(source / "myrk" / "runtime" / "0.166.0" / "codex-exec-server-x86_64-pc-windows-msvc.exe"))
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "MYRK_EXECUTOR_PATH": executable, "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    result = subprocess.run([sys.executable, str(driver), str(copied)], cwd=copied, env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=60,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "startup-origin-ok"
