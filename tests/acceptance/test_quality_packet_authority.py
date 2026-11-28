"""Canonical quality packets retain their execution and review boundaries."""

import asyncio
import json

import pytest

from tests.acceptance.test_frozen_execution_control import execution, registry_for_product
from tests.support.agent_acceptance import AcceptancePlan


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_review_originals_are_inaccessible_to_actual_read_command_and_child_tools(protocol, acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_files import exercise_files

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_files(reference, "QG1.tuning.1", protocol, root=acceptance_public_root, probe_review_originals=True))
        proof = outcome.facts["review_material_access"]
        assert proof["originals_preserved"] is True
        assert proof["read_gold_denied"] is True
        assert proof["read_input_denied"] is True
        assert proof["read_freeze_denied"] is True
        assert proof["actual_command_denied"] is True
        assert proof["child_inherited_denial"] is True
        assert proof["private_guard_absent_from_sdk_and_wire"] is True
        assert outcome.facts["real_http_attempts"] == 0
        assert outcome.semantic_status == "not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_runtime_permission_record_cannot_remove_canonical_review_denials(acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        bind_packet(reference, "QG1.tuning.1", acceptance_public_root)
        record = acceptance_public_root / "quality-task.json"
        value = json.loads(record.read_bytes())
        value["denied_paths"] = []
        record.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError, match="permission|denial"):
            manifest("quality.qg1.tuning.1", acceptance_public_root, "deepseek")
        assert not (acceptance_public_root / "conversation.json").exists()
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_packet_cannot_erase_owner_domain_standards_and_self_report_ready():
    from hashlib import sha256
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_packet import QualityPacketRef
    from tests.support.agent_acceptance.output_review import digest

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        path = packet / "freeze.json"
        value = json.loads(path.read_bytes())
        next(task for task in value["tasks"] if task["task_id"] == "QG1.tuning.1")["domain_standards"] = {}
        value["binding"] = digest({name: item for name, item in value.items() if name != "binding"})
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        weakened = QualityPacketRef(path=path, sha256=sha256(path.read_bytes()).hexdigest())
        with pytest.raises(ValueError, match="standard|criteria"):
            builtin_registry(quality_packet=weakened)
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_caller_cannot_replace_a_canonical_quality_handler_before_planning(prepared_root):
    from dataclasses import replace
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance import builtin_registry, quality_plan, FrozenParentBudget
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    async def caller_supplied(_context):
        raise AssertionError("Caller handler must never become a canonical task adapter")
    try:
        reference = build_implementation_quality_packet(packet)
        registry = builtin_registry(quality_packet=reference)
        identity = "quality.qg1.tuning.1"
        registry.cases[identity] = replace(registry.cases[identity], run=caller_supplied)
        parent = FrozenParentBudget(parent_id="synthetic-handler-" + packet.name, accounting_root=prepared_root,
            request_cap=2520, phase_microusd={"screening": 0, "quality": 39076920, "judge": 0}, reserve_microusd=0, profile="synthetic")
        with pytest.raises(ValueError, match="canonical"):
            quality_plan(packet / "freeze.json", registry=registry, parent_budget=parent)
        assert not prepared_root.exists()
    finally:
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.parametrize(("scope", "expected_block"), [("protocol", False), ("execution", False), ("shared", False), ("protocol", True)])
def test_candidate_report_keeps_unaffected_protocols_eligible_after_local_stop(scope, expected_block, prepared_root):
    from tests.support.agent_acceptance import FrozenParentBudget, prepare_live, LiveGate, LiveAuthorization, quality_candidate_status
    from tests.acceptance.test_frozen_execution_control import own_synthetic_index

    registry = registry_for_product()
    rows = tuple(execution(identity, protocol=protocol, repeat=order, scenario_id=scenario, stage="quality")
        for order, (identity, protocol, scenario) in enumerate((("tuning", "deepseek", "QG1.tuning.1"),
            ("formal-ds", "deepseek", "QG1.formal.1"), ("formal-gem", "google", "QG1.formal.3")), 1))
    parent = FrozenParentBudget(parent_id="synthetic-local-stop-" + prepared_root.name, accounting_root=prepared_root,
        request_cap=3, phase_microusd={"screening": 0, "quality": 60213, "judge": 0}, reserve_microusd=0, profile="synthetic")
    own_synthetic_index(parent)
    plan = AcceptancePlan(executions=rows, parent_budget=parent, mode="prepare-live")
    bundle = prepare_live(plan, root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-local-stop", binding=bundle.binding, total_limit=3,
        case_limits={row.execution_id: row.request_cap for row in rows}, total_microusd=60213,
        case_microusd={row.execution_id: row.cost_cap_microusd for row in rows}, transport="mock", parent_budget_id=parent.parent_id)
    first = rows[0]
    gate = LiveGate(prepared_root, registry, grant)
    source = prepared_root / "stop-observation.txt"
    source.write_text("Synthetic accepted local stop observation; no model service was called.", encoding="utf-8")
    gate.for_execution(first.execution_id).request_stop(scope=scope, reason="synthetic scoped stop", source=source,
        case_id=first.case_id, protocol=first.protocol, expected_block=expected_block)
    before = gate.accounting_report()
    state = quality_candidate_status(prepared_root, registry=registry)
    affected = {row.execution_id for row in rows if not expected_block and (scope == "shared"
        or scope == "protocol" and row.protocol == first.protocol or scope == "execution" and row.execution_id == first.execution_id)}
    assert set(state["stopped_execution_ids"]) == affected
    assert set(state["eligible_execution_ids"]) == {row.execution_id for row in rows} - affected
    assert state["state"] == ("stopped" if scope == "shared" else "frozen")
    assert state["sending_authorized"] is False
    assert state["scope_stops"] == before["scope_stops"]
    assert gate.accounting_report() == before
    assert before["actual_http"] == before["reserved_requests"] == 0



