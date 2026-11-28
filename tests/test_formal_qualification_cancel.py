from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest

from myrk.check_plugin import project_test_directory
from scripts.run_checks import ROOT, run_check


def read_observation(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def test_canceling_formal_offline_check_removes_its_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    task = os.environ.get("MYRK_TASK_ID", "formal-cancel-test")
    task_id = f"{task}.formal-cancel.{uuid4().hex}"
    monkeypatch.setenv("MYRK_TASK_ID", task_id)
    legacy_preparations = project_test_directory(ROOT / ".scratch/acceptance-runs")
    legacy_preparations.mkdir(parents=True, exist_ok=True)
    other_preparation = legacy_preparations / f"control-{task_id}"
    other_preparation.mkdir()
    other_marker = other_preparation / "owner.json"
    other_contents = '{"task_id":"another-check"}'
    other_marker.write_text(other_contents, encoding="utf-8")
    runtime_parent = ROOT / ".scratch/check-runs/runtime-tmp"
    stop, finished = Event(), Event()
    observed: Path | None = None
    owned_job_exited = False

    def stop_after_preparation() -> None:
        nonlocal observed
        deadline = monotonic() + 45
        try:
            while not finished.is_set() and monotonic() < deadline:
                for run in runtime_parent.glob("b-*"):
                    if read_observation(run / "owner.json").get("task_id") != task_id:
                        continue
                    for parent in (legacy_preparations, run / "offline-qualification"):
                        for marker in parent.glob("*/owner.json"):
                            if read_observation(marker).get("task_id") != task_id:
                                continue
                            prepared = marker.parent
                            if (read_observation(prepared / "prepared.json")
                                    and read_observation(prepared / "ledger.json")):
                                observed = prepared
                                return
                finished.wait(.05)
        finally:
            stop.set()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            observer = executor.submit(stop_after_preparation)
            try:
                receipt = run_check("pytest", ["tests/acceptance/test_selected.py", "--acceptance-case", "formal.question",
                    "--acceptance-protocol", "deepseek", "--acceptance-mode", "offline", "-q"], stop=stop)
            finally:
                finished.set()
                observer.result()
        assert observed is not None, receipt
        assert receipt["exit_code"] == 130, receipt
        assert receipt["commands"][0]["job_active_processes"] == 0
        assert receipt["commands"][0]["interpreter_exit_confirmed"] is True
        owned_job_exited = True
        assert receipt["runtime_removed"] is True
        assert not observed.exists(), f"Canceled check left its prepared offline bundle: {observed}"
        assert observed.is_relative_to(Path(receipt["runtime_directory"]))
        assert other_marker.read_text(encoding="utf-8") == other_contents
    finally:
        other_marker.unlink(missing_ok=True)
        if other_preparation.exists():
            other_preparation.rmdir()
        if (owned_job_exited and observed is not None and observed.parent == legacy_preparations
                and observed.exists()):
            remaining = project_test_directory(observed)
            assert remaining.parent == legacy_preparations
            owner = read_observation(remaining / "owner.json")
            assert owner.get("owner") == "agent-acceptance-preparation"
            assert owner.get("task_id") == task_id
            assert owner.get("runtime_directory") == str(remaining)
            shutil.rmtree(remaining)


def test_public_prepare_live_rejects_the_check_runtime_directory() -> None:
    from tests.support.agent_acceptance import AcceptancePlan, SharedControlError, builtin_registry, prepare_live

    check_run = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"])
    root = check_run / "public-persistent-preparation"
    plan = AcceptancePlan(cases=["ordinary.question"], protocols=["deepseek"], mode="prepare-live")
    with pytest.raises(SharedControlError, match="outside disposable check runs"):
        prepare_live(plan, root=root, registry=builtin_registry())
    assert not root.exists()
