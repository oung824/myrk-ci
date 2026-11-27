"""The unified package maps old scope without manufacturing new evidence."""

import pytest

from tests.acceptance.test_historical_reuse_regrading import historical_group
from tests.support.agent_acceptance import builtin_registry


def test_package_maps_original_126_cases_and_277_pairs_into_current_scope_once():
    from tests.support.agent_acceptance.integrated_package import integrated_execution_package

    package = integrated_execution_package(registry=builtin_registry())
    coverage = package["coverage"]
    assert coverage["legacy_case_count"] == 126
    assert coverage["legacy_combination_count"] == 277
    mappings = coverage["legacy_mapping"]
    assert len(mappings) == len({(row["case_id"], row["protocol"]) for row in mappings}) == 277
    assert all(row["case_id"] == row["current_case_id"] and row["protocol"] == row["current_protocol"] for row in mappings)
    current = coverage["combinations"]
    assert len(current) == len({(row["case_id"], row["protocol"]) for row in current})
    assert coverage["current_combination_count"] == len(current)
    assert set(coverage["groups"]) == {f"C{number:02}" for number in range(1, 18)}
    assert coverage["crosscutting_groups"] == ["C11", "C12", "C14"]
    assert all(row["independent_sample_increment"] == 0 for row in coverage["crosscutting"])
    assert package["sending_authorized"] is False
    assert package["product_admission"] == "not_established"


def test_package_freezes_212_product_executions_and_one_7256_request_180_usd_envelope():
    from tests.support.agent_acceptance.integrated_package import integrated_execution_package

    package = integrated_execution_package(registry=builtin_registry())
    plan = package["proposal"]
    assert plan["screening"] == {"deepseek": 96, "google": 32, "total": 128}
    assert plan["quality"] == {"formal_tasks": 60, "tuning_tasks": 12, "google_compatibility_executions": 12,
        "independent_tasks": 72, "product_executions": 84, "maximum_predeclared_shared": 12}
    assert plan["product_execution_cap_without_sharing"] == 212
    assert plan["requests"] == {"deepseek_product": 5314, "google_product": 1462, "google_judge": 480, "parent_cap": 7256}
    assert plan["money"] == {"currency": "USD", "proposed_total_microusd": 180000000,
        "phase_microusd": {"screening": 70000000, "quality": 40000000, "judge": 60000000}, "reserve_microusd": 10000000}
    assert len(plan["executions"]) == 692
    assert len({row["execution_id"] for row in plan["executions"]}) == 692
    assert all(row["sending_authorized"] is False for row in package["routes"])
    assert {row["role"] for row in package["routes"]} == {"product", "judge"}
    assert all(row["status"] == "unqualified" and row["unresolved"] for row in package["routes"])
    assert package["human_path"]["required_tickets"] == ["20", "21"]
    assert package["human_path"]["optional_judge_tickets"] == ["22", "23"]
    assert package["human_path"]["actual_human_reviews"] == 0


def test_package_preserves_known_failures_and_all_27_local_obligations_without_claiming_admission():
    from tests.support.agent_acceptance.integrated_package import integrated_execution_package

    package = integrated_execution_package(registry=builtin_registry())
    assert set(package["local_obligations"]) == {"B01", "B02", "B03", "B04", *[f"L{number:02}" for number in range(1, 24)]}
    assert all(row["case_ids"] and row["evidence"] and row["current_measurement"] == "not_measured"
        for row in package["local_obligations"].values())
    failures = {row["id"]: row for row in package["known_product_failures"]}
    assert set(failures) == {"R14-source-change", "D16-off", "C09-startup-failure-quit", "new-chat-permission-reset", "QG6-candidate-save-failure"}
    assert failures["R14-source-change"]["critical_contracts"] == {"K1": "failed", "K2": "failed"}
    assert failures["D16-off"]["product_status"] == failures["C09-startup-failure-quit"]["product_status"] == "failed"
    assert all(row["evidence"] for row in failures.values())
    assert all(row["tool_status"] == "passed" for identity, row in failures.items() if identity != "QG6-candidate-save-failure")
    assert failures["QG6-candidate-save-failure"]["tool_status"] == "failed"
    assert package["critical_contracts"] == {"K1": "failed", "K2": "failed", "K3": "unknown", "K4": "unknown", "K5": "failed"}
    assert package["labels"]["all_passed"] is False
    assert package["labels"]["version_admission"] == "not_established"
    assert package["labels"]["user_acceptance"] == "pending"