def test_canonical_packet_cannot_reduce_required_outputs_of_its_actual_workflow():
    from hashlib import sha256
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_packet import QualityPacketRef
    from tests.support.agent_acceptance.output_review import digest

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        path = packet / "freeze.json"
        value = json.loads(path.read_bytes())
        next(task for task in value["tasks"] if task["task_id"] == "QG1.formal.10")["required_outputs"] = {"main_answer": 1}
        value["binding"] = digest({name: item for name, item in value.items() if name != "binding"})
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        weakened = QualityPacketRef(path=path, sha256=sha256(path.read_bytes()).hexdigest())
        with pytest.raises(ValueError, match="output|workflow"):
            builtin_registry(quality_packet=weakened)
    finally:
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.parametrize("parameters", [
    {"reads": ["../outside.txt"]},
    {"reads": ["source.txt"], "edits": [{"path": "../outside.txt", "old_text": "a", "new_text": "b"}]},
    {"reads": ["source.txt"], "create": [{"path": "../outside.txt", "content": "b"}]},
    {"reads": ["source.txt"], "calculate": {"path": "../outside.csv", "column": "amount"}},
    {"reads": ["source.txt"], "deliver": ["../outside.txt"]},
])
def test_canonical_visible_file_operations_cannot_escape_the_task_folder(parameters):
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
    with pytest.raises(ValueError, match="path|folder"):
        QualityVisibleInput(task_id="QG2.formal.1", goal="Read only the actual task source.", adapter="qg2-1",
            sources={"source.txt": {"text": "Actual local task source.\n"}}, parameters=parameters)



def test_supplement_packet_freezes_only_initial_material_and_keeps_future_update_controller_only():
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE, source_material_path
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput, model_prompt

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        value = read_quality_packet(reference)
        task = next(item for item in value.tasks if item.task_id == "QG5.formal.1")
        visible = QualityVisibleInput.model_validate_json(source_material_path(task.input_path).read_bytes())
        assert "L73" in visible.goal and "周二14:10" in visible.goal
        assert set(visible.sources) == {"current-source.txt"}
        assert visible.sources["current-source.txt"].text == "reservation=L73\ntime=周二14:10\noptional_notice=unadopted\n"
        assert visible.parameters["workflow"]["after"] == "周三09:40"
        assert "周三09:40" not in model_prompt(visible)
        assert "周三09:40" not in visible.sources["current-source.txt"].text
        assert task.prior_uses == ("implementation",)
    finally:
        if packet.exists(): shutil.rmtree(packet)



def test_genesis_and_candidate_review_trees_stay_private_with_separate_legal_data_runtime(acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_files import exercise_files
    from tests.support.agent_acceptance.quality_packet import PACKETS
    packet = SOURCE / PACKETS / ("implementation-" + uuid4().hex)
    genesis = acceptance_public_root / "parent"
    active = genesis / ".candidates/active"
    old = genesis / ".candidates/older"
    old.mkdir(parents=True)
    (active / PACKETS).mkdir(parents=True)
    old_review = genesis / PACKETS / "old-freeze/gold.json"
    old_review.parent.mkdir(parents=True)
    old_review.write_text("old-genesis-review-marker-9147", encoding="utf-8")
    old_candidate = old / "gold.txt"
    old_candidate.write_text("old-candidate-review-marker-6208", encoding="utf-8")
    try:
        reference = build_implementation_quality_packet(packet)
        result = asyncio.run(exercise_files(reference, "QG1.tuning.1", "deepseek", root=genesis / ".scratch/execution-regression",
            source_roots=(genesis, active), probe_paths=(old_review, old_candidate)))
        assert result.product_status == "passed", result.facts
        assert result.facts["quality_native"]["actual_sources_read"] == ["weather.txt"]
        assert result.facts["additional_denial_probes"] == [True, True]
        native = result.evidence["exchange"].read_text(encoding="utf-8")
        assert "old-genesis-review-marker-9147" not in native
        assert "old-candidate-review-marker-6208" not in native
        assert old_review.read_text(encoding="utf-8") == "old-genesis-review-marker-9147"
        assert old_candidate.read_text(encoding="utf-8") == "old-candidate-review-marker-6208"
    finally:
        if packet.exists(): shutil.rmtree(packet)
