"""Parallel backend checks preserve the collected suite and its process isolation."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from textwrap import dedent
from time import monotonic, sleep

import pytest


SOURCE = Path(__file__).resolve().parents[1]


def run_synthetic_cli(source: Path, *arguments: str, scheduling: str | None = "duration") -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, PYTHONPATH=str(source), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        MYRK_TEST_TIMING_REFERENCE=str(source / ".scratch/reference.json"))
    if scheduling is None:
        environment.pop("MYRK_TEST_SCHEDULING", None)
    else:
        environment["MYRK_TEST_SCHEDULING"] = scheduling
    return subprocess.run(
        [sys.executable, "-B", "scripts/run_checks.py", "pytest-parallel", *arguments, "-q"],
        cwd=source, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


@pytest.fixture
def duration_check_source(synthetic_check_source: Path) -> Path:
    source = synthetic_check_source
    (source / "tests/acceptance/test_formal_scenarios.py").unlink()
    (source / "tests/test_regular.py").unlink()
    (source / "tests/conftest.py").write_text("", encoding="utf-8")
    for name in ("a", "b", "c", "d"):
        (source / f"tests/test_{name}.py").write_text(
            "def test_first():\n    assert True\n\ndef test_second():\n    assert True\n", encoding="utf-8")
    (source / ".scratch").mkdir(exist_ok=True)
    (source / ".scratch/reference.json").write_text(json.dumps({"node_seconds": {
        "tests/test_a.py::test_first": 6, "tests/test_a.py::test_second": 3,
        "tests/test_b.py::test_first": 4, "tests/test_b.py::test_second": 4,
        "tests/test_c.py::test_first": 3, "tests/test_c.py::test_second": 3,
        "tests/test_d.py::test_first": 3, "tests/test_d.py::test_second": 2,
    }}), encoding="utf-8")
    return source


def test_duration_cli_splits_only_allowed_formal_parameters_and_discovers_new_parameters(
    synthetic_check_source: Path,
) -> None:
    source = synthetic_check_source
    (source / "tests/conftest.py").write_text("", encoding="utf-8")
    (source / "tests/test_regular.py").unlink()
    formal = source / "tests/acceptance/test_formal_scenarios.py"
    template = dedent('''\
        import pytest

        @pytest.mark.parametrize("scenario", CASES)
        def test_selected_formal_scenario_traverses_real_main_service_and_sdk(scenario):
            assert scenario

        def test_remaining_first():
            assert True

        @pytest.mark.parametrize("scenario", ["alpha", "beta"])
        def test_selected_formal_scenario_traverses_real_main_service_and_sdk_unapproved(scenario):
            assert scenario
        ''')
    prefix = "tests/acceptance/test_formal_scenarios.py::"
    allowed = prefix + "test_selected_formal_scenario_traverses_real_main_service_and_sdk"
    remainder = [prefix + "test_remaining_first", *[
        allowed + "_unapproved[" + case + "]" for case in ("alpha", "beta")]]
    (source / ".scratch").mkdir(exist_ok=True)
    (source / ".scratch/reference.json").write_text(json.dumps({"node_seconds": {
        allowed + "[alpha]": 10, allowed + "[beta]": 9,
        remainder[0]: 2, remainder[1]: 3, remainder[2]: 3,
    }}), encoding="utf-8")
    for cases in (['alpha', 'beta'], ['alpha', 'beta', 'gamma']):
        formal.write_text(template.replace("CASES", repr(cases)), encoding="utf-8")
        process = run_synthetic_cli(source, "tests/acceptance/test_formal_scenarios.py")
        assert process.returncode == 0, process.stdout + process.stderr
        result = json.loads(process.stdout.strip().splitlines()[-1])
        assert result["completeness"]["complete"] is True
        assert result["runtime_removed"] is True
        lanes = [lane["expected_nodeids"] for name, lane in result["lanes"].items() if name != "serial"]
        expected_lanes = ([[allowed + "[alpha]"], [allowed + "[beta]", *remainder]] if len(cases) == 2
            else [[allowed + "[alpha]", allowed + "[gamma]"], [allowed + "[beta]", *remainder]])
        assert lanes == expected_lanes
        assert result["timing_estimates"]["unknown_nodes"] == len(cases) - 2
        assert any(all(node in lane for node in remainder) for lane in lanes)
        collected = [allowed + "[" + case + "]" for case in cases] + remainder
        assert sorted(node for lane in lanes for node in lane) == sorted(collected)
        assert all(lane == [node for node in collected if node in lane] for lane in lanes)
        assert result["timing_index"]["update_status"] == "not_eligible"


def test_parallel_cli_records_native_phase_timestamps(duration_check_source: Path) -> None:
    process = run_synthetic_cli(duration_check_source, "tests/test_a.py", "tests/test_b.py")
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result["completeness"]["complete"] is True
    for lane in result["lanes"].values():
        if not lane["expected_nodeids"]:
            continue
        observation = json.loads(Path(lane["observation_path"]).read_text(encoding="utf-8"))
        for node in lane["expected_nodeids"]:
            phases = {report["when"]: report for report in observation["reports"] if report["nodeid"] == node}
            assert all(0 < report["start"] <= report["stop"] for report in phases.values())
            assert phases["setup"]["stop"] <= phases["call"]["start"]
            assert phases["call"]["stop"] <= phases["teardown"]["start"]


def test_parallel_cli_defaults_to_duration_and_balances_whole_files_in_collection_order(
    duration_check_source: Path,
) -> None:
    process = run_synthetic_cli(duration_check_source, scheduling=None)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result["scheduling_mode"] == "duration"
    assert result["completeness"]["complete"] is True
    assert result["runtime_removed"] is True
    assert result["lanes"]["lane1"]["expected_nodeids"] == [
        "tests/test_a.py::test_first", "tests/test_a.py::test_second",
        "tests/test_d.py::test_first", "tests/test_d.py::test_second",
    ]
    assert result["lanes"]["lane2"]["expected_nodeids"] == [
        "tests/test_b.py::test_first", "tests/test_b.py::test_second",
        "tests/test_c.py::test_first", "tests/test_c.py::test_second",
    ]


@pytest.mark.parametrize("scheduling", ["legacy", "duration"])
def test_cli_runs_automatically_discovered_serial_files_after_all_parallel_runtime_cleanup(
    duration_check_source: Path, scheduling: str,
) -> None:
    source = duration_check_source
    (source / "tests/test_resource_audit_serial.py").write_text(dedent("""\
        import os
        from pathlib import Path

        def test_audit_has_exclusive_runtime():
            current = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"])
            assert list(current.parent.iterdir()) == [current]
        """), encoding="utf-8")
    process = run_synthetic_cli(source, scheduling=scheduling)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    serial = result["lanes"]["serial"]
    assert serial["expected_nodeids"] == ["tests/test_resource_audit_serial.py::test_audit_has_exclusive_runtime"]
    assert result["completeness"]["complete"] is True
    assert serial["receipt"]["runtime_removed"] is True
    ordinary = [lane["receipt"] for name, lane in result["lanes"].items() if name != "serial" and lane["receipt"]]
    assert all(receipt["runtime_removed"] is True for receipt in ordinary)
    assert all(receipt["finished_at"] <= serial["receipt"]["started_at"] for receipt in ordinary)


def test_duration_cli_learns_complete_success_and_discovers_new_renamed_and_deleted_files(
    duration_check_source: Path,
) -> None:
    source = duration_check_source
    first = run_synthetic_cli(source)
    assert first.returncode == 0, first.stdout + first.stderr
    index = source / ".scratch/check-runs/timings/backend.json"
    learned = json.loads(index.read_text(encoding="utf-8"))
    assert learned["node_seconds"]["tests/test_a.py::test_first"] < 1
    (source / "tests/test_d.py").rename(source / "tests/test_z.py")
    (source / "tests/test_c.py").unlink()
    (source / "tests/test_new.py").write_text("def test_new():\n    assert True\n", encoding="utf-8")
    second = run_synthetic_cli(source)
    assert second.returncode == 0, second.stdout + second.stderr
    result = json.loads(second.stdout.strip().splitlines()[-1])
    assert result["completeness"]["complete"] is True
    assert result["completeness"]["expected_count"] == 7
    assert result["timing_index"]["read_status"] == "loaded"
    assert result["timing_index"]["update_status"] == "updated"
    assert result["timing_estimates"]["unknown_nodes"] == 3
    assert set(json.loads(index.read_text(encoding="utf-8"))["node_seconds"]) == {
        "tests/test_a.py::test_first", "tests/test_a.py::test_second",
        "tests/test_b.py::test_first", "tests/test_b.py::test_second",
        "tests/test_z.py::test_first", "tests/test_z.py::test_second", "tests/test_new.py::test_new",
    }
    assert index.stat().st_size <= 2 * 1024 ** 2


def test_cli_filtered_configuration_and_failed_runs_preserve_the_last_complete_success(
    duration_check_source: Path,
) -> None:
    source = duration_check_source
    successful = run_synthetic_cli(source)
    assert successful.returncode == 0, successful.stdout + successful.stderr
    index = source / ".scratch/check-runs/timings/backend.json"
    original = index.read_bytes()
    configuration = source / "pyproject.toml"
    configuration.write_text(configuration.read_text(encoding="utf-8") + 'addopts = "-k first"\n', encoding="utf-8")
    filtered = run_synthetic_cli(source)
    assert filtered.returncode == 0, filtered.stdout + filtered.stderr
    assert index.read_bytes() == original
    configuration.write_text(configuration.read_text(encoding="utf-8").replace('addopts = "-k first"\n', ""), encoding="utf-8")
    (source / "tests/test_failure.py").write_text('def test_failure():\n    assert False, "original failure"\n', encoding="utf-8")
    failed = run_synthetic_cli(source)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert index.read_bytes() == original


def test_cli_prepares_shared_desktop_dependency_link_before_different_files_start_building(
    duration_check_source: Path,
) -> None:
    source = duration_check_source
    shared = source.parent / "shared-project"
    registration = shared / ".git/worktrees/source"
    registration.mkdir(parents=True)
    relocated = shared / ".scratch/worktrees/source"
    relocated.parent.mkdir(parents=True)
    source.rename(relocated)
    source = relocated
    (registration / "commondir").write_text("../..", encoding="utf-8")
    (source / ".git").rmdir()
    (source / ".git").write_text("gitdir: " + str(registration), encoding="utf-8")
    modules = shared / "desktop/node_modules"
    modules.mkdir(parents=True)
    (modules / "shared-marker.txt").write_text("shared dependency", encoding="utf-8")
    (source / "desktop").mkdir()
    (source / "desktop/package.json").write_text("{}", encoding="utf-8")
    for name in ("a", "b", "c", "d"):
        (source / f"tests/test_{name}.py").write_text(dedent("""\
            from pathlib import Path

            def test_first_build_uses_prepared_dependency():
                modules = Path(__file__).resolve().parents[1] / "desktop/node_modules"
                assert modules.is_junction()
                assert (modules / "shared-marker.txt").read_text(encoding="utf-8") == "shared dependency"
            """), encoding="utf-8")
    process = run_synthetic_cli(source)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result["dependency_preparation"]["status"] == "linked"
    assert result["completeness"]["complete"] is True
    assert result["project_root"] == str(shared)


@pytest.mark.parametrize("index_kind", ["missing", "broken", "oversized", "environment", "unwritable"])
def test_duration_cli_falls_back_from_unusable_timings_without_changing_suite_coverage(
    duration_check_source: Path, index_kind: str,
) -> None:
    source = duration_check_source
    (source / ".scratch/reference.json").unlink()
    index = source / ".scratch/check-runs/timings/backend.json"
    index.parent.mkdir(parents=True)
    if index_kind == "broken":
        index.write_text("{broken", encoding="utf-8")
    elif index_kind == "oversized":
        index.write_bytes(b" " * (2 * 1024 ** 2 + 1))
    elif index_kind == "environment":
        index.write_text(json.dumps({"version": 1, "environment": {}, "node_seconds": {"tests/test_a.py::test_first": 999}}), encoding="utf-8")
    elif index_kind == "unwritable":
        index.mkdir()
    process = run_synthetic_cli(source)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result["completeness"]["complete"] is True
    assert result["completeness"]["expected_count"] == 8
    assert result["runtime_removed"] is True
    assert result["timing_index"]["read_status"] == "fallback"
    assert result["timing_reference"]["status"] == "fallback"
    assert result["timing_estimates"]["unknown_nodes"] == 8
    assert result["timing_index"]["update_status"] == ("failed" if index_kind == "unwritable" else "updated")
    assert not list(index.parent.glob("*.tmp"))


def test_cli_rejects_unknown_scheduling_modes_before_collection(duration_check_source: Path) -> None:
    process = run_synthetic_cli(duration_check_source, scheduling="unsupported")
    assert process.returncode != 0
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert "MYRK_TEST_SCHEDULING must be legacy or duration" in result["error"]
    assert result["collection_receipt"] is None


def test_duration_cli_bounds_learned_timings_without_dropping_executed_nodes(duration_check_source: Path) -> None:
    source = duration_check_source
    (source / "tests/test_many.py").write_text(dedent("""\
        import pytest

        @pytest.mark.parametrize("case", range(1400), ids=lambda case: "長" * 550 + str(case))
        def test_many(case):
            assert case >= 0
        """), encoding="utf-8")
    process = run_synthetic_cli(source)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result["completeness"]["complete"] is True
    assert result["completeness"]["expected_count"] == 1408
    assert result["timing_index"]["update_status"] == "updated"
    assert result["timing_index"]["evicted_nodes"] > 0
    index = source / ".scratch/check-runs/timings/backend.json"
    assert index.stat().st_size <= 2 * 1024 ** 2
    assert len(json.loads(index.read_text(encoding="utf-8"))["node_seconds"]) < 1408
    reused = run_synthetic_cli(source, "tests/test_a.py")
    assert reused.returncode == 0, reused.stdout + reused.stderr
    assert json.loads(reused.stdout.strip().splitlines()[-1])["timing_index"]["read_status"] == "loaded"


def test_cli_explicit_partial_selection_does_not_update_the_timing_index(duration_check_source: Path) -> None:
    source = duration_check_source
    assert run_synthetic_cli(source).returncode == 0
    index = source / ".scratch/check-runs/timings/backend.json"
    original = index.read_bytes()
    partial = run_synthetic_cli(source, "tests/test_a.py")
    assert partial.returncode == 0, partial.stdout + partial.stderr
    result = json.loads(partial.stdout.strip().splitlines()[-1])
    assert result["execution_mode"] == "serial"
    assert result["completeness"]["expected_count"] == 2
    assert result["timing_index"]["update_status"] == "not_eligible"
    assert index.read_bytes() == original


@pytest.mark.parametrize("change", ["source", "typescript_declaration", "collection"])
def test_duration_cli_rejects_source_or_worker_collection_drift_and_preserves_learned_timings(
    duration_check_source: Path, change: str,
) -> None:
    source = duration_check_source
    assert run_synthetic_cli(source).returncode == 0
    index = source / ".scratch/check-runs/timings/backend.json"
    original = index.read_bytes()
    if change != "collection":
        changed = "myrk/changed.py" if change == "source" else "desktop/tests/changed.d.mts"
        (source / "tests/test_mutation.py").write_text(dedent(f"""\
            from pathlib import Path

            def test_source_mutation():
                target = Path(__file__).resolve().parents[1] / {changed!r}
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("changed = True", encoding="utf-8")
            """), encoding="utf-8")
    else:
        (source / "tests/conftest.py").write_text(dedent("""\
            def pytest_collection_modifyitems(config, items):
                if config.getoption("myrk_shard") == "lane1":
                    items.pop()
            """), encoding="utf-8")
    process = run_synthetic_cli(source)
    assert process.returncode != 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result["source_inputs_unchanged"] is False if change != "collection" else result["completeness"]["complete"] is False
    assert result["runtime_removed"] is True, process.stdout + process.stderr
    assert index.read_bytes() == original


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


@pytest.mark.parametrize("scheduling", ["legacy", "duration"])
def test_parallel_cli_ctrl_c_stops_both_interpreters_and_ignoring_descendants(
    synthetic_check_source: Path, scheduling: str,
) -> None:
    source = synthetic_check_source
    if scheduling == "duration":
        formal = source / "tests/acceptance/test_formal_scenarios.py"
        formal.write_text(formal.read_text(encoding="utf-8").partition("def test_regular_check_in_the_formal_file")[0], encoding="utf-8")
        (source / "tests/test_regular.py").write_text(
            'def test_regular_check(lane_barrier):\n    lane_barrier("other")\n', encoding="utf-8")
    timing_index = source / ".scratch/check-runs/timings/backend.json"
    timing_index.parent.mkdir(parents=True)
    timing_index.write_text('{"before_cancel": true}', encoding="utf-8")
    reference = source / ".scratch/reference.json"
    if scheduling == "duration":
        prefix = "tests/acceptance/test_formal_scenarios.py::test_selected_formal_scenario_traverses_real_main_service_and_sdk"
        reference.write_text(json.dumps({"node_seconds": {
            prefix + "[alpha]": 3, prefix + "[beta]": 1, "tests/test_regular.py::test_regular_check": 2,
        }}), encoding="utf-8")
    probe = source / ".scratch/probe"
    probe.mkdir(parents=True)
    (source / "process_probe.py").write_text(
        dedent("""\
            import ctypes
            from ctypes import wintypes
            import json
            import os
            from pathlib import Path
            import signal
            import subprocess
            import sys
            from time import monotonic, sleep


            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
            kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]


            def publish(destination, **facts):
                times = [wintypes.FILETIME() for _ in range(4)]
                assert kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(value) for value in times))
                pending = destination.with_suffix(".pending")
                pending.write_text(json.dumps({
                    "pid": os.getpid(),
                    "created_filetime": str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime),
                    **facts,
                }), encoding="utf-8")
                pending.replace(destination)


            if __name__ == "__main__" and sys.argv[1] == "descendant":
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGBREAK, signal.SIG_IGN)
                assert kernel.SetConsoleCtrlHandler(None, True)
                publish(Path(sys.argv[2]), ignores_ctrl_c=True)
                sleep(120)

            if __name__ == "__main__" and sys.argv[1] == "console":
                source = Path(__file__).resolve().parent
                probe = source / ".scratch/probe"
                deadline = monotonic() + 30
                while not (probe / "start.request").exists():
                    assert monotonic() < deadline, "The parent never assigned the console helper to its cleanup job"
                    sleep(.01)
                assert kernel.SetConsoleCtrlHandler(None, True)
                # Reset the inherited ignore property before entering the public CLI.
                adapter = (
                    "import ctypes,runpy,sys;"
                    "kernel=ctypes.WinDLL('kernel32',use_last_error=True);"
                    "kernel.SetConsoleCtrlHandler.argtypes=[ctypes.c_void_p,ctypes.c_int];"
                    "assert kernel.SetConsoleCtrlHandler(None,False);"
                    "sys.argv=sys.argv[1:];"
                    "runpy.run_path(sys.argv[0],run_name='__main__')"
                )
                with (probe / "cli.stdout.txt").open("w", encoding="utf-8") as stdout, (probe / "cli.stderr.txt").open("w", encoding="utf-8") as stderr:
                    coordinator = subprocess.Popen(
                        [sys.argv[2], "-B", "-c", adapter, "scripts/run_checks.py", "pytest-parallel", "-q"],
                        cwd=source, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    )
                    try:
                        deadline = monotonic() + 30
                        while not (probe / "signal.request").exists():
                            assert coordinator.poll() is None, "The CLI exited before the parent requested Ctrl+C"
                            assert monotonic() < deadline, "The parent never requested Ctrl+C"
                            sleep(.01)
                        assert kernel.GenerateConsoleCtrlEvent(0, 0)
                        publish(probe / "signal.sent.json", event=0, group=0)
                        returncode = coordinator.wait(timeout=30)
                        (probe / "console.result.json").write_text(json.dumps({"returncode": returncode}), encoding="utf-8")
                    finally:
                        if coordinator.poll() is None:
                            coordinator.kill()
                        coordinator.wait(timeout=5)
            """),
        encoding="utf-8",
    )
    (source / "tests/conftest.py").write_text(
        dedent("""\
            import os
            from pathlib import Path
            import subprocess
            import sys
            from time import monotonic, sleep

            import pytest

            from process_probe import publish


            @pytest.fixture
            def lane_barrier():
                def hold_with_descendant(lane):
                    probe = Path(os.environ["MYRK_PARALLEL_PROBE"])
                    helper = Path(__file__).resolve().parents[1] / "process_probe.py"
                    ready = probe / (lane + ".descendant.json")
                    descendant = subprocess.Popen([sys._base_executable, "-B", str(helper), "descendant", str(ready)])
                    deadline = monotonic() + 20
                    while not ready.exists():
                        assert descendant.poll() is None, "The descendant exited before ignoring Ctrl+C"
                        assert monotonic() < deadline, "The descendant never became ready"
                        sleep(.01)
                    publish(probe / (lane + ".interpreter.json"),
                        runtime_directory=os.environ["MYRK_CHECK_RUN_DIRECTORY"], descendant_pid=descendant.pid)
                    sleep(120)
                return hold_with_descendant
            """),
        encoding="utf-8",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]

    class JobAccounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
            (name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")
        ]

    handles: list[int] = []
    controller = None
    unrelated = None
    cleanup_job = kernel.CreateJobObjectW(None, None)
    assert cleanup_job

    def hold_identity(identity: dict[str, object]) -> int:
        handle = kernel.OpenProcess(0x101001, False, identity["pid"])
        assert handle, f"Cannot hold the observed process: {identity}"
        handles.append(int(handle))
        times = [wintypes.FILETIME() for _ in range(4)]
        assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
        assert str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) == identity["created_filetime"]
        assert kernel.WaitForSingleObject(handle, 0) == 258
        return int(handle)

    try:
        unrelated = subprocess.Popen(
            [sys._base_executable, "-B", "-c", "from time import sleep; sleep(120)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        controller = subprocess.Popen(
            [sys._base_executable, "-B", str(source / "process_probe.py"), "console", sys.executable],
            cwd=source,
            env=dict(os.environ, PYTHONPATH=str(source), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", MYRK_PARALLEL_PROBE=str(probe),
                MYRK_TEST_SCHEDULING=scheduling, MYRK_TEST_TIMING_REFERENCE=str(reference)),
            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        assert kernel.AssignProcessToJobObject(cleanup_job, int(getattr(controller, "_handle")))
        (probe / "start.request").touch()
        ready_paths = [probe / f"{lane}.{kind}.json" for lane in ("formal", "other") for kind in ("interpreter", "descendant")]
        deadline = monotonic() + 30
        while not all(path.is_file() for path in ready_paths) and controller.poll() is None and monotonic() < deadline:
            sleep(.01)
        assert all(path.is_file() for path in ready_paths), {
            "error": "Both groups and their signal-ignoring descendants must be ready before Ctrl+C",
            "ready_files": sorted(path.name for path in probe.iterdir()),
            "plans": [json.loads(path.read_text(encoding="utf-8"))["lanes"]
                for path in (source / ".scratch/check-runs/results").glob("*.reports/pytest-shards/collection.json")],
        }
        observed = {}
        for lane in ("formal", "other"):
            interpreter = json.loads((probe / f"{lane}.interpreter.json").read_text(encoding="utf-8"))
            descendant = json.loads((probe / f"{lane}.descendant.json").read_text(encoding="utf-8"))
            assert descendant["ignores_ctrl_c"] is True
            assert interpreter["descendant_pid"] == descendant["pid"]
            observed[lane] = interpreter
            hold_identity(interpreter)
            hold_identity(descendant)
        assert len(set(identity["pid"] for identity in observed.values())) == 2
        assert kernel.WaitForSingleObject(int(getattr(unrelated, "_handle")), 0) == 258
        (probe / "signal.request").touch()
        helper_stdout, helper_stderr = controller.communicate(timeout=45)

        assert controller.returncode == 0, helper_stdout + helper_stderr
        sent = json.loads((probe / "signal.sent.json").read_text(encoding="utf-8"))
        assert sent["event"] == 0 and sent["group"] == 0
        assert json.loads((probe / "console.result.json").read_text(encoding="utf-8"))["returncode"] == 130
        stdout = (probe / "cli.stdout.txt").read_text(encoding="utf-8")
        stderr = (probe / "cli.stderr.txt").read_text(encoding="utf-8")
        aggregate = json.loads(stdout.strip().splitlines()[-1])
        assert aggregate["exit_code"] == 130, stdout + stderr
        assert aggregate["runtime_removed"] is True
        assert aggregate["collection_receipt"]["runtime_removed"] is True
        for lane in ("formal", "other"):
            lane_name = lane if scheduling == "legacy" else "lane1" if lane == "formal" else "lane2"
            receipt = aggregate["lanes"][lane_name]["receipt"]
            assert receipt["runtime_removed"] is True
            assert not Path(receipt["runtime_directory"]).exists()
            assert receipt["runtime_directory"] == observed[lane]["runtime_directory"]
            for command in receipt["commands"]:
                assert command["interpreter"]["pid"] == observed[lane]["pid"]
                assert command["interpreter"]["created_filetime"] == observed[lane]["created_filetime"]
                assert command["interpreter_exit_confirmed"] is True
                assert command["job_active_processes"] == 0
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
        assert not list((source / ".scratch/check-runs/runtime-tmp").iterdir())
        assert timing_index.read_text(encoding="utf-8") == '{"before_cancel": true}'
        assert unrelated.poll() is None
        assert kernel.WaitForSingleObject(int(getattr(unrelated, "_handle")), 0) == 258
    finally:
        assert kernel.TerminateJobObject(cleanup_job, 1)
        accounting = JobAccounting()
        deadline = monotonic() + 5
        while True:
            assert kernel.QueryInformationJobObject(cleanup_job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None)
            if accounting.active == 0 or monotonic() >= deadline:
                break
            sleep(.01)
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        for process in (controller, unrelated):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
        kernel.CloseHandle(cleanup_job)
        assert accounting.active == 0, "The test console still has owned descendant processes"
