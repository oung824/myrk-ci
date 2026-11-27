"""Save, import and recovery through the existing public acceptance path."""

import pytest
from pathlib import Path

from tests.support.agent_acceptance import builtin_registry


def test_r16_r17_r18_have_frozen_public_screening_contracts() -> None:
    registry = builtin_registry()
    for name, risk, cap in [("formal.restart", "R16", 20), ("formal.recovery-unknown", "R17", 30), ("formal.recovery-family", "R18", 30)]:
        case = registry.cases[name]
        assert case.owner_ticket == "13"
        assert case.screening["deepseek"]["scenario_id"] == risk
        assert case.screening["deepseek"]["request_cap"] == cap
        assert case.frozen_material_input is True
    assert registry.cases["formal.restart"].screening["google"]["request_cap"] == 20


def test_local_save_boundary_keeps_original_and_pending_effects(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    case = asyncio.run(run_plan(AcceptancePlan(cases=["formal.recovery-unknown"]), root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert case.tool_status == "passed", case.facts
    contract = case.facts["recovery_contract"]
    assert contract["planned_local_save_exit"] is True
    assert contract["reliable_and_pending_preserved"] is True
    assert contract["actual_edit_before_unsaved_boundary"] is True
    assert contract["open_http_attempts"] == 0
    assert contract["unknown_verified_before_resume"] is True
    assert contract["duplicate_mutating_actions"] == []
    assert case.critical_contracts["K3"] == "passed"
    assert case.semantic_status == "not_measured"


def test_family_recovery_keeps_child_and_applied_supplement(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    case = asyncio.run(run_plan(AcceptancePlan(cases=["formal.recovery-family"]), root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert case.tool_status == "passed", case.facts
    contract = case.facts["recovery_contract"]
    assert contract["child_record_unchanged"] is True
    assert contract["child_scope_and_account_preserved"] is True
    assert contract["child_import_cannot_promote_to_main"] is True
    assert contract["applied_supplement_preserved_once"] is True
    assert contract["unfinished_parent_work_retained"] is True
    assert contract["open_http_attempts"] == 0
    assert contract["usage_retained"] is True
    assert case.product_status == "passed", case.facts
    assert case.critical_contracts["K3"] == "passed"


def test_legacy_stopped_and_save_failure_public_counterexamples(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    result = asyncio.run(run_plan(AcceptancePlan(cases=["recovery.legacy", "queue.saved-stop", "actions.record-write-failure"]), root=acceptance_public_root, registry=builtin_registry()))
    legacy, stopped, failed = result.cases
    assert legacy.tool_status == legacy.product_status == "passed", legacy.facts
    assert legacy.facts["source_unchanged"] is True
    assert legacy.facts["requests_before"] == 1 and legacy.facts["requests_after"] == 2
    assert legacy.facts["open_http_attempts"] == legacy.facts["exhausted_http_attempts"] == 0
    assert stopped.product_status == "passed", stopped.facts
    assert stopped.facts["original_status"] == "cancelled"
    assert stopped.facts["open_http_attempts"] == 0
    assert failed.tool_status == "passed", failed.facts
    assert failed.product_status == "failed"
    assert failed.facts["planned_record_obstruction"] is True
    assert failed.facts["save_failures"]


@pytest.mark.parametrize("risk,protocol", [("r16", "deepseek"), ("r17", "deepseek"), ("r18", "deepseek"), ("r16", "google")])
def test_frozen_three_executions_keep_original_parent_budget_and_material(acceptance_public_root: Path, prepared_root: Path, risk: str, protocol: str) -> None:
    import asyncio
    import json
    from hashlib import sha256
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import AcceptancePlan, FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate, prepare_live, run_plan
    materials = Path(__file__).parent / "materials/save-import-recovery"
    specification = materials / "risk-materials.json"
    family = json.loads(specification.read_bytes())["families"][risk]
    cap = family["request_cap"]
    rows = [FrozenExecution(execution_id=f"{risk}:{protocol}:{index}", case_id=family["case_id"], protocol=protocol,
        scenario_id=family["scenario_id"], material_family_id=family["family_id"], material_variant=item["material_variant"],
        material_path=materials/item["material"], material_sha256=sha256((materials/item["material"]).read_bytes()).hexdigest(),
        equivalence_path=specification, equivalence_sha256=sha256(specification.read_bytes()).hexdigest(),
        startpoint=f"{risk}-{protocol}-isolated-{index}", order=index, risk_branch=family["branch"], request_cap=cap,
        cost_cap_microusd=cap*122880) for index,item in enumerate(family["executions"],1)]
    parent = FrozenParentBudget(parent_id="synthetic-recovery-"+prepared_root.name, accounting_root=prepared_root,
        request_cap=3*cap, phase_microusd={"screening":3*cap*122880,"quality":0,"judge":0}, reserve_microusd=0, profile="synthetic")
    plan = AcceptancePlan(executions=rows, parent_budget=parent)
    registry = builtin_registry()
    activation = project_root(Path(__file__)) / ".scratch/acceptance-parent-budgets" / (sha256(parent.parent_id.encode()).hexdigest()+".json")
    assert not activation.exists() and not prepared_root.exists()
    observation = acceptance_public_root.with_name(acceptance_public_root.name+"-precreation.json")
    observation.parent.mkdir(parents=True,exist_ok=True)
    observation.write_text(json.dumps({"prepared_root":str(prepared_root),"parent_activation":str(activation),"both_absent_before":True,
        "owner":"ticket13 synthetic recovery matrix","delete_when":"Owned processes exit and necessary original evidence is preserved."},indent=2)+"\n",encoding="utf-8")
    try:
        bundle = prepare_live(plan.model_copy(update={"mode":"prepare-live"}), root=prepared_root, registry=registry)
        grant = LiveAuthorization(authorization_id="synthetic-recovery-matrix", binding=bundle.binding, transport="mock", parent_budget_id=parent.parent_id,
            total_limit=3*cap, case_limits={row.execution_id:cap for row in rows}, total_microusd=3*cap*122880,
            case_microusd={row.execution_id:cap*122880 for row in rows})
        result = asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
        assert [case.execution_id for case in result.cases] == [row.execution_id for row in rows]
        for row,case in zip(rows,result.cases):
            assert case.tool_status == "passed", case.facts
            assert case.critical_contracts["K3"] == "passed"
            assert case.product_status == "passed", case.facts
            contract = case.facts["recovery_contract"]
            assert contract["open_http_attempts"] == 0
            assert contract["identity_scope_approval_preserved"] is True
            assert contract["usage_retained"] is True
            assert contract["duplicate_mutating_actions"] == []
            assert case.facts["real_http_attempts"] == 0 and case.facts["processes_complete"] is True
            assert len(case.facts["phases"]) == 2 and all(item["exit_code"] == 0 for item in case.facts["phases"])
            review = json.loads(case.evidence["recovery_review"].read_bytes())
            assert review["execution_id"] == row.execution_id
            assert review["frozen_execution"]["material_sha256"] == row.material_sha256
            assert review["human_review"] == "not_performed"
            material = (materials/family["executions"][row.order-1]["material"]).read_text(encoding="utf-8").strip()
            exchange = json.loads(case.evidence["work/receiver/exchange.json"].read_bytes())
            assert all(material in json.dumps(item["messages"],ensure_ascii=False) for item in exchange["sdk_inputs"])
            ledger = json.loads(case.evidence["ledger.json"].read_bytes())
            actual = [item for item in ledger["attempts"] if item["execution_id"] == row.execution_id]
            assert 0 < len(actual) <= cap
            assert all(item["role"] == "product" and item["reservation_microusd"] > 0 for item in actual)
            if risk == "r16":
                assert contract["independent_check_receipt_verified"] is True
                assert contract["completed_edits"] == contract["completed_checks"] == 1
            elif risk == "r17":
                assert contract["planned_local_save_exit"] and contract["reliable_and_pending_preserved"]
                assert contract["unknown_verified_before_resume"] and contract["imported_unknown_blocks_resume"]
                assert case.evidence["work/receiver/save-diagnostics.json.incomplete-original"].read_bytes() == b""
            else:
                assert contract["child_scope_and_account_preserved"] and contract["applied_supplement_preserved_once"]
                assert contract["child_record_unchanged"] and contract["unfinished_parent_work_retained"]
                assert {item["actor"] for item in actual} == {"main", "delegation"}
            assert case.semantic_status == "not_measured" and case.user_acceptance == "pending"
        assert json.loads(result.result_path.read_bytes())["planned_denominator"] == 3
    finally:
        if activation.exists() and not any((prepared_root/".scratch").glob("execution-*")):
            original = json.loads(activation.read_bytes())
            assert original["parent_id"] == parent.parent_id and Path(original["accounting_root"]).resolve() == prepared_root.resolve()
            assert not activation.is_symlink() and not activation.is_junction()
            activation.unlink()
