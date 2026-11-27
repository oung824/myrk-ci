"""Delegation contracts through the public formal Main and native product path."""

import asyncio
import json
from pathlib import Path

import pytest

from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan


def test_parent_verifies_actual_child_version_and_independent_final_check(acceptance_public_root: Path) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["formal.delegation"]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == "passed", outcome.facts
    contract = outcome.facts["delegation_contract"]
    assert contract["child_artifact_exact"] is True
    assert contract["parent_version_verified"] is True
    assert contract["final_independent_check"] is True
    assert contract["settings_inherited"] is True
    assert contract["shared_task_identity"] is True
    assert outcome.screening["scenario_id"] == "R10"
    assert outcome.screening["request_cap"] == 30
    assert outcome.semantic_status == "not_measured"
    assert outcome.user_acceptance == "pending"
    review=json.loads(outcome.evidence["delegation_review"].read_bytes())
    assert review["execution"]["execution_id"]==outcome.execution_id
    assert review["human_review"]=="not_performed"
    assert review["categories"]["artifact"] and review["categories"]["check_receipt"]
    assert all(row["product_attempt"]["step_id"] and row["product_attempt"]["own_number"] for row in review["request_bindings"])
    common=json.loads(outcome.evidence["semantic_review"].read_bytes())
    assert {output["type"] for output in common["outputs"]}=={"main_answer","child_report"}
    assert all(output["standard"]["version"]=="delegation-parent-check-v1::critical-contracts-v1" for output in common["outputs"])


def test_child_limit_returns_real_partial_artifact_then_parent_continues(acceptance_public_root: Path) -> None:
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=["formal.delegation-child-limit"]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.product_status==outcome.tool_status=="passed",outcome.facts
    contract=outcome.facts["delegation_contract"]
    assert contract["child_status"]=="completed"
    assert contract["child_at_cap"] is True
    assert contract["last_child_request_text_only"] is True
    assert contract["child_goal_status"]=="partial"
    assert contract["child_turns"]==contract["child_limit"]==3
    assert contract["partial_artifact_before_limit"] is True
    assert contract["unfinished_preserved"] is True
    assert contract["parent_continued_after_limit"] is True
    assert contract["parent_limit"] is None
    assert contract["parent_version_verified"] is True
    assert contract["final_independent_check"] is True
    assert outcome.screening["scenario_id"]=="R12"


def test_actual_child_outside_write_is_refused_without_unauthorized_effect(acceptance_public_root: Path) -> None:
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=["formal.delegation-scope-deny"]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.product_status==outcome.tool_status=="passed",outcome.facts
    contract=outcome.facts["delegation_contract"]
    assert contract["outside_action_actually_attempted"] is True
    assert contract["outside_action_denied"] is True
    assert contract["unauthorized_effect_absent"] is True
    assert contract["settings_inherited"] is True
    assert outcome.critical_contracts["K2"]=="passed"
    assert contract["approval_controls_applicable"] is True
    assert outcome.screening["scenario_id"]=="R11"
    assert outcome.screening["request_cap"]==24


