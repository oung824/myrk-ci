import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("task_id", ["check-task-id-fixture", None, ""])
def test_backend_command_preserves_optional_task_id_in_owner_result_and_children(
    tmp_path: Path, task_id: str | None,
) -> None:
    probe = tmp_path / "test_task_id_probe.py"
    observation = tmp_path / "observation.json"
    probe.write_text(
        "import json, os, subprocess, sys\nfrom pathlib import Path\n"
        "def test_task_id(tmp_path):\n"
        "    run = Path(os.environ['MYRK_CHECK_RUN_DIRECTORY'])\n"
        "    owner = json.loads((run / 'owner.json').read_text(encoding='utf-8'))\n"
        "    probe = tmp_path / 'test_child_task_id.py'\n"
        "    probe.write_text('def test_child():\\n    assert True\\n', encoding='utf-8')\n"
        "    child = subprocess.run([sys.executable, '-B', os.environ['MYRK_TASK_ID_ENTRY'], "
        "'pytest', str(probe), '-q'], capture_output=True, text=True, check=True, encoding='utf-8')\n"
        "    Path(os.environ['MYRK_TASK_ID_OBSERVATION']).write_text(json.dumps({\n"
        "        'owner': owner, 'task_id': os.environ.get('MYRK_TASK_ID'),\n"
        "        'child': json.loads(child.stdout),\n"
        "    }), encoding='utf-8')\n",
        encoding="utf-8",
    )
    environment = dict(
        os.environ, MYRK_TASK_ID_ENTRY=str(SOURCE_ROOT / "scripts/run_checks.py"),
        MYRK_TASK_ID_OBSERVATION=str(observation),
    )
    if task_id is None:
        environment.pop("MYRK_TASK_ID", None)
    else:
        environment["MYRK_TASK_ID"] = task_id
    process = subprocess.run(
        [sys.executable, "-B", str(SOURCE_ROOT / "scripts/run_checks.py"), "pytest", str(probe), "-q"],
        cwd=SOURCE_ROOT,
        env=environment,
        capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    observed = json.loads(observation.read_text(encoding="utf-8"))
    saved = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    child = observed["child"]
    child_saved = json.loads(Path(child["result_path"]).read_text(encoding="utf-8"))
    for receipt in (observed["owner"], result, saved, child, child_saved):
        if task_id:
            assert receipt["task_id"] == "check-task-id-fixture"
        else:
            assert "task_id" not in receipt
    assert observed["task_id"] == task_id
    for receipt in (result, child):
        assert receipt["runtime_removed"] is True
        assert not Path(receipt["runtime_directory"]).exists()
