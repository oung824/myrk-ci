"""Check the runtime boundary before desktop work can start."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys


SOURCE = Path(__file__).resolve().parents[1]


def service_environment(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(SOURCE),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "MYRK_RUNTIME_ROOT": str(tmp_path / "runtime"),
        "MYRK_DATA_DIRECTORY": str(tmp_path / "conversations"),
    }


def test_formal_validator_import_does_not_prepare_an_execution_copy(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "validate", "--help"],
        cwd=SOURCE, env=service_environment(tmp_path), capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "validate" in result.stdout
    assert not (tmp_path / "runtime").exists()


def test_standalone_check_without_startup_state_stops_before_spawning(tmp_path: Path) -> None:
    program = """
import asyncio
from pathlib import Path
import sys

from myrk.actions import Action
from myrk.checks import execute_check
from myrk.permissions import AccessSettings

root = Path(sys.argv[1])
action = Action(task_id="check-startup-test", kind="check", target="Readiness")
try:
    asyncio.run(execute_check([sys.executable, "-B", "-c", "print('unexpected launch')"], root, 5,
        action, access=AccessSettings(writable_roots=[str(root)]), save=lambda: None,
        evidence_directory=root / "evidence", protected_paths=[]))
except OSError as error:
    assert "可信执行副本" in str(error), str(error)
else:
    raise AssertionError("A check ran before the trusted execution copy was prepared.")
assert action.process_id is None and action.job_name is None
assert not (root / "evidence").exists() and not (root / "runtime").exists()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, str(tmp_path)], cwd=SOURCE,
        env=service_environment(tmp_path), capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_desktop_service_has_one_trusted_execution_copy_at_ready(tmp_path: Path) -> None:
    async def observe() -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "myrk.desktop_service", cwd=SOURCE, env=service_environment(tmp_path),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assert child.stdin is not None and child.stdout is not None
        runtime = tmp_path / "runtime"
        try:
            line = await asyncio.wait_for(child.stdout.readline(), timeout=30)
            assert json.loads(line)["event"] == "ready", line
            copy, = runtime.iterdir()
            owner = json.loads((copy / "owner.json").read_bytes())
            assert owner["purpose"] == "supervisor-runtime"
            assert owner.get("task_id") == os.environ.get("MYRK_TASK_ID")
            assert (copy / "executor.exe").is_file() and (copy / "supervisor.py").is_file()
        finally:
            child.stdin.close()
            try:
                _, stderr = await asyncio.wait_for(child.communicate(), timeout=10)
            except TimeoutError:
                child.kill()
                await child.communicate()
                raise
        assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
        assert not any(runtime.iterdir())

    asyncio.run(observe())