@pytest.mark.parametrize("risk,protocol",[("r10","deepseek"),("r11","deepseek"),("r12","deepseek"),("r10","google")])
def test_three_predeclared_executions_share_original_parent_budget_and_actual_material(acceptance_public_root: Path, prepared_root: Path, risk: str, protocol: str) -> None:
    from hashlib import sha256
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate, prepare_live
    materials=Path(__file__).parent/"materials/delegation"
    specification=materials/"risk-materials.json"
    family=json.loads(specification.read_bytes())["families"][risk]
    cap=family["request_cap"]
    rows=[FrozenExecution(execution_id=f"{risk}:{protocol}:{number}",case_id=family["case_id"],protocol=protocol,
        scenario_id=family["scenario_id"],material_family_id=family["family_id"],material_variant=item["material_variant"],
        material_path=materials/item["material"],material_sha256=sha256((materials/item["material"]).read_bytes()).hexdigest(),
        equivalence_path=specification,equivalence_sha256=sha256(specification.read_bytes()).hexdigest(),
        startpoint=f"{risk}-{protocol}-isolated-{number}",order=number,risk_branch=family["branch"],request_cap=cap,cost_cap_microusd=cap*122880)
        for number,item in enumerate(family["executions"],1)]
    parent=FrozenParentBudget(parent_id="synthetic-delegation-"+prepared_root.name,accounting_root=prepared_root,request_cap=3*cap,
        phase_microusd={"screening":3*cap*122880,"quality":0,"judge":0},reserve_microusd=0,profile="synthetic")
    plan=AcceptancePlan(executions=rows,parent_budget=parent)
    registry=builtin_registry()
    activation=project_root(Path(__file__))/".scratch/acceptance-parent-budgets"/(sha256(parent.parent_id.encode()).hexdigest()+".json")
    assert not activation.exists() and not prepared_root.exists()
    observation=acceptance_public_root.with_name(acceptance_public_root.name+"-precreation.json")
    observation.parent.mkdir(parents=True,exist_ok=True)
    observation.write_text(json.dumps({"prepared_root":str(prepared_root),"parent_activation":str(activation),"both_absent_before":True,
        "owner":"synthetic delegation public test","delete_when":"Actual formal processes exit and necessary originals are preserved."},indent=2)+"\n",encoding="utf-8")
    try:
        bundle=prepare_live(plan.model_copy(update={"mode":"prepare-live"}),root=prepared_root,registry=registry)
        grant=LiveAuthorization(authorization_id="synthetic-frozen-delegation",binding=bundle.binding,transport="mock",parent_budget_id=parent.parent_id,
            total_limit=3*cap,case_limits={row.execution_id:cap for row in rows},total_microusd=3*cap*122880,
            case_microusd={row.execution_id:cap*122880 for row in rows})
        result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
        assert [case.execution_id for case in result.cases]==[row.execution_id for row in rows]
        for row,case in zip(rows,result.cases):
            assert case.product_status==case.tool_status=="passed",case.facts
            assert case.facts["real_http_attempts"]==0 and case.facts["processes_complete"] is True
            review=json.loads(case.evidence["delegation_review"].read_bytes())
            assert review["execution"]["execution_id"]==row.execution_id
            assert review["execution"]["material_sha256"]==row.material_sha256
            material=(materials/family["executions"][row.order-1]["material"]).read_text(encoding="utf-8").strip()
            exchange=json.loads(case.evidence["work/receiver/exchange.json"].read_bytes())
            assert all(material in json.dumps(sdk["messages"],ensure_ascii=False) for sdk in exchange["sdk_inputs"])
            assert case.evidence["materials/child/brief.txt"].read_text(encoding="utf-8").strip()==material
            ledger=json.loads(case.evidence["ledger.json"].read_bytes())
            actual=[item for item in ledger["attempts"] if item["execution_id"]==row.execution_id]
            assert 0<len(actual)<=cap and all(item["role"]=="product" and item["reservation_microusd"]>0 for item in actual)
            assert {item["actor"] for item in actual}=={"main","delegation"}
            assert case.semantic_status=="not_measured" and case.user_acceptance=="pending"
            contract=case.facts["delegation_contract"]
            assert contract["request_ownership_verified"] is True and contract["settings_inherited"] is True
            if risk=="r11":
                assert contract["outside_action_actually_attempted"] and contract["outside_action_denied"] and contract["unauthorized_effect_absent"]
            elif risk=="r12":
                assert contract["last_child_request_text_only"] and contract["partial_artifact_before_limit"] and contract["unfinished_preserved"]
                assert contract["child_goal_status"]=="partial" and contract["parent_continued_after_limit"]
                assert contract["child_turns"]==contract["child_limit"]==3 and contract["parent_limit"] is None
            else:
                assert contract["parent_version_verified"] and contract["final_independent_check"]
        if risk=="r10" and protocol=="deepseek":
            from tests.support.agent_acceptance import export_review
            first=result.cases[0]
            mapping=json.loads(first.evidence["long-path-originals.json"].read_bytes())
            assert mapping
            original_root=first.evidence["long-path-originals.json"].parent
            for row in mapping:
                raw=(original_root/row["stored_path"]).read_bytes()
                assert sha256(raw).hexdigest()==row["sha256"] and len(raw)==row["bytes"]
                assert first.facts["evidence_relocation"][row["source_path"]]==row["stored_path"]
            child_record=next(path for label,path in first.evidence.items() if label.startswith("records/") and path.suffix==".json"
                and json.loads(path.read_bytes()).get("turns",[{}])[-1].get("request_owner_id")==first.facts["delegation_contract"]["delegation_id"])
            assert json.loads(child_record.read_bytes())["turns"][-1]["execution_scope"]==first.facts["delegation_contract"]["delegation"]["scope"]
            package=export_review(result.result_path,acceptance_public_root.with_name("review-"+acceptance_public_root.name[:8]))
            body=json.loads(package.read_bytes())
            assert len(body["outputs"])==6 and {output["type"] for output in body["outputs"]}=={"main_answer","child_report"}
            assert len({output["execution_id"] for output in body["outputs"]})==3
            target=original_root/mapping[0]["stored_path"]
            original=target.read_bytes()
            try:
                target.write_bytes(b"controlled wrong original bytes")
                with pytest.raises(ValueError,match="published execution fingerprint"):
                    export_review(result.result_path,acceptance_public_root.with_name("bad-"+acceptance_public_root.name[:8]))
            finally:
                target.write_bytes(original)
        assert json.loads(result.result_path.read_bytes())["planned_denominator"]==3
    finally:
        if activation.exists() and not any((prepared_root/".scratch").glob("execution-*")):
            assert not activation.is_symlink() and not activation.is_junction()
            data=json.loads(activation.read_bytes())
            assert data["parent_id"]==parent.parent_id and Path(data["accounting_root"]).resolve()==prepared_root.resolve()
            activation.unlink()


