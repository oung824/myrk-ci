"""Unhandled event-loop failures cannot be hidden behind a successful test call."""

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest


SOURCE = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("node", "marker", "error"), [
    ("test_real_async_generator_close_failure", "an error occurred during closing of asynchronous generator", "synthetic-generator-close-8149"),
    ("test_real_unhandled_task_failure", "Task exception was never retrieved", "synthetic-unhandled-task-9271"),
])
def test_normal_check_entry_fails_on_actual_event_loop_errors_and_retains_the_original_trace(node, marker, error):
    output = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/runtime-diagnostics" / uuid4().hex
    output.mkdir(parents=True)
    process = subprocess.run([sys.executable, "-B", "scripts/run_checks.py", "pytest",
        "tests/acceptance/runtime_probe_fixture.py::" + node, "-q", "--log-cli-level=ERROR"], cwd=SOURCE, env=os.environ,
        capture_output=True)
    (output / "subprocess.stdout").write_bytes(process.stdout)
    (output / "subprocess.stderr").write_bytes(process.stderr)
    assert process.returncode != 0, process.stdout.decode("utf-8")
    receipt = json.loads(process.stdout.decode("utf-8").splitlines()[-1])
    reports = receipt["retained_reports"]["paths"]
    diagnoses = [path for report in reports for path in Path(report).rglob("qualification_error.json")]
    assert len(diagnoses) == 1
    diagnosis = json.loads(diagnoses[0].read_bytes())
    assert diagnosis["category"] == "unhandled_asyncio_failure"
    assert diagnosis["historical_issue_status"] == "unknown"
    assert marker in diagnosis["records"][0]["message"]
    assert error in diagnosis["records"][0]["traceback"]
    original = diagnoses[0].parent / diagnosis["original_log"]
    assert marker in original.read_text(encoding="utf-8") and error in original.read_text(encoding="utf-8")
    (output / "qualification_error.json").write_bytes(diagnoses[0].read_bytes())
    (output / "asyncio-original.log").write_bytes(original.read_bytes())


def test_planned_action_stderr_with_the_same_words_does_not_fail_qualification():
    process = subprocess.run([sys.executable, "-B", "scripts/run_checks.py", "pytest",
        "tests/acceptance/runtime_probe_fixture.py::test_controlled_action_diagnostic_is_not_an_asyncio_shutdown_failure", "-q"],
        cwd=SOURCE, env=os.environ, capture_output=True)
    assert process.returncode == 0, process.stdout.decode("utf-8") + process.stderr.decode("utf-8")
