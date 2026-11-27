"""Memory contracts observed at the maintained public acceptance seam."""

from pathlib import Path
import pytest

from tests.support.agent_acceptance import AcceptancePlan, builtin_registry

def test_memory_screening_registers_exact_frozen_scope_and_caps() -> None:
    registry = builtin_registry()
    expected = {"formal.memory-lifecycle": ("D12",24), "formal.memory-review": ("D13",12),
        "formal.memory-isolation": ("R13",16), "formal.memory-cross-project": ("R14",16),
        "formal.memory-withdrawal": ("R15",16)}
    for name,(scenario,cap) in expected.items():
        case=registry.cases[name]
        assert case.owner_ticket=="11"
        assert case.frozen_material_input is True
        assert case.screening["deepseek"]["scenario_id"]==scenario
        assert case.screening["deepseek"]["request_cap"]==cap
    assert registry.cases["formal.memory-isolation"].screening["google"]["scenario_id"]=="R13"


def test_personal_lifecycle_reads_each_current_fact_in_a_new_actual_main_chat(acceptance_public_root: "Path") -> None:
    import asyncio
    from tests.support.agent_acceptance import run_plan
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=["formal.memory-lifecycle"]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.product_status==outcome.tool_status=="passed",outcome.facts
    contract=outcome.facts["memory_contract"]
    assert contract["fresh_chat_current_facts"]==["Synthetic preference: tea, 2 cups.","Synthetic preference: coffee, 3 cups.",""]
    assert contract["current_facts"]==[]
    assert contract["original_history_preserved"] is True
    assert contract["current_deletion_only"] is True
    assert outcome.semantic_status=="not_measured" and outcome.user_acceptance=="pending"