def test_unified_cli_freezes_reviewable_sources_and_rejects_damaged_package(acceptance_public_root):
    import json
    import os
    import subprocess
    import sys

    destination = acceptance_public_root / "execution-package"
    command = [sys.executable, "-B", "-m", "tests.support.agent_acceptance"]
    frozen = subprocess.run([*command, "freeze-execution-package", str(destination)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True)
    assert frozen.returncode == 0, frozen.stderr.decode("utf-8", errors="replace")
    receipt = json.loads(frozen.stdout)
    package = json.loads((destination / "package.json").read_bytes())
    assert receipt["sending_authorized"] is False
    assert package["source_files"]["tests/support/agent_acceptance/integrated_package.py"]
    assert (destination / "sources.zip").is_file()
    checked = subprocess.run([*command, "verify-execution-package", str(destination)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True)
    assert checked.returncode == 0, checked.stderr.decode("utf-8", errors="replace")
    damaged = package.copy()
    damaged["package"]["labels"]["all_passed"] = True
    (destination / "package.json").write_text(json.dumps(damaged), encoding="utf-8")
    rejected = subprocess.run([*command, "verify-execution-package", str(destination)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True)
    assert rejected.returncode != 0
    assert b"binding" in rejected.stderr.lower()


@pytest.mark.acceptance_desktop
def test_r19_keeps_three_normal_summaries_and_used_capacity_recovery_across_full_main_reopens(acceptance_public_root):
    import asyncio
    import json
    from tests.support.agent_acceptance import AcceptancePlan, run_plan

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["integration.r19"]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    assert outcome.facts["normal_compactions"] >= 3
    assert outcome.facts["capacity_recovery_decisions"] == 1
    assert outcome.facts["capacity_recovery_preserved_across_reopens"] is True
    assert outcome.facts["normal_exit_phases"] == 3
    assert outcome.facts["same_task_scope_approval"] is True
    assert outcome.facts["original_messages_preserved"] is True
    assert outcome.facts["inherited_coverage"] is True
    assert outcome.facts["open_http_attempts"] == 0
    assert outcome.facts["program_runs"] == "x"
    assert outcome.facts["independent_receipt_verified"] is True
    assert outcome.facts["requests_after"] > outcome.facts["requests_before"]
    assert outcome.facts["real_http_attempts"] == 0
    assert outcome.semantic_status == "not_measured"
    record = json.loads(outcome.evidence["conversation"].read_bytes())
    assert record["turns"][-2]["status"] == "failed"
    assert record["turns"][-1]["status"] == "completed"
    assert record["turns"][-2]["actions"] == record["turns"][-1]["actions"] == []
    assert all(row["status"] != "preparing" for turn in record["turns"] for row in turn["compactions"])


def test_package_rebuilds_72_actual_quality_tasks_without_counting_google_twice(acceptance_public_root):
    from uuid import uuid4
    import json
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.integrated_package import integrated_execution_package

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("package-control-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        report = integrated_execution_package(registry=builtin_registry(quality_packet=reference), quality_packet=reference)
        quality = report["quality_packet"]
        assert len(quality["tasks"]) == 72
        assert len({row["task_id"] for row in quality["tasks"]}) == 72
        assert len({row["material_family_id"] for row in quality["tasks"]}) == 72
        assert sum(row["purpose"] == "formal" for row in quality["tasks"]) == 60
        assert sum(row["purpose"] == "tuning" for row in quality["tasks"]) == 12
        assert quality["google_compatibility_tasks"] == [f"QG{group}.formal.{slot}" for group in range(1, 7) for slot in (3, 9)]
        assert quality["independent_task_count"] == 72
        assert quality["product_execution_count"] == 84
        assert all(row["prior_uses"] == ["implementation"] for row in quality["tasks"])
        assert all(row["goal"] and (row["sources"] or row["task_id"] == "QG1.formal.9" and row["expected_missing_sources"]) for row in quality["tasks"])
        assert quality["missing_domain_standards"] == [row["task_id"] for row in quality["tasks"] if not row["domain_standards"]]
        assert all(row["domain_standards"] or not row["formal_adapter_implemented"] for row in quality["tasks"])
        tasks = {row["task_id"]: row for row in quality["tasks"]}
        assert tasks["QG4.formal.9"]["source_id"] == "qg4-withdrawn-transfer"
        assert tasks["QG4.formal.10"]["source_id"] == "qg4-save-obstruction"
        assert tasks["QG6.formal.6"]["runtime_adapter"] == "qg6-6"
        assert quality["unseen_quality_ready"] is False
        assert quality["actual_human_review"] == "not_performed"
        assert report["labels"]["version_admission"] == "not_established"
        assert report["coverage"]["current_case_count"] == len(builtin_registry().cases)
        assert report["coverage"]["legacy_combination_count"] == 277
        acceptance_public_root.mkdir(parents=True, exist_ok=True)
        (acceptance_public_root / "package-observation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if (packet / "freeze.json").is_file():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(packet / "freeze.json", original / "freeze.original.json")
        if packet.exists(): shutil.rmtree(packet)


def test_verify_prepared_cli_rebuilds_its_canonical_quality_registry(prepared_root, acceptance_public_root):
    from uuid import uuid4
    import json
    import os
    import shutil
    import subprocess
    import sys
    from tests.support.agent_acceptance import AcceptancePlan, FrozenParentBudget, prepare_live, quality_plan
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("verify-control-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        registry = builtin_registry(quality_packet=reference)
        proposal_parent = FrozenParentBudget(parent_id="synthetic-verify-" + prepared_root.name, accounting_root=prepared_root,
            request_cap=2520, phase_microusd={"screening": 0, "quality": 39076920, "judge": 0}, reserve_microusd=0, profile="synthetic")
        proposal = quality_plan(packet / "freeze.json", registry=registry, parent_budget=proposal_parent, mode="offline")
        row = next(row for row in proposal.executions if row.scenario_id == "QG1.tuning.1").model_copy(update={"order": 1, "dependencies": ()})
        parent = proposal_parent.model_copy(update={"request_cap": row.request_cap,
            "phase_microusd": {"screening": 0, "quality": row.cost_cap_microusd, "judge": 0}})
        plan = AcceptancePlan(executions=(row,), parent_budget=parent, quality_packet=reference, mode="prepare-live")
        bundle = prepare_live(plan, root=prepared_root, registry=registry)
        observed = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance", "verify-prepared", str(prepared_root)],
            cwd=prepared_root, env={**os.environ, "PYTHONPATH": str(prepared_root), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        assert observed.returncode == 0, observed.stderr.decode("utf-8", errors="replace")
        assert json.loads(observed.stdout)["binding"] == bundle.binding
        assert json.loads((prepared_root / "ledger.json").read_bytes())["attempts"] == []
    finally:
        if (packet / "freeze.json").is_file():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG1.tuning.1.json", "gold/QG1.tuning.1.json"):
                target = original / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, target)
        if packet.exists(): shutil.rmtree(packet)


def test_quality_frozen_package_preserves_the_full_canonical_member_set(acceptance_public_root):
    from uuid import uuid4
    import json
    import shutil
    from zipfile import ZipFile
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.integrated_package import freeze_integrated_execution_package, verify_integrated_execution_package

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("frozen-package-control-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        registry = builtin_registry(quality_packet=reference)
        destination = acceptance_public_root / "frozen-package"
        path = freeze_integrated_execution_package(destination, registry=registry, quality_packet=reference)
        value = json.loads(path.read_bytes())
        frozen = json.loads((packet / "freeze.json").read_bytes())
        assert set(frozen["files"]) <= value["source_files"].keys()
        assert reference.path.as_posix() in value["source_files"]
        with ZipFile(destination / "sources.zip") as archive:
            assert set(frozen["files"]) <= set(archive.namelist())
            assert archive.read(reference.path.as_posix()) == (packet / "freeze.json").read_bytes()
        assert verify_integrated_execution_package(destination, registry=registry)["sending_authorized"] is False
        changed = packet / "gold/QG1.formal.1.json"
        original = changed.read_bytes()
        changed.write_bytes(original + b"\nchanged")
        with pytest.raises(ValueError, match="binding|changed"):
            verify_integrated_execution_package(destination, registry=registry)
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_combined_report_preserves_complete_current_and_historical_original_groups(historical_group, acceptance_public_root):
    from hashlib import sha256
    from tests.support.agent_acceptance import HistoricalChange, export_review, import_historical_group
    from tests.support.agent_acceptance.integrated_package import integrated_execution_package

    result, _ = historical_group
    original_sha = sha256(result.result_path.read_bytes()).hexdigest()
    review = export_review(result.result_path, acceptance_public_root / "review")
    historical = import_historical_group(result.result_path, destination=acceptance_public_root / "history",
        change=HistoricalChange(author="synthetic-integrated-package-controller", current={"source": "synthetic current source"},
            reason="Synthetic complete-group retention; no actual applicability or content review is claimed."))
    report = integrated_execution_package(registry=builtin_registry(), review_package=review, historical_groups=(historical,))
    originals = report["observations"]["original_planned_results"]
    assert [row["product_status"] for row in originals] == ["passed", "failed", "not_executed"]
    assert report["observations"]["current_review"]["planned_denominator"] == 3
    groups = report["observations"]["historical_groups"]
    assert len(groups) == 1 and groups[0]["original_denominator"] == 3
    assert [row["original"]["product_status"] for row in groups[0]["executions"]] == ["passed", "failed", "not_executed"]
    assert groups[0]["new_product_executions"] == 0
    assert groups[0]["layers"]["historical_reuse"] == "pending_review"
    assert report["observations"]["layers"]["current_real"] == "not_measured"
    assert report["human_path"]["actual_human_reviews"] == 0
    assert report["labels"]["all_passed"] is False
    assert report["labels"]["user_acceptance"] == "pending"
    assert sha256(result.result_path.read_bytes()).hexdigest() == original_sha

def test_combined_report_cli_keeps_current_and_historical_denominators(historical_group, acceptance_public_root):
    import json
    import os
    import subprocess
    import sys
    from tests.support.agent_acceptance import HistoricalChange, export_review, import_historical_group
    from tests.support.agent_acceptance.frozen import SOURCE
    result, _ = historical_group
    review = export_review(result.result_path, acceptance_public_root / "cli-review")
    historical = import_historical_group(result.result_path, destination=acceptance_public_root / "cli-history",
        change=HistoricalChange(author="synthetic-controller", current={"source": "synthetic current source"},
            reason="CLI implementation regression only; no human or applicability judgement is made."))
    process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance", "execution-package-report",
        "--review-package", str(review), "--historical-group", str(historical)], cwd=SOURCE,
        env={**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
    assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    observed = json.loads(process.stdout)
    assert [row["product_status"] for row in observed["observations"]["original_planned_results"]] == ["passed", "failed", "not_executed"]
    assert observed["observations"]["historical_groups"][0]["original_denominator"] == 3
    assert observed["human_path"]["actual_human_reviews"] == 0
    assert observed["sending_authorized"] is False
    assert observed["labels"]["version_admission"] == "not_established"
    (acceptance_public_root / "cli-combined-report.json").write_bytes(process.stdout)

def test_package_retains_actual_main_new_chat_default_permission_failure():
    from tests.support.agent_acceptance.integrated_package import integrated_execution_package
    package = integrated_execution_package(registry=builtin_registry())
    gap = next(row for row in package["known_product_failures"] if row["id"] == "new-chat-permission-reset")
    assert gap["product_status"] == "failed"
    assert gap["tool_status"] == "passed"
    assert gap["critical_contracts"] == {"K1": "failed"}
    assert gap["new_chat_terminal_http_attempts"] == 0
    assert gap["original_stop_and_reservations_preserved"] is True
    assert "Main:new-chat-after-temporary-configure" in gap["blocks"]
    assert gap["evidence"] and all(row["path"] and len(row["sha256"]) == 64 for row in gap["evidence"])
    assert package["labels"]["all_passed"] is False


def test_package_keeps_partial_candidate_save_failure_without_qualifying_the_task():
    from tests.support.agent_acceptance.integrated_package import integrated_execution_package
    package = integrated_execution_package(registry=builtin_registry())
    failure = next(row for row in package["known_product_failures"] if row["id"] == "QG6-candidate-save-failure")
    assert failure["product_status"] == "failed"
    assert failure["tool_status"] == "failed"
    assert failure["critical_contracts"] == {"K5": "failed"}
    assert failure["completed_primary_sources"] == 2
    assert failure["required_primary_sources"] == 16
    assert failure["cause"] == "unknown"
    assert failure["remaining_execution"] == "not_measured"
    assert failure["actual_model_semantics"] == "not_measured"
    assert failure["normal_summaries"] == 1
    assert failure["evidence"]
    assert package["labels"]["all_passed"] is False