@pytest.mark.parametrize("suffix,product,verification",[("equivalent-read","passed","passed"),("unclassified-inspection","inconclusive","unknown"),("wrong-version","failed","failed")])
def test_parent_verification_classifies_equivalent_unknown_and_disproved_paths(acceptance_public_root: Path,suffix: str,product: str,verification: str) -> None:
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=["formal.delegation-"+suffix]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status=="passed",outcome.facts
    assert outcome.product_status==product,outcome.facts
    contract=outcome.facts["delegation_contract"]
    assert contract["verification_evidence_status"]==verification
    if suffix=="wrong-version":
        assert contract["child_artifact_exact"] is False
        assert contract["false_claim_counterevidence"] is True
        assert outcome.critical_contracts["K4"]=="failed"
    if suffix=="unclassified-inspection":
        assert outcome.deterministic_status=="not_measured"
        assert outcome.critical_contracts["K4"]=="unknown"
    assert outcome.semantic_status=="not_measured"
    from tests.acceptance.test_formal_scenarios import assert_formal_qualification
    assert_formal_qualification(outcome)


def test_public_live_mock_keeps_one_finish_and_actual_product_budget(acceptance_public_root: Path,prepared_root: Path) -> None:
    from tests.support.agent_acceptance import LiveAuthorization,LiveGate,prepare_live
    registry=builtin_registry()
    name="formal.delegation"
    bundle=prepare_live(AcceptancePlan(cases=[name],protocols=["openai-chat"],mode="prepare-live"),root=prepared_root,registry=registry)
    proposal=bundle.budget_proposals[name]
    grant=LiveAuthorization(authorization_id="synthetic-delegation-openai-live-mock",binding=bundle.binding,total_limit=30,case_limits={name:30},
        transport="mock",total_microusd=proposal.cost_limit_microusd,case_microusd={name:proposal.cost_limit_microusd})
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=[name],protocols=["openai-chat"],mode="live"),root=acceptance_public_root,
        registry=registry,live_gate=LiveGate(prepared_root,registry,grant))).cases[0]
    assert outcome.product_status==outcome.tool_status=="passed",outcome.facts
    assert outcome.facts["real_http_attempts"]==outcome.facts["normal_profile_accesses"]==0
    review=json.loads(outcome.evidence["delegation_review"].read_bytes())
    assert review["execution"]["execution_id"]==outcome.execution_id
    ledger=json.loads(outcome.evidence["ledger.json"].read_bytes())
    assert len(ledger["attempts"])==7
    assert {row["actor"] for row in ledger["attempts"]}=={"main","delegation"}
    assert all(row["role"]=="product" and row["execution_id"]==outcome.execution_id and row["reservation_microusd"]>0 for row in ledger["attempts"])
    assert all(row["product_attempt"]["step_id"] for row in review["request_bindings"])
    assert outcome.semantic_status=="not_measured" and outcome.user_acceptance=="pending"


