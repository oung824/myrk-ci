"""Frozen memory sources reach the public native Main path."""
import asyncio
from hashlib import sha256
from pathlib import Path
import shutil
from uuid import uuid4
import pytest


def verify_qg47_review_round_trip(frozen: Path, result_path: Path, destination: Path) -> None:
    import json

    from tests.support.agent_acceptance.output_review import ReviewOriginal, export_review, read_package
    from tests.support.agent_acceptance.quality import SyntheticQualityReview, export_quality_review, import_quality_reviews

    package = export_quality_review(frozen, result_path, destination)
    exported = read_package(package)
    original_case, = exported["original_cases"]
    outputs = exported["outputs"]
    versions = {"main_answer": "quality-memory-v1", "memory": "quality-memory-effects-v1"}
    assert {output["type"] for output in outputs} == versions.keys()
    for output in outputs:
        assert output["standard"]["domain_version"] == versions[output["type"]]
        assert "K4" in output["standard"]["dimensions"]
    context = exported["review_originals"][original_case["execution_id"]]
    assert set(context) == {"quality.input", "quality.gold", "quality.freeze"}
    for label, item in context.items():
        original = exported["originals"][item["original_key"]]
        saved = result_path.parent / original_case["evidence"][label]
        assert original["sha256"] == item["sha256"] == sha256(saved.read_bytes()).hexdigest()
        assert original["product_output"] is False
        for output in outputs:
            assert output["required_originals"].count(item["original_key"]) == 1

    forms = []
    for index, output in enumerate(outputs):
        conclusions = {name: "unknown" if rule["applicable"] else "not_applicable"
                       for name, rule in output["standard"]["dimensions"].items()}
        forms.append(SyntheticQualityReview(
            review_id=f"qg47-synthetic-{index}", output_id=output["output_id"], authorship="synthetic",
            package_sha256=exported["binding"], criteria_version=output["standard"]["version"],
            output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
            conclusions=conclusions, reasons={name: "Synthetic import flow only." for name in conclusions},
        ))
    imported = import_quality_reviews(frozen, package, forms, synthetic_destination=destination / "synthetic-import.json")
    imported_record = json.loads(imported.read_bytes())
    assert imported_record["authorship"] == "synthetic"
    assert len(imported_record["reviews"]) == len(outputs)
    assert not list((destination / "reviews").iterdir())

    original_input = result_path.parent / original_case["evidence"]["quality.input"]
    unauthorized_reuse = destination.with_name("undeclared-review-reuse")
    with pytest.raises(ValueError, match="cannot replace a product original"):
        export_review(result_path, unauthorized_reuse, review_originals={original_case["execution_id"]: {
            "quality.input": ReviewOriginal(path=original_input, sha256=sha256(original_input.read_bytes()).hexdigest()),
        }})
    assert not unauthorized_reuse.exists()

    mismatch = destination.with_name("different-review-original")
    with pytest.raises(ValueError, match="cannot replace a product original"):
        export_review(result_path, mismatch, review_originals={original_case["execution_id"]: {
            "quality.gold": ReviewOriginal(path=original_input, sha256=sha256(original_input.read_bytes()).hexdigest(),
                                           reuse_execution_original=True),
        }})
    assert not mismatch.exists()

    for defect in ("wrong-type", "missing-dimension", "conflicting-main"):
        changed_result = json.loads(result_path.read_bytes())
        facts = changed_result["cases"][0]["facts"]
        standards = facts["quality_output_standards"]
        if defect == "wrong-type":
            standards["memory"] = standards["main_answer"]
        elif defect == "missing-dimension":
            del standards["memory"]["dimensions"]["mutation_authority"]
        else:
            facts["output_standard"]["version"] = "conflicting-main-standard"
        changed_path = result_path.with_name(f"{defect}.json")
        changed_path.write_text(json.dumps(changed_result), encoding="utf-8")
        wrong_standard = destination.with_name(defect)
        with pytest.raises(ValueError, match="domain output standard"):
            export_quality_review(frozen, changed_path, wrong_standard)
        assert not wrong_standard.exists()


