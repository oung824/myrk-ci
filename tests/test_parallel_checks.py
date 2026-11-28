"""Parallel backend checks preserve the collected suite and its process isolation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from textwrap import dedent

import pytest


SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def synthetic_check_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    for relative in (
        "scripts/run_checks.py",
        "myrk/project_paths.py",
        "myrk/check_outputs.py",
        "myrk/check_plugin.py",
        "myrk/windows_jobs.py",
        "conftest.py",
    ):
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / relative, destination)
    for relative in ("scripts/parallel_checks.py", "scripts/pytest_shards.py"):
        if (SOURCE / relative).exists():
            shutil.copyfile(SOURCE / relative, source / relative)
    (source / "myrk/__init__.py").write_text("", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
        'pythonpath = ["."]\ncache_dir = ".scratch/check-runs/cache/pytest"\n',
        encoding="utf-8",
    )
    (source / "tests/acceptance").mkdir(parents=True)
    (source / "tests/conftest.py").write_text(
        dedent("""\
            import json
            import os
            from pathlib import Path
            from time import monotonic, sleep

            import pytest


            @pytest.fixture
            def lane_barrier():
                def wait_for_peer(lane):
                    probe = Path(os.environ["MYRK_PARALLEL_PROBE"])
                    probe.mkdir(parents=True, exist_ok=True)
                    (probe / (lane + ".ready.json")).write_text(json.dumps({
                        "pid": os.getpid(),
                        "runtime_directory": os.environ["MYRK_CHECK_RUN_DIRECTORY"],
                    }), encoding="utf-8")
                    peer = "other" if lane == "formal" else "formal"
                    deadline = monotonic() + 20
                    while not (probe / (peer + ".ready.json")).exists():
                        assert monotonic() < deadline, "Both groups must reach the barrier together"
                        sleep(.01)
                return wait_for_peer
            """),
        encoding="utf-8",
    )
    (source / "tests/acceptance/test_formal_scenarios.py").write_text(
        dedent("""\
            import pytest


            @pytest.mark.parametrize("scenario", ["alpha", "beta"])
            def test_selected_formal_scenario_traverses_real_main_service_and_sdk(scenario, lane_barrier):
                if scenario == "alpha":
                    lane_barrier("formal")
                assert scenario in ("alpha", "beta")


            def test_regular_check_in_the_formal_file(lane_barrier):
                lane_barrier("other")
            """),
        encoding="utf-8",
    )
    (source / "tests/test_regular.py").write_text(
        "def test_regular_check():\n    assert 2 + 2 == 4\n", encoding="utf-8",
    )
    return source


def test_parallel_cli_runs_the_complete_suite_in_two_overlapping_processes(
    synthetic_check_source: Path,
) -> None:
    source = synthetic_check_source
    probe = source / ".scratch/probe"
    environment = dict(
        os.environ, PYTHONPATH=str(source), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        MYRK_PARALLEL_PROBE=str(probe), MYRK_TEST_SCHEDULING="legacy",
    )
    process = subprocess.run(
        [sys.executable, "-B", "scripts/run_checks.py", "pytest-parallel", "-q"],
        cwd=source, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )

    assert process.returncode == 0, process.stdout + process.stderr
    aggregate = json.loads(process.stdout.strip().splitlines()[-1])
    assert aggregate["mode"] == "pytest-parallel"
    assert aggregate["execution_mode"] == "parallel"
    assert "memory" not in aggregate
    assert aggregate["exit_code"] == 0
    assert aggregate["runtime_removed"] is True
    assert aggregate["collection_receipt"]["runtime_removed"] is True
    assert aggregate["completeness"]["complete"] is True
    assert aggregate["completeness"]["expected_count"] == 4
    assert aggregate["completeness"]["reported_count"] == 4
    assert json.loads(Path(aggregate["result_path"]).read_text(encoding="utf-8")) == aggregate
    expected = {
        "formal": [
            "tests/acceptance/test_formal_scenarios.py::test_selected_formal_scenario_traverses_real_main_service_and_sdk[alpha]",
            "tests/acceptance/test_formal_scenarios.py::test_selected_formal_scenario_traverses_real_main_service_and_sdk[beta]",
        ],
        "other": [
            "tests/acceptance/test_formal_scenarios.py::test_regular_check_in_the_formal_file",
            "tests/test_regular.py::test_regular_check",
        ],
    }
    observed_processes = {}
    for lane, nodeids in expected.items():
        result = aggregate["lanes"][lane]
        assert sorted(result["expected_nodeids"]) == sorted(nodeids)
        receipt = result["receipt"]
        assert receipt["exit_code"] == 0
        assert receipt["runtime_removed"] is True
        observations = [
            Path(path) / "pytest-shards/observation.json"
            for path in receipt["retained_reports"]["paths"]
        ]
        observation = json.loads(next(path for path in observations if path.is_file()).read_text(encoding="utf-8"))
        assert sorted(observation["selected_nodeids"]) == sorted(nodeids)
        assert sorted((report["nodeid"], report["when"], report["outcome"]) for report in observation["reports"]) == sorted(
            (nodeid, phase, "passed") for nodeid in nodeids for phase in ("setup", "call", "teardown")
        )
        assert all(report["duration"] >= 0 for report in observation["reports"])
        observed_processes[lane] = json.loads((probe / (lane + ".ready.json")).read_text(encoding="utf-8"))
        assert observed_processes[lane]["runtime_directory"] == receipt["runtime_directory"]
    assert observed_processes["formal"]["pid"] != observed_processes["other"]["pid"]
    assert observed_processes["formal"]["runtime_directory"] != observed_processes["other"]["runtime_directory"]


@pytest.mark.parametrize("failed_lane", ["formal", "other", "both"])
def test_parallel_cli_preserves_assertion_failures_and_finishes_both_groups(
    synthetic_check_source: Path, failed_lane: str,
) -> None:
    source = synthetic_check_source
    (source / "tests/acceptance/test_formal_scenarios.py").write_text(
        dedent(f"""\
            import pytest


            @pytest.mark.parametrize("scenario", ["alpha", "beta"])
            def test_selected_formal_scenario_traverses_real_main_service_and_sdk(scenario, lane_barrier):
                if scenario == "alpha":
                    lane_barrier("formal")
                    assert {failed_lane not in ("formal", "both")}, "original formal assertion evidence"
                assert scenario in ("alpha", "beta")


            def test_regular_check_in_the_formal_file(lane_barrier):
                lane_barrier("other")
                assert {failed_lane not in ("other", "both")}, "original other assertion evidence"
            """),
        encoding="utf-8",
    )
    environment = dict(
        os.environ, PYTHONPATH=str(source), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        MYRK_PARALLEL_PROBE=str(source / ".scratch/probe"), MYRK_TEST_SCHEDULING="legacy",
    )
    process = subprocess.run(
        [sys.executable, "-B", "scripts/run_checks.py", "pytest-parallel", "-q"],
        cwd=source, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )

    assert process.returncode == 1, process.stdout + process.stderr
    aggregate = json.loads(process.stdout.strip().splitlines()[-1])
    assert aggregate["exit_code"] == 1
    assert aggregate["runtime_removed"] is True
    assert aggregate["execution_mode"] == "parallel"
    assert aggregate["completeness"]["complete"] is True
    assert aggregate["completeness"]["expected_count"] == 4
    assert aggregate["completeness"]["reported_count"] == 4
    expected = {
        "formal": [
            "tests/acceptance/test_formal_scenarios.py::test_selected_formal_scenario_traverses_real_main_service_and_sdk[alpha]",
            "tests/acceptance/test_formal_scenarios.py::test_selected_formal_scenario_traverses_real_main_service_and_sdk[beta]",
        ],
        "other": [
            "tests/acceptance/test_formal_scenarios.py::test_regular_check_in_the_formal_file",
            "tests/test_regular.py::test_regular_check",
        ],
    }
    receipts = [aggregate["collection_receipt"]]
    for lane, nodeids in expected.items():
        receipt = aggregate["lanes"][lane]["receipt"]
        receipts.append(receipt)
        failed = failed_lane in (lane, "both")
        assert receipt["exit_code"] == (1 if failed else 0)
        assert receipt["runtime_removed"] is True
        observations = [
            Path(path) / "pytest-shards/observation.json"
            for path in receipt["retained_reports"]["paths"]
        ]
        observation = json.loads(next(path for path in observations if path.is_file()).read_text(encoding="utf-8"))
        assert sorted(observation["selected_nodeids"]) == sorted(nodeids)
        assert sorted((report["nodeid"], report["when"], report["outcome"]) for report in observation["reports"]) == sorted(
            (nodeid, phase, "failed" if failed and nodeid == nodeids[0] and phase == "call" else "passed")
            for nodeid in nodeids for phase in ("setup", "call", "teardown")
        )
        if failed:
            failure_output = "\n".join(
                Path(path).read_text(encoding="utf-8") for path in receipt["retained"]["paths"]
            )
            assert f"original {lane} assertion evidence" in failure_output
    for receipt in receipts:
        for command in receipt["commands"]:
            assert command["launcher_priority_class"] == 0x4000
            assert command["interpreter_priority_class"] == 0x4000