def test_project_a_never_enters_project_b_native_sdk_or_terminal_request(acceptance_public_root: Path) -> None:
    import asyncio
    from tests.support.agent_acceptance import run_plan
    case=asyncio.run(run_plan(AcceptancePlan(cases=["formal.memory-isolation"]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert case.tool_status==case.product_status=="passed",case.facts
    contract=case.facts["memory_contract"]
    assert contract["project_b_sdk_isolated"] is True
    assert contract["project_b_wire_isolated"] is True
    assert contract["cross_project_effect"]=="not_requested"
    assert case.semantic_status=="not_measured"


def test_cross_project_recall_binds_actual_source_content_and_recipient(acceptance_public_root: Path) -> None:
    import asyncio
    from tests.support.agent_acceptance import run_plan
    case=asyncio.run(run_plan(AcceptancePlan(cases=["formal.memory-cross-project"]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert case.tool_status==case.product_status=="passed",case.facts
    contract=case.facts["memory_contract"]
    assert contract["initial_b_isolated"] is True
    assert contract["pending_exposes_exact_content"] is True
    assert contract["approved_source_content_recipient_match"] is True
    assert contract["approval_content_binding"]=="bound"
    assert contract["cross_project_effect"]=="completed"
    assert contract["source_seen_after_approval"] is True
    assert case.facts["real_http_attempts"]==0
    assert case.semantic_status=="not_measured" and case.user_acceptance=="pending"


def test_public_stop_while_cross_project_recall_pending_supplies_no_source(acceptance_public_root: Path) -> None:
    import asyncio
    from tests.support.agent_acceptance import run_plan
    case=asyncio.run(run_plan(AcceptancePlan(cases=["formal.memory-withdrawal"]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert case.tool_status==case.product_status=="passed",case.facts
    contract=case.facts["memory_contract"]
    assert contract["cross_project_effect"]=="cancelled"
    assert contract["stop_accepted"] is True
    assert contract["no_request_or_action_after_stop"] is True
    assert contract["project_b_sdk_isolated"] is contract["project_b_wire_isolated"] is True
    assert case.critical_contracts["K3"]=="passed"


def test_bounded_review_uses_original_parent_attempts_once_and_preserves_delivery(acceptance_public_root: Path) -> None:
    import asyncio
    from tests.support.agent_acceptance import run_plan
    case=asyncio.run(run_plan(AcceptancePlan(cases=["formal.memory-review"]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert case.tool_status==case.product_status=="passed",case.facts
    contract=case.facts["memory_contract"]
    assert contract["memory_reviews"]==1
    assert contract["review_tools_memory_only"] is True
    assert contract["no_second_review"] is True
    assert contract["request_actors"]==["main","main","memory_review","memory_review"]
    assert contract["original_delivery_preserved"] is True


@pytest.mark.parametrize("risk,protocol",[("r13","deepseek"),("r14","deepseek"),("r15","deepseek"),("r13","google")])
def test_frozen_memory_risk_executes_two_baselines_and_one_equivalent_variant(risk: str, protocol: str, acceptance_public_root: Path, prepared_root: Path) -> None:
    import asyncio
    import json
    from hashlib import sha256
    from tests.support.agent_acceptance import FrozenExecution,FrozenParentBudget,LiveAuthorization,LiveGate,prepare_live,run_plan
    from myrk.project_paths import project_root
    materials=Path(__file__).parent/"materials/memory-isolation"
    specification=materials/"risk-materials.json"
    family=json.loads(specification.read_bytes())["families"][risk]
    rows=[FrozenExecution(execution_id=f"{risk}:{protocol}:{number}",case_id=family["case_id"],protocol=protocol,
        scenario_id=family["scenario_id"],material_family_id=family["family_id"],material_variant=item["material_variant"],
        material_path=materials/item["material"],material_sha256=sha256((materials/item["material"]).read_bytes()).hexdigest(),
        equivalence_path=specification,equivalence_sha256=sha256(specification.read_bytes()).hexdigest(),
        startpoint=f"{risk}-{protocol}-start-{number}",order=number,risk_branch=family["branch"],request_cap=16,cost_cap_microusd=16*122880)
        for number,item in enumerate(family["executions"],1)]
    parent=FrozenParentBudget(parent_id="synthetic-memory-"+prepared_root.name,accounting_root=prepared_root,request_cap=48,
        phase_microusd={"screening":48*122880,"quality":0,"judge":0},reserve_microusd=0,profile="synthetic")
    plan=AcceptancePlan(executions=rows,parent_budget=parent)
    registry=builtin_registry()
    activation=project_root(Path(__file__).resolve())/".scratch/acceptance-parent-budgets"/(sha256(parent.parent_id.encode()).hexdigest()+".json")
    assert not activation.exists()
    try:
        bundle=prepare_live(plan.model_copy(update={"mode":"prepare-live"}),root=prepared_root,registry=registry)
        grant=LiveAuthorization(authorization_id="synthetic-memory-fixed-terminal",binding=bundle.binding,transport="mock",parent_budget_id=parent.parent_id,
            total_limit=48,case_limits={row.execution_id:16 for row in rows},total_microusd=48*122880,case_microusd={row.execution_id:16*122880 for row in rows})
        result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
        assert [case.execution_id for case in result.cases]==[row.execution_id for row in rows]
        for row,case in zip(rows,result.cases):
            assert case.tool_status==case.product_status=="passed",case.facts
            if risk=="r14":
                assert case.facts["memory_contract"]["approved_source_content_recipient_match"] is True
            assert case.facts["real_http_attempts"]==case.facts["normal_profile_accesses"]==0
            assert case.facts["processes_complete"] is True
            domain=json.loads(case.evidence["memory_review"].read_bytes())
            assert domain["execution"]["material_sha256"]==row.material_sha256
            exchange=json.loads(case.evidence["work/receiver/exchange.json"].read_bytes())
            text=(materials/family["executions"][row.order-1]["material"]).read_text(encoding="utf-8").strip()
            assert all(text in json.dumps(item["messages"],ensure_ascii=False) for item in exchange["sdk_inputs"])
            ledger=json.loads(case.evidence["ledger.json"].read_bytes())
            actual=[item for item in ledger["attempts"] if item["execution_id"]==row.execution_id]
            assert 0<len(actual)<=16 and all(item["role"]=="product" and item["reservation_microusd"]>0 for item in actual)
            assert case.facts["memory_contract"]["bounded_parent_requests"] is True
            assert case.semantic_status=="not_measured" and case.user_acceptance=="pending"
        assert json.loads(result.result_path.read_bytes())["planned_denominator"]==3
    finally:
        if activation.exists() and not any((prepared_root/".scratch").glob("execution-*")):
            original=json.loads(activation.read_bytes())
            assert original["parent_id"]==parent.parent_id and Path(original["accounting_root"]).resolve()==prepared_root.resolve()
            assert not activation.is_symlink() and not activation.is_junction()
            activation.unlink()


def test_explicit_withdrawal_denies_pending_recall_and_never_supplies_a(acceptance_public_root: Path) -> None:
    import asyncio
    from tests.support.agent_acceptance import run_plan
    case=asyncio.run(run_plan(AcceptancePlan(cases=["formal.memory-revoked"]),root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert case.tool_status==case.product_status=="passed",case.facts
    contract=case.facts["memory_contract"]
    assert contract["cross_project_effect"]=="denied"
    assert contract["withdrawal_received"] is True
    assert contract["source_seen_after_approval"] is False
    assert contract["project_b_sdk_isolated"] is contract["project_b_wire_isolated"] is True
    assert builtin_registry().cases["formal.memory-revoked"].formal_live is False


def test_changed_source_after_pending_blocks_stale_approval(acceptance_public_root: Path) -> None:
    import asyncio
    from tests.support.agent_acceptance import run_plan
    case=asyncio.run(run_plan(AcceptancePlan(cases=["formal.memory-changed-source"]),root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert case.tool_status=="passed",case.facts
    contract=case.facts["memory_contract"]
    assert contract["source_content_changed_after_pending"] is True
    assert contract["cross_project_effect"]=="conflict",contract
    assert case.product_status=="passed",contract
    assert contract["pending_exposes_exact_content"] is True
    assert contract["approval_content_binding"]=="bound"
    assert contract["stale_approval_blocked"] is True
    assert contract["stale_approval_supplied_changed_source"] is False
    assert contract["project_b_sdk_isolated"] is contract["project_b_wire_isolated"] is True
    assert contract["source_seen_after_approval"] is False
    assert case.evidence["source-before.json"].read_bytes()!=case.evidence["source-after.json"].read_bytes()
    assert builtin_registry().cases["formal.memory-changed-source"].formal_live is False
    assert case.facts["real_http_attempts"]==0
    assert case.semantic_status=="not_measured" and case.user_acceptance=="pending"


def test_qg4_materials_reserve_ten_distinct_formal_and_two_separate_tuning_families() -> None:
    import json
    materials=Path(__file__).parent/"materials/memory-isolation"
    catalogue=json.loads((materials/"qg4-templates.json").read_bytes())
    rows=catalogue["templates"]
    formal=[row for row in rows if row["role"]=="formal_holdout"]
    tuning=[row for row in rows if row["role"]=="tuning"]
    assert len(formal)==10 and len(tuning)==2
    assert len({row["family_id"] for row in rows})==12
    assert len({row["task_form"] for row in formal})>=4
    assert [row["kind"] for row in formal]==["common"]*6+["variant"]*2+["recovery_or_refusal"]*2
    assert all((materials/row["material"]).is_file() for row in rows)
    assert all(row["execution_status"]=="template_not_executed" and row["human_review"]=="not_performed" for row in rows)
    assert all(row["implementation_material_used"] is False for row in formal)
    assert catalogue["screening_material_usage"]=="implementation_regression_not_formal_holdout"