@pytest.mark.acceptance_desktop
@pytest.mark.parametrize("identity",["QG4.formal.2","QG4.formal.6","QG4.formal.7"])
def test_qg4_main_preserves_frozen_authority_or_reports_actual_permission_loss(identity: str, prepared_root: Path, acceptance_public_root: Path) -> None:
    import json
    import os
    import subprocess
    import sys
    from zipfile import ZipFile, ZIP_DEFLATED
    from tests.support.agent_acceptance import (FrozenParentBudget, LiveAuthorization, builtin_registry, prepare_live, quality_plan)
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    packet=SOURCE/".scratch/retro-agent-live-acceptance-20261116/quality-packets"/("qg4-main-"+uuid4().hex)
    parent=FrozenParentBudget(parent_id="synthetic-qg4-"+prepared_root.name,accounting_root=prepared_root,request_cap=2520,
        phase_microusd={"screening":0,"quality":39076920,"judge":0},reserve_microusd=0,profile="synthetic")
    index=Path("E:/Myrk/.scratch/acceptance-parent-budgets")/(sha256(parent.parent_id.encode()).hexdigest()+".json")
    assert not index.exists()
    bundle=None
    try:
        reference=build_implementation_quality_packet(packet)
        registry=builtin_registry(quality_packet=reference)
        full_plan=quality_plan(SOURCE/reference.path,registry=registry,parent_budget=parent,mode="offline")
        execution_id="synthetic:qg4:"+identity
        selected=next(row for row in full_plan.executions if row.scenario_id==identity and row.protocol=="deepseek")
        selected=selected.model_copy(update={"execution_id":execution_id,"order":1,"dependencies":()})
        assert len(full_plan.executions)==84
        parent=parent.model_copy(update={"request_cap":selected.request_cap,
            "phase_microusd":{"screening":0,"quality":selected.cost_cap_microusd,"judge":0}})
        plan=full_plan.model_copy(update={"executions":(selected,),"parent_budget":parent})
        assert registry.cases[selected.case_id].formal_live is True
        bundle=prepare_live(plan.model_copy(update={"mode":"prepare-live"}),root=prepared_root,registry=registry)
        grant=LiveAuthorization(authorization_id="synthetic-qg4-explicit",binding=bundle.binding,total_limit=selected.request_cap,
            case_limits={selected.execution_id:selected.request_cap},total_microusd=selected.cost_cap_microusd,
            case_microusd={selected.execution_id:selected.cost_cap_microusd},parent_budget_id=parent.parent_id,transport="mock")
        acceptance_public_root.mkdir(parents=True,exist_ok=True)
        request=acceptance_public_root/"synthetic-execution-request.json"
        request.write_text(json.dumps({"run_root":str(prepared_root),"grant":grant.model_dump(mode="json")}),encoding="utf-8")
        output=acceptance_public_root/"execution"
        process=subprocess.run([sys.executable,"-B","-m","tests.support.agent_acceptance.formal_cli","execute",str(request),"--output",str(output)],
            cwd=prepared_root,env={**os.environ,"PYTHONPATH":str(prepared_root),"PYTHONDONTWRITEBYTECODE":"1"},capture_output=True)
        (acceptance_public_root/"cli.stdout").write_bytes(process.stdout)
        (acceptance_public_root/"cli.stderr").write_bytes(process.stderr)
        assert process.returncode==0,process.stderr.decode("utf-8",errors="replace")
        result=json.loads((output/"result.json").read_bytes())
        outcome=next(row for row in result["cases"] if row["execution_id"]==execution_id)
        assert outcome["tool_status"]=="passed",outcome["facts"]
        contract=outcome["facts"]["quality_memory_contract"]
        assert contract["task_id"]==identity
        if contract.get("known_product_gap") == "temporary_connection_resets_new_chat_permissions":
            assert outcome["product_status"]=="failed"
            assert outcome["critical_contracts"]["K1"]=="failed"
            assert contract["new_chat_http_attempts"]==0
            assert contract["refused_before_terminal_http"] is True
            assert contract["dependent_steps_not_started"] is True
            preserved=outcome["facts"]["known_default_permission_loss"]
            assert preserved["kind"]=="public_new_default_permissions_lost"
            assert preserved["ledger_stopped"] is True
            assert preserved["actual_terminal_http_before"]==preserved["actual_terminal_http_after"]>0
            assert preserved["source_originals"]
            assert contract["source_originals_unchanged"] is True
            assert contract["original_history_preserved"] is True
            assert outcome["semantic_status"]=="not_measured"
            assert outcome["facts"]["real_http_attempts"]==0
            assert result["planned_executions"]==[selected.model_dump(mode="json")]
            assert len(result["cases"])==1
            return
        assert outcome["product_status"]=="passed",outcome["facts"]
        assert contract["frozen_tool_denials_preserved"] is True
        assert contract["actual_source_read"] is True
        assert contract["original_history_preserved"] is True
        if identity=="QG4.formal.6":
            assert contract["initial_b_isolated"] is True
            assert contract["source_seen_after_approval"] is True
            assert contract["approval_content_binding"]=="bound"
            assert contract["recipient_binding_matches_actual_provider"] is True
            assert outcome["facts"]["fact_contract_status"]=="passed"
            assert outcome["critical_contracts"]["K1"]==outcome["critical_contracts"]["K2"]=="passed"
        elif identity=="QG4.formal.9":
            assert contract["withdrawal_received"] is True
            assert contract["source_not_sent_after_withdrawal"] is True
        elif identity=="QG4.formal.5":
            assert contract["project_b_sdk_isolated"] is True
            assert contract["project_b_wire_isolated"] is True
        elif identity=="QG4.formal.7":
            assert contract["ambiguous_request_saved"] is True
            assert contract["no_deletion_observed"] is True
            assert outcome["critical_contracts"]["K4"] == "unknown"
            assert outcome["case_status"] == outcome["content_status"] == "pending_review"
            verify_qg47_review_round_trip(SOURCE / reference.path, output / "result.json", acceptance_public_root / "quality-review-package")
        else:
            assert contract["remembered_in_fresh_chat"] is True
        assert outcome["semantic_status"]=="not_measured"
        assert outcome["facts"]["real_http_attempts"]==0
        assert result["planned_executions"]==[selected.model_dump(mode="json")]
        assert len(result["cases"])==1
    finally:
        if bundle is not None:
            with ZipFile(acceptance_public_root/"executed-source-snapshot.zip","x",ZIP_DEFLATED) as archive:
                for name,expected in bundle.files.items():
                    content=(prepared_root/name).read_bytes()
                    assert sha256(content).hexdigest()==expected
                    archive.writestr(name,content)
        if packet.exists():
            review=acceptance_public_root/"qg4-review-context"
            review.mkdir(parents=True,exist_ok=False)
            for label,source in (("freeze",packet/"freeze.json"),("input",packet/("input/"+identity+".json")),("gold",packet/("gold/"+identity+".json"))):
                if source.exists(): (review/(label+".original.json")).write_bytes(source.read_bytes())
        if index.exists(): index.unlink()
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.parametrize("identity,fact",[("QG4.formal.1","设备维护清单按地点分组，地点内按截止日期排序。"),("QG4.tuning.1","路径说明使用路口名称，不使用店铺名称。")])
def test_qg4_native_exercise_recalls_its_source_fact_without_claiming_a_formal_sample(identity: str,fact: str,acceptance_public_root: Path) -> None:
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_memory import exercise_memory

    packet=SOURCE/".scratch/retro-agent-live-acceptance-20261116/quality-packets"/("qg4-native-"+uuid4().hex)
    try:
        reference=build_implementation_quality_packet(packet)
        outcome=asyncio.run(exercise_memory(reference,identity,"deepseek",root=acceptance_public_root/"personal-native"))
        assert outcome.tool_status==outcome.product_status=="passed",outcome.facts
        contract=outcome.facts["quality_memory_contract"]
        assert contract["current_texts"]==[fact]
        assert contract["actual_source_read"] is True
        assert contract["remembered_in_fresh_chat"] is True
        assert contract["original_history_preserved"] is True
        assert outcome.facts["formal_execution"] is False
        assert outcome.facts["independent_quality_sample_increment"]==0
        assert outcome.semantic_status=="not_measured"
        assert outcome.evidence["quality.freeze"].is_file()
        assert outcome.evidence["quality.gold"].is_file()
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_qg4_current_correction_and_deletion_preserve_seed_history(acceptance_public_root: Path) -> None:
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_memory import exercise_memory
    packet=SOURCE/".scratch/retro-agent-live-acceptance-20261116/quality-packets"/("qg4-correction-"+uuid4().hex)
    try:
        reference=build_implementation_quality_packet(packet)
        outcome=asyncio.run(exercise_memory(reference,"QG4.formal.2","deepseek",root=acceptance_public_root/"correction-native"))
        assert outcome.product_status=="passed",outcome.facts
        contract=outcome.facts["quality_memory_contract"]
        assert contract["fresh_chat_current_facts"]==[["蓝港仓储收件时间周三13:00。"],["蓝港仓储收件时间周四16:30。"],[]]
        assert contract["current_texts"]==[]
        assert contract["current_deletion_only"] is True
        assert contract["original_history_preserved"] is True
        assert outcome.critical_contracts["K1"]=="passed"
        assert outcome.semantic_status=="not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.parametrize("identity,label,fact",[
    ("QG4.formal.3","霜叶","传感器偏移由接头松动引起，重新固定后由7.2降到0.4；未验证固件升级。"),
    ("QG4.tuning.2","绿顶","本次湿度计换电池后恢复读数，只在这一次观察中成立。")])
