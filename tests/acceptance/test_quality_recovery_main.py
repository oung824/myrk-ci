"""Task-specific long-context materials reach the real native Main execution."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


@pytest.mark.acceptance_desktop
def test_qg5_supplement_main_uses_actual_user_event_and_one_exact_edit(prepared_root, acceptance_public_root):
    import os
    import subprocess
    import sys
    from zipfile import ZipFile, ZIP_DEFLATED
    from tests.support.agent_acceptance import (AcceptancePlan, FrozenParentBudget, LiveAuthorization, LiveGate,
        builtin_registry, prepare_live, quality_plan, run_plan)
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-control-" + uuid4().hex)
    parent = FrozenParentBudget(parent_id="synthetic-qg5-" + prepared_root.name, accounting_root=prepared_root,
        request_cap=24, phase_microusd={"screening": 0, "quality": 353904, "judge": 0}, reserve_microusd=0, profile="synthetic")
    index = Path("E:/Myrk/.scratch/acceptance-parent-budgets") / (sha256(parent.parent_id.encode()).hexdigest() + ".json")
    bundle = None
    try:
        reference = build_implementation_quality_packet(packet)
        registry = builtin_registry(quality_packet=reference)
        proposal_parent = parent.model_copy(update={"request_cap": 2520,
            "phase_microusd": {"screening": 0, "quality": 39076920, "judge": 0}})
        proposal = quality_plan(SOURCE / reference.path, registry=registry, parent_budget=proposal_parent, mode="offline")
        identity = "quality:deepseek:QG5.formal.1:1"
        execution = next(row for row in proposal.executions if row.execution_id == identity).model_copy(update={"order": 1})
        plan = AcceptancePlan(executions=(execution,), parent_budget=parent, quality_packet=reference, mode="offline")
        bundle = prepare_live(plan.model_copy(update={"mode": "prepare-live"}), root=prepared_root, registry=registry, evaluator_isolation_directories=())
        grant = LiveAuthorization(authorization_id="synthetic-qg5-explicit", binding=bundle.binding, total_limit=24,
            case_limits={row.execution_id: row.request_cap for row in plan.executions}, total_microusd=353904,
            case_microusd={row.execution_id: row.cost_cap_microusd for row in plan.executions},
            parent_budget_id=parent.parent_id, transport="mock")
        acceptance_public_root.mkdir(parents=True, exist_ok=True)
        request = acceptance_public_root / "synthetic-execution-request.json"
        request.write_text(json.dumps({"run_root": str(prepared_root), "grant": grant.model_dump(mode="json")}), encoding="utf-8")
        output = acceptance_public_root / "execution"
        process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "execute",
            str(request), "--output", str(output)], cwd=prepared_root,
            env={**os.environ, "PYTHONPATH": str(prepared_root), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        (acceptance_public_root / "cli.stdout").write_bytes(process.stdout)
        (acceptance_public_root / "cli.stderr").write_bytes(process.stderr)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        result = json.loads((output / "result.json").read_bytes())
        outcome = next(row for row in result["cases"] if row["execution_id"] == identity)
        assert outcome["tool_status"] == "passed", outcome["facts"]
        contract = outcome["facts"]["quality_workflow_contract"]
        assert contract["task_id"] == "QG5.formal.1"
        assert contract["supplement_applied_once"] is True
        assert contract["first_applied_request"] == 2
        assert contract["no_future_update_in_first_input"] is True
        assert contract["source_card_unchanged"] is True
        assert contract["edit_count"] == 1
        assert outcome["semantic_status"] == "not_measured"
        assert outcome["facts"]["budget"]["actual_http"] <= 24
        assert result["planned_executions"] == [row.model_dump(mode="json") for row in plan.executions]
        assert len(result["cases"]) == 1
    finally:
        if bundle is not None:
            acceptance_public_root.mkdir(parents=True, exist_ok=True)
            with ZipFile(acceptance_public_root / "executed-source-snapshot.zip", "x", ZIP_DEFLATED) as archive:
                for name, expected in bundle.files.items():
                    content = (prepared_root / name).read_bytes()
                    assert sha256(content).hexdigest() == expected
                    archive.writestr(name, content)
        originals = [packet / "freeze.json", packet / "input/QG5.formal.1.json", packet / "gold/QG5.formal.1.json"]
        if any(path.is_file() for path in originals):
            destination = acceptance_public_root / "review-context"
            destination.mkdir(parents=True, exist_ok=False)
            bindings = {}
            for path in originals:
                if path.is_file():
                    target = destination / path.relative_to(packet)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
                    bindings[path.relative_to(packet).as_posix()] = {"source": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
                        "purpose": "review_context", "product_output": False}
            (destination / "originals-index.json").write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
        if index.exists():
            assert index.parent == Path("E:/Myrk/.scratch/acceptance-parent-budgets")
            index.unlink()
        if packet.exists(): shutil.rmtree(packet)


def _run_main_control(task_id, prepared_root, acceptance_public_root, *, build_packet=None):
    import os
    import subprocess
    import sys
    from zipfile import ZipFile, ZIP_DEFLATED
    from tests.support.agent_acceptance import (AcceptancePlan, FrozenParentBudget, LiveAuthorization, LiveGate,
        builtin_registry, prepare_live, quality_plan, run_plan)
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-control-" + uuid4().hex)
    parent = FrozenParentBudget(parent_id="synthetic-qg5-" + prepared_root.name, accounting_root=prepared_root,
        request_cap=24, phase_microusd={"screening": 0, "quality": 353904, "judge": 0}, reserve_microusd=0, profile="synthetic")
    index = Path("E:/Myrk/.scratch/acceptance-parent-budgets") / (sha256(parent.parent_id.encode()).hexdigest() + ".json")
    bundle = None
    try:
        reference = (build_packet or build_implementation_quality_packet)(packet)
        registry = builtin_registry(quality_packet=reference)
        proposal_parent = parent.model_copy(update={"request_cap": 2520,
            "phase_microusd": {"screening": 0, "quality": 39076920, "judge": 0}})
        proposal = quality_plan(SOURCE / reference.path, registry=registry, parent_budget=proposal_parent, mode="offline")
        identity = "quality:deepseek:" + task_id + ":1"
        execution = next(row for row in proposal.executions if row.execution_id == identity).model_copy(update={"order": 1})
        plan = AcceptancePlan(executions=(execution,), parent_budget=parent, quality_packet=reference, mode="offline")
        bundle = prepare_live(plan.model_copy(update={"mode": "prepare-live"}), root=prepared_root, registry=registry, evaluator_isolation_directories=())
        grant = LiveAuthorization(authorization_id="synthetic-qg5-explicit", binding=bundle.binding, total_limit=24,
            case_limits={row.execution_id: row.request_cap for row in plan.executions}, total_microusd=353904,
            case_microusd={row.execution_id: row.cost_cap_microusd for row in plan.executions},
            parent_budget_id=parent.parent_id, transport="mock")
        acceptance_public_root.mkdir(parents=True, exist_ok=True)
        request = acceptance_public_root / "synthetic-execution-request.json"
        request.write_text(json.dumps({"run_root": str(prepared_root), "grant": grant.model_dump(mode="json")}), encoding="utf-8")
        output = acceptance_public_root / "execution"
        process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "execute",
            str(request), "--output", str(output)], cwd=prepared_root,
            env={**os.environ, "PYTHONPATH": str(prepared_root), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        (acceptance_public_root / "cli.stdout").write_bytes(process.stdout)
        (acceptance_public_root / "cli.stderr").write_bytes(process.stderr)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        result = json.loads((output / "result.json").read_bytes())
        outcome = next(row for row in result["cases"] if row["execution_id"] == identity)
        assert outcome["tool_status"] == "passed", outcome["facts"]
        return outcome
    finally:
        if bundle is not None:
            acceptance_public_root.mkdir(parents=True, exist_ok=True)
            with ZipFile(acceptance_public_root / "executed-source-snapshot.zip", "x", ZIP_DEFLATED) as archive:
                for name, expected in bundle.files.items():
                    content = (prepared_root / name).read_bytes()
                    assert sha256(content).hexdigest() == expected
                    archive.writestr(name, content)
        originals = [packet / "freeze.json", packet / ("input/" + task_id + ".json"), packet / ("gold/" + task_id + ".json")]
        if any(path.is_file() for path in originals):
            destination = acceptance_public_root / "review-context"
            destination.mkdir(parents=True, exist_ok=False)
            bindings = {}
            for path in originals:
                if path.is_file():
                    target = destination / path.relative_to(packet)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
                    bindings[path.relative_to(packet).as_posix()] = {"source": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
                        "purpose": "review_context", "product_output": False}
            (destination / "originals-index.json").write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
        if index.exists():
            assert index.parent == Path("E:/Myrk/.scratch/acceptance-parent-budgets")
            index.unlink()
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.acceptance_desktop
def test_qg5_main_adopts_late_supplement_after_the_original_read(prepared_root, acceptance_public_root, monkeypatch):
    from tests.acceptance.test_quality_recovery_instances import build_late_supplement_packet

    outcome = _run_main_control("QG5.formal.7", prepared_root, acceptance_public_root,
        build_packet=lambda packet: build_late_supplement_packet(packet, monkeypatch))
    assert outcome["product_status"] == "passed"
    contract = outcome["facts"]["quality_workflow_contract"]
    assert contract["late_arrival_verified"] is True
    assert contract["first_applied_request"] == 3
    assert contract["read_only_attachment_read"] is True and contract["source_card_unchanged"] is True
    assert contract["edit_bound_to_original"] is True and contract["edit_count"] == 1
    assert outcome["facts"]["real_http_attempts"] == 0


@pytest.mark.acceptance_desktop
def test_qg5_main_advances_the_independent_queue_after_a_verified_service_failure(prepared_root, acceptance_public_root):
    outcome = _run_main_control("QG5.formal.10", prepared_root, acceptance_public_root)
    contract = outcome["facts"]["quality_workflow_contract"]
    assert outcome["product_status"] == "failed"
    assert contract["raw_failure_not_relabelled_stop"] is True
    assert contract["unknown_effects_verified_before_next"] is True
    assert contract["independent_queue_identity_preserved"] is True
    assert contract["original_not_retried"] is True
    assert contract["next_completed"] is True and contract["next_source_read"] is True
    assert outcome["facts"]["real_http_attempts"] == 0
    assert outcome["semantic_status"] == "not_measured"


@pytest.mark.acceptance_desktop
@pytest.mark.parametrize("task_id", ["QG5.formal.4", "QG5.formal.6"])
def test_qg5_main_parallel_stop_and_local_save_recovery(task_id, prepared_root, acceptance_public_root):
    outcome = _run_main_control(task_id, prepared_root, acceptance_public_root)
    if task_id == "QG5.formal.4" and outcome["facts"].get("known_default_permission_loss"):
        assert outcome["product_status"] == "failed"
        assert outcome["critical_contracts"]["K1"] == "failed"
        budget = outcome["facts"]["budget"]
        assert budget["stopped"] is True
        assert "Quality actual product Task permissions changed" in budget["stop_reason"]
        unknown = [row for row in budget["attempts"] if row["state"] == "unknown"]
        assert len(unknown) == 1 and unknown[0]["occupied_microusd"] > 0
        assert len(budget["attempts"]) == 3
        assert outcome["user_acceptance"] == "pending"
    elif task_id == "QG5.formal.4":
        contract = outcome["facts"]["quality_workflow_contract"]
        assert contract["codes"] == ["C-572", "P-918"]
        assert contract["distinct_thread_task_ids"] is True
        assert contract["partial_effect_preserved"] is True
        assert contract["b_continues_after_a_stop"] is True
        assert contract["separately_stopped"] is True
        assert contract["exact_actual_receiver_bodies"] is True
        assert contract["actual_approval_subjects_match"] is True
        assert contract["no_request_after_each_stop"] is True
    else:
        contract = outcome["facts"]["quality_recovery_contract"]
        assert contract["planned_local_exit"] == 71
        assert contract["reliable_and_pending_differ"] is True
        assert contract["imported_unknown_blocks_resume"] is True
        assert contract["verified_effect"] == "completed"
        assert contract["completed_effects_not_replayed"] is True
        assert contract["same_task_and_scope"] is True
        assert contract["open_http_attempts"] == contract["blocked_resume_http_attempts"] == 0
    assert outcome["semantic_status"] == "not_measured"
    assert outcome["facts"]["budget"]["actual_http"] <= 24
