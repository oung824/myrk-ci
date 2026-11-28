"""Parallel backend scheduling preserves collected tests and timing evidence."""

import json
import os
from pathlib import Path
import subprocess
import sys
from textwrap import dedent

import pytest

from tests.test_parallel_checks import synthetic_check_source


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