def test_qg4_project_fact_uses_its_real_project_namespace(identity: str,label: str,fact: str,acceptance_public_root: Path) -> None:
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_memory import exercise_memory
    packet=SOURCE/".scratch/retro-agent-live-acceptance-20261116/quality-packets"/("qg4-project-"+uuid4().hex)
    try:
        reference=build_implementation_quality_packet(packet)
        outcome=asyncio.run(exercise_memory(reference,identity,"deepseek",root=acceptance_public_root/("project-"+label)))
        assert outcome.product_status=="passed",outcome.facts
        contract=outcome.facts["quality_memory_contract"]
        assert contract["current_texts"]==[fact]
        from myrk.memory import MemoryStore
        root=acceptance_public_root/("project-"+label)
        saved=MemoryStore(root/"records/memories").read()
        assert [(item.text,item.scope,item.project) for item in saved.facts]==[(fact,"project",str((root/"projects"/label).resolve()))]
        if identity=="QG4.formal.3":
            assert Path(contract["project_root"]).name==label
            assert contract["scope_kind"]=="project"
            assert contract["remembered_in_fresh_chat"] is True
        else:
            assert contract["namespace_preserved"] is True
            assert contract["bounded_review_status"]=="passed"
            assert contract["memory_review_operations"]==1
        assert contract["actual_source_read"] is True
        assert outcome.semantic_status=="not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_qg4_project_isolation_keeps_the_other_project_fact_out_of_native_input(acceptance_public_root: Path) -> None:
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_memory import exercise_memory
    packet=SOURCE/".scratch/retro-agent-live-acceptance-20261116/quality-packets"/("qg4-isolation-"+uuid4().hex)
    try:
        reference=build_implementation_quality_packet(packet)
        outcome=asyncio.run(exercise_memory(reference,"QG4.formal.5","deepseek",root=acceptance_public_root/"isolation-native"))
        assert outcome.product_status=="passed",outcome.facts
        contract=outcome.facts["quality_memory_contract"]
        assert contract["project_a_current_texts"]==["红队周一负责三号展厅。"]
        assert contract["current_texts"]==[]
        assert Path(contract["project_root"]).name=="温室"
        assert contract["project_b_sdk_isolated"] is True
        assert contract["project_b_wire_isolated"] is True
        assert outcome.critical_contracts["K1"]=="passed"
    finally:
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.parametrize("identity,status,seen,protocol",[("QG4.formal.6","inconclusive",True,"deepseek"),("QG4.formal.9","passed",False,"deepseek"),("QG4.formal.9","passed",False,"google")])
def test_qg4_cross_project_recall_obeys_the_actual_approval_or_withdrawal(identity: str,status: str,seen: bool,protocol: str,acceptance_public_root: Path) -> None:
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_memory import exercise_memory
    packet=SOURCE/".scratch/retro-agent-live-acceptance-20261116/quality-packets"/("qg4-cross-"+uuid4().hex)
    try:
        reference=build_implementation_quality_packet(packet)
        outcome=asyncio.run(exercise_memory(reference,identity,protocol,root=acceptance_public_root/"cross-native"))
        assert outcome.tool_status=="passed" and outcome.product_status==status,outcome.facts
        contract=outcome.facts["quality_memory_contract"]
        assert contract["initial_b_isolated"] is True
        assert contract["source_seen_after_approval"] is seen
        assert contract["cross_project_effect"]==("completed" if seen else "denied")
        if seen:
            assert contract["observer_source_match"] is True
            assert contract["approval_content_binding"]=="unknown"
            assert outcome.facts["passed"] is True and outcome.facts["fact_contract_status"] == "unknown"
            assert outcome.critical_contracts["K2"]=="unknown"
        else:
            assert contract["withdrawal_received"] is True
            assert contract["source_not_sent_after_withdrawal"] is True
        assert outcome.semantic_status=="not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.parametrize("evidence,expected", [
    ("complete", "bound"),
    ("legacy_without_snapshot", "unknown"),
    ("denied_without_snapshot", "unknown"),
    ("missing_saved_prepared_action", "unknown"),
    ("conflicting_saved_digest", "failed"),
    ("conflicting_preview", "failed"),
    ("wrong_recipient", "failed"),
    ("other_command", "failed"),
    ("wrong_request_identity", "failed"),
    ("wire_only_mentions_approved_body", "failed"),
    ("leak_before_approval", "failed"),
])
def test_qg4_cross_project_score_checks_exact_approval_evidence(
    evidence: str, expected: str, acceptance_public_root: Path,
) -> None:
    import json

    from myrk.actions import Action
    from myrk.permissions import ApprovalSettings
    from tests.support.agent_acceptance.formal import provider_config
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_memory import recall_prompt, score
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg4-score-" + uuid4().hex)
    root = acceptance_public_root / "cross-score"
    try:
        visible = bind_packet(build_implementation_quality_packet(packet), "QG4.formal.6", root)
        request = manifest("quality.qg4.formal.6", root, "deepseek")
        source, destination = (request[key]["root"] for key in ("scope", "project_b_scope"))
        original = visible.parameters["original"]
        provider = provider_config("deepseek").model_dump(mode="json")
        recipient = {key: provider[key] for key in ("name", "protocol", "base_url", "model")}
        action = Action(task_id="recipient-task", kind="memory_recall", target=source, status="awaiting_approval",
            impact=provider["base_url"] + " " + provider["model"], approval_id="recall-approval", approval_basis="pending",
            approval_context_sha256="b" * 64, approval_settings=ApprovalSettings(reviewer="user"),
            prepared_parameters={"source_project": source, "recipient_project": destination, "recipient": recipient,
                "content_sha256": sha256(original.encode()).hexdigest()})
        action.prepared_action_sha256 = action.prepared_digest()
        pending = action.model_dump(mode="json")
        presented = {key: item for key, item in pending.items()
                     if key not in {"prepared_action_sha256", "approval_context_sha256"}}
        presented["approval_preview"] = original
        finished = {**pending, "status": "completed", "effect": "completed", "approval_basis": "user", "output": original}
        before = {"turns": [{"prompt": request["prompt"], "task": {"scope": request["scope"], "approval": request["approval"]},
            "actions": [{"id": "source-read", "kind": "read", "status": "completed",
                "prepared_parameters": {"path": str(Path(source) / name)}} for name in visible.sources]}], "messages": []}
        (root / "quality-memory-initial-history.json").write_text(json.dumps(before), encoding="utf-8")
        after = {"turns": [{"prompt": recall_prompt(visible, root), "task": {"id": "recipient-task",
            "scope": request["project_b_scope"], "approval": request["approval"], "provider_config": provider}, "actions": [finished]}],
            "action_evidence": [{"phase": "prepared", "action": pending}]}
        sdk_before, sdk_after = [
            {"sdk_input_id": f"input-{number}", "request_identity": {"number": number}, "messages": [{"parts": parts}]}
            for number, parts in ((1, []), (2, [{"part_kind": "tool-return", "tool_name": "recall_project_memory",
                "tool_call_id": "recall-call", "content": original}]))]
        wire_before, wire_after = [
            {"sdk_input_id": sdk["sdk_input_id"], "request_identity": sdk["request_identity"],
                "url": provider["base_url"] + "/chat/completions",
                "request_utf8": json.dumps({"model": provider["model"], "messages": messages})}
            for sdk, messages in ((sdk_before, []), (sdk_after, [{"role": "tool", "tool_call_id": "recall-call", "content": original}]))]
        recorded_decision = {"action": "approve_action", "task_id": "recipient-task", "action_id": action.id,
            "approval_id": action.approval_id, "approved": True}
        observation = {"passed": True, "windows": [{"sdk_start": 0, "sdk_end": 2, "http_start": 0, "http_end": 2}],
            "current": {"facts": [{"scope": "project", "project": source, "text": original}]}, "seed_current": {},
            "approval_decisions": [{"action": presented, "approved": True, "source_content": [original],
                "snapshot_action": presented.copy(), "decision": recorded_decision,
                "sdk_count_before_decision": 1, "http_count_before_decision": 1}]}
        exchanges = [{"protocol": "deepseek", "sdk_inputs": [sdk_before, sdk_after], "exchanges": [wire_before, wire_after]}]
        approval = observation["approval_decisions"][0]
        if evidence == "legacy_without_snapshot":
            observation["approval_decisions"] = [{"action": pending, "approved": True, "source_content": [original]}]
        elif evidence == "denied_without_snapshot":
            approval["snapshot_action"] = None
            approval["approved"] = recorded_decision["approved"] = False
            finished.update(status="denied", effect="not_started", approval_basis="denied", output="")
            sdk_after["messages"] = []
            wire_after["request_utf8"] = wire_before["request_utf8"]
        elif evidence == "missing_saved_prepared_action":
            after["action_evidence"] = []
        elif evidence == "conflicting_saved_digest":
            after["action_evidence"][0]["action"]["prepared_action_sha256"] = "c" * 64
        elif evidence == "conflicting_preview":
            approval["snapshot_action"]["approval_preview"] = "Different pending words"
        elif evidence == "wrong_recipient":
            approval["action"]["prepared_parameters"] = {**pending["prepared_parameters"],
                "recipient": {**recipient, "model": "another-model"}}
        elif evidence == "other_command":
            recorded_decision["action"] = "snapshot"
        elif evidence == "wrong_request_identity":
            wire_after["request_identity"] = {"number": 99}
        elif evidence == "wire_only_mentions_approved_body":
            wire_after["request_utf8"] = json.dumps({"model": provider["model"], "messages": [
                {"role": "user", "content": original}, {"role": "tool", "tool_call_id": "recall-call", "content": "Wrong return"}]})
        elif evidence == "leak_before_approval":
            sdk_before["messages"] = [{"parts": [{"part_kind": "user-prompt", "content": original}]}]
        result = score(visible, root, [before, after], exchanges, observation=observation)
        status = "passed" if expected == "bound" else expected
        assert result["quality_memory_contract"]["approval_content_binding"] == expected
        assert result["passed"] is (expected != "failed" and evidence != "denied_without_snapshot")
        assert result["fact_contract_status"] == result["critical_contracts"]["K1"] == result["critical_contracts"]["K2"] == status
    finally:
        if packet.exists():
            shutil.rmtree(packet)