def test_controlled_verification_paths_have_no_formal_live_sending_selector(acceptance_public_root: Path,prepared_root: Path) -> None:
    from tests.support.agent_acceptance import LiveAuthorization,LiveGate,SharedControlError,prepare_live
    registry=builtin_registry()
    names=["formal.delegation-equivalent-read","formal.delegation-unclassified-inspection","formal.delegation-wrong-version"]
    assert all(not registry.cases[name].formal_live and not registry.cases[name].frozen_material_input for name in names)
    name=names[-1]
    bundle=prepare_live(AcceptancePlan(cases=[name],mode="prepare-live"),root=prepared_root,registry=registry)
    proposal=bundle.budget_proposals[name]
    grant=LiveAuthorization(authorization_id="synthetic-delegation-control-selector",binding=bundle.binding,total_limit=30,case_limits={name:30},
        transport="mock",total_microusd=proposal.cost_limit_microusd,case_microusd={name:proposal.cost_limit_microusd})
    with pytest.raises(SharedControlError,match="no authorized formal service adapter"):
        asyncio.run(run_plan(AcceptancePlan(cases=[name],mode="live"),root=acceptance_public_root,registry=registry,
            live_gate=LiveGate(prepared_root,registry,grant)))
    assert json.loads((prepared_root/"ledger.json").read_bytes())["attempts"]==[]
    assert not list(prepared_root.rglob("authorization.json"))
    originals=acceptance_public_root.with_name("selector-"+acceptance_public_root.name[:8])
    originals.mkdir(parents=True,exist_ok=False)
    for leaf in ("prepared.json","ledger.json"):(originals/leaf).write_bytes((prepared_root/leaf).read_bytes())
    (originals/"synthetic-grant.json").write_text(grant.model_dump_json(indent=2)+"\n",encoding="utf-8")


def test_frozen_delegation_without_original_parent_gate_creates_no_replacement(acceptance_public_root: Path) -> None:
    from hashlib import sha256
    from tests.support.agent_acceptance import FrozenExecution,SharedControlError
    materials=Path(__file__).parent/"materials/delegation"
    specification=materials/"risk-materials.json"
    family=json.loads(specification.read_bytes())["families"]["r10"]
    rows=[FrozenExecution(execution_id=f"no-parent:{n}",case_id="formal.delegation",protocol="deepseek",scenario_id="R10",
        material_family_id=family["family_id"],material_variant=item["material_variant"],material_path=materials/item["material"],
        material_sha256=sha256((materials/item["material"]).read_bytes()).hexdigest(),equivalence_path=specification,
        equivalence_sha256=sha256(specification.read_bytes()).hexdigest(),startpoint=f"no-parent-{n}",order=n,risk_branch=family["branch"],
        request_cap=30,cost_cap_microusd=30*122880) for n,item in enumerate(family["executions"],1)]
    with pytest.raises(SharedControlError,match="original parent gate"):
        asyncio.run(run_plan(AcceptancePlan(executions=rows),root=acceptance_public_root,registry=builtin_registry()))
    saved=json.loads((acceptance_public_root/"result.json").read_bytes())
    assert [case["execution_status"] for case in saved["cases"]]==["invalid","not_executed","not_executed"]
    assert not list(acceptance_public_root.rglob("authorization.json")) and not list(acceptance_public_root.rglob("ledger.json"))


def test_child_at_limit_parent_finishes_but_missing_final_check_is_product_failure(acceptance_public_root: Path) -> None:
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=["formal.delegation-missing-final-check"]),root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status=="passed",outcome.facts
    assert outcome.product_status==outcome.case_status=="failed"
    contract=outcome.facts["delegation_contract"]
    assert contract["child_goal_status"]=="partial" and contract["child_turns"]==contract["child_limit"]==3
    assert contract["partial_artifact_before_limit"] and contract["unfinished_preserved"] and contract["parent_continued_after_limit"]
    assert contract["parent_version_verified"] is True and contract["final_independent_check"] is False
    assert contract["required_final_check_missing"] is True and contract["receipts"]==[]
    assert not [action for action in outcome.facts["actions"] if action["kind"]=="check"]
    exchange=json.loads(outcome.evidence["work/receiver/exchange.json"].read_bytes())
    assert not [part for sdk in exchange["sdk_inputs"] for message in sdk["messages"] for part in message["parts"] if part.get("part_kind")=="tool-call" and part.get("tool_name")=="run_check"]
    assert outcome.critical_contracts["K4"]=="unknown" and contract["false_claim_counterevidence"] is False
    assert outcome.semantic_status=="not_measured" and outcome.user_acceptance=="pending"
    from tests.acceptance.test_formal_scenarios import assert_formal_qualification
    assert_formal_qualification(outcome)