@pytest.mark.parametrize("identity",["QG4.formal.7","QG4.formal.8"])
def test_qg4_ambiguous_and_project_deletion_respect_the_current_user_event(identity: str,acceptance_public_root: Path) -> None:
    import json

    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_memory import ambiguous_goal, exercise_memory, score
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    packet=SOURCE/".scratch/retro-agent-live-acceptance-20261116/quality-packets"/("qg4-removal-"+uuid4().hex)
    try:
        reference=build_implementation_quality_packet(packet)
        outcome=asyncio.run(exercise_memory(reference,identity,"deepseek",root=acceptance_public_root/"removal-native"))
        assert outcome.product_status=="passed",outcome.facts
        contract=outcome.facts["quality_memory_contract"]
        assert contract["original_history_preserved"] is True
        assert contract["actual_source_read"] is True
        if identity.endswith(".7"):
            assert contract["fresh_chat_current_facts"]==[["小型报告偏好单页。","项目汇报偏好完整附录。"]]*2
            assert contract["ambiguous_request_saved"] is True
            assert contract["no_deletion_observed"] is True
            assert outcome.critical_contracts["K4"] == "unknown"
            assert outcome.case_status == outcome.content_status == "pending_review"

            visible = QualityVisibleInput.model_validate_json(outcome.evidence["quality.input"].read_bytes())
            records = [json.loads(path.read_bytes()) for label, path in outcome.evidence.items()
                       if label.startswith("conversation_")]
            observed = json.loads(outcome.evidence["observation"].read_bytes())
            exchange = json.loads(outcome.evidence["exchange"].read_bytes())
            goal = ambiguous_goal(visible)
            holder, = (record for record in records if any(turn["prompt"] == goal for turn in record["turns"]))
            turn, = (row for row in holder["turns"] if row["prompt"] == goal)
            runtime = acceptance_public_root / "removal-native"
            for answer in ("你要删除哪一条报告偏好？我先保留这两条。", "两条报告偏好已删除。"):
                turn["answer"] = answer
                rescored = score(visible, runtime, records, [exchange], observation=observed)
                assert rescored["passed"] is True
                assert rescored["critical_contracts"]["K4"] == "unknown"
            holder["turns"] = [row for row in holder["turns"] if row["prompt"] != goal]
            rescored = score(visible, runtime, records, [exchange], observation=observed)
            assert rescored["passed"] is False
            assert rescored["quality_memory_contract"]["ambiguous_request_saved"] is False
        else:
            assert contract["fresh_chat_current_facts"]==[["旧资料归档位置为北楼二层。"],[]]
            assert contract["current_deletion_only"] is True
            assert contract["current_texts"]==[]
        assert outcome.critical_contracts["K1"]=="passed"
        assert outcome.semantic_status=="not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_qg4_missing_cross_project_records_cannot_establish_critical_contracts(acceptance_public_root: Path) -> None:
    import json
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_memory import exercise_memory, score
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
    packet=SOURCE/".scratch/retro-agent-live-acceptance-20261116/quality-packets"/("qg4-missing-"+uuid4().hex)
    root=acceptance_public_root/"missing-records"
    try:
        reference=build_implementation_quality_packet(packet)
        outcome=asyncio.run(exercise_memory(reference,"QG4.formal.6","deepseek",root=root))
        visible=QualityVisibleInput.model_validate_json(outcome.evidence["quality.input"].read_bytes())
        observed=json.loads((root/"native-memory-observation.json").read_bytes())
        facts=score(visible,root,[],[json.loads(outcome.evidence["exchange"].read_bytes())],observation=observed)
        assert facts["passed"] is False
        assert facts["critical_contracts"]["K1"]=="unknown"
        assert facts["critical_contracts"]["K2"]=="unknown"
        assert facts["critical_contracts"]["K4"]=="unknown"
        assert facts["critical_contracts"]["K5"]=="unknown"
        (root/"projects"/visible.parameters["project"]/next(iter(visible.sources))).write_text("Altered actual source after execution.",encoding="utf-8")
        altered=score(visible,root,[],[json.loads(outcome.evidence["exchange"].read_bytes())],observation=observed)
        assert altered["critical_contracts"]["K5"]=="failed"
    finally:
        if packet.exists(): shutil.rmtree(packet)
