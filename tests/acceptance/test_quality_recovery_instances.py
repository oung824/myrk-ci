"""Distinct QG5 workflows use the public service and actual saved effects."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


def assert_frozen_sources_match(input_path: Path, materials: Path) -> None:
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    frozen = QualityVisibleInput.model_validate_json(input_path.read_bytes())
    for name, source in frozen.sources.items():
        assert (materials / name).read_bytes() == source.text.encode(source.encoding), name


def test_qg5_library_supplement_applies_once_after_the_actual_user_event(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-control-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, "QG5.formal.1", "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
        contract = outcome.facts["quality_workflow_contract"]
        assert contract["task_id"] == "QG5.formal.1"
        assert contract["supplement_applied_once"] is True
        assert contract["first_applied_request"] == 2
        assert contract["no_future_update_in_first_input"] is True
        assert contract["source_card_unchanged"] is True
        assert contract["edit_count"] == 1
        assert (acceptance_public_root / "materials/reservation.txt").read_text(encoding="utf-8") == "reservation=L73\ntime=周三09:40\noptional_notice=unadopted\n"
        assert outcome.facts["real_http_attempts"] == 0
        assert outcome.facts["formal_execution"] is False
        assert outcome.facts["independent_quality_samples_added"] == 0
        assert outcome.semantic_status == "not_measured"
    finally:
        if (packet / "freeze.json").is_file():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG5.formal.1.json", "gold/QG5.formal.1.json"):
                destination = original / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, destination)
        if packet.exists(): shutil.rmtree(packet)

def build_late_supplement_packet(packet: Path, monkeypatch):
    from tests.support.agent_acceptance import quality_recovery
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    task_id = "QG5.formal.7"
    later_time = "17:48"
    witness = "Synthetic witness for unrelated checkout R-924.\r\nReference only.\r\n"
    original_projection = quality_recovery.project_input

    def project_regression(visible: QualityVisibleInput) -> QualityVisibleInput:
        projected = original_projection(visible)
        if projected.task_id != task_id:
            return projected
        target = projected.parameters["workflow"]["filename"]
        return projected.model_copy(update={
            "sources": {target: projected.sources["current-source.txt"], "witness.txt": QualitySource(text=witness)},
            "parameters": {**projected.parameters,
                "workflow": {**projected.parameters["workflow"], "after": later_time},
                "supplement_arrival": {"timing": "after_target_read", "read_only_source": "witness.txt"}},
        })

    with monkeypatch.context() as fixture:
        fixture.setattr(quality_recovery, "project_input", project_regression)
        return build_implementation_quality_packet(packet)


def test_qg5_late_supplement_waits_for_target_read_and_preserves_attachment(acceptance_public_root, monkeypatch):
    from tests.support.agent_acceptance import quality_recovery
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    task_id = "QG5.formal.7"
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-late-" + uuid4().hex)
    try:
        reference = build_late_supplement_packet(packet, monkeypatch)
        outcome = asyncio.run(quality_recovery.exercise_recovery(reference, task_id, "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
        contract = outcome.facts["quality_workflow_contract"]
        assert contract["task_id"] == task_id
        assert contract["first_applied_request"] == 3
        assert contract["supplement_applied_once"] is True
        assert contract["late_arrival_verified"] is True
        assert contract["read_only_attachment_read"] is True
        assert contract["source_card_unchanged"] is True
        assert contract["edit_count"] == 1
        assert outcome.facts["actual_event_identity"] is True
        assert outcome.facts["real_http_attempts"] == 0
        assert outcome.facts["formal_execution"] is False
        assert outcome.facts["independent_quality_samples_added"] == 0
        registered = json.loads((packet / f"input/{task_id}.json").read_bytes())
        witness = registered["sources"]["witness.txt"]
        assert (acceptance_public_root / "materials/witness.txt").read_bytes() == witness["text"].encode(witness["encoding"])
        for name in ("service.events", "service.exchange", "conversation", "supplement_observation"):
            assert outcome.evidence[name].is_file(), name
        exchange = json.loads(outcome.evidence["service.exchange"].read_bytes())
        exchanges = exchange["exchanges"]
        update = registered["parameters"]["workflow"]["after"]
        assert all(update not in exchange["request_utf8"] for exchange in exchanges[:2])
        assert update in exchanges[2]["request_utf8"]
        proof_path = outcome.evidence["supplement_observation"]
        original_proof = proof_path.read_bytes()
        saved = json.loads(outcome.evidence["conversation"].read_bytes())
        visible = QualityVisibleInput.model_validate(registered)
        try:
            for change in ({"saved": False}, {"supplement": {}}, {"task_id": "different-task"}):
                unsafe_proof = json.loads(original_proof)
                unsafe_proof["received"].update(change)
                proof_path.write_bytes(json.dumps(unsafe_proof).encode("utf-8"))
                score = quality_recovery.score(visible, acceptance_public_root, [saved], [exchange])
                assert score["quality_workflow_contract"]["late_arrival_verified"] is False
                assert score["goal_program_contract_passed"] is False
        finally:
            proof_path.write_bytes(original_proof)
    finally:
        if (packet / "freeze.json").is_file():
            original = acceptance_public_root / "review-context"
            for name in ("freeze.json", f"input/{task_id}.json", f"gold/{task_id}.json"):
                destination = original / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, destination)
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.parametrize(("slot", "attachment"), [(7, "target.txt"), (7, "missing.txt"), (1, "witness.txt")])
def test_qg5_late_supplement_requires_a_distinct_registered_attachment_in_slot_seven(slot, attachment):
    from tests.support.agent_acceptance.quality_recovery import supports
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    visible = QualityVisibleInput(task_id=f"QG5.formal.{slot}", adapter=f"qg5-{slot}",
        goal="Read a synthetic target and adjust it only on a subsequent explicit user update.",
        sources={"target.txt": QualitySource(text="fixture=F100\ntime=13:54\noptional_notice=unadopted\n"),
                 "witness.txt": QualitySource(text="Synthetic read-only witness.\n")},
        parameters={"workflow": {"subject": "fixture", "code": "F100", "before": "13:54", "after": "17:48",
            "filename": "target.txt", "optional": "notice"}})
    assert supports(visible)
    controlled = visible.model_copy(update={"parameters": {**visible.parameters,
        "supplement_arrival": {"timing": "after_target_read", "read_only_source": attachment}}})
    assert not supports(controlled)


def test_qg5_invoice_queue_uses_three_distinct_tasks_and_preserves_missing_original(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-queue-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, "QG5.formal.2", "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        assert_frozen_sources_match(packet / "input/QG5.formal.2.json", acceptance_public_root / "materials")
        contract = outcome.facts["quality_workflow_contract"]
        assert contract["three_distinct_task_ids"] is True
        assert contract["queue_order_and_authorization_preserved"] is True
        assert contract["missing_original_retained"] is True
        assert contract["last_source_read"] is True
        assert contract["subjects"] == ["F18", "F29", "F34"]
        assert contract["amounts"] == ["92.40", "15.60", "203.10"]
        assert outcome.facts["real_http_attempts"] == 0
        assert outcome.facts["independent_quality_samples_added"] == 0
    finally:
        if packet.exists():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG5.formal.2.json", "gold/QG5.formal.2.json"):
                destination = original / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, destination)
            shutil.rmtree(packet)

def test_qg5_parallel_private_recipients_use_actual_isolated_receivers(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-parallel-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, "QG5.formal.3", "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        contract = outcome.facts["quality_workflow_contract"]
        assert contract["distinct_thread_task_ids"] is True
        assert contract["sdk_wire_sources_isolated"] is True
        assert contract["exact_actual_receiver_bodies"] is True
        assert contract["actual_approval_subjects_match"] is True
        assert contract["codes"] == ["M-284", "W-691"]
        assert outcome.facts["real_supplier_http_attempts"] == 0
    finally:
        if packet.exists():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG5.formal.3.json", "gold/QG5.formal.3.json"):
                destination = original / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, destination)
            shutil.rmtree(packet)

def test_qg5_delivery_stop_retains_partial_effect_and_other_chat_finishes_send(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-stop-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, "QG5.formal.4", "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        assert_frozen_sources_match(packet / "input/QG5.formal.4.json", acceptance_public_root / "materials")
        contract = outcome.facts["quality_workflow_contract"]
        assert contract["codes"] == ["C-572", "P-918"]
        assert contract["partial_effect_preserved"] is True
        assert contract["b_continues_after_a_stop"] is True
        assert contract["separately_stopped"] is True
        assert contract["exact_actual_receiver_bodies"] is True
        assert contract["no_request_after_each_stop"] is True
        assert outcome.facts["real_supplier_http_attempts"] == 0
    finally:
        if packet.exists():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG5.formal.4.json", "gold/QG5.formal.4.json"):
                destination = original / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, destination)
            shutil.rmtree(packet)

def restock_reporting_input():
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    return QualityVisibleInput(task_id="QG5.formal.10", adapter="qg5-10",
        goal="Review the bay inventory and, separately, compare the archive sleeve request.",
        sources={"inventory-page1.txt": QualitySource(text="inventory=I-942\r\nbays-checked=11\r\n", encoding="utf-8-sig"),
                 "restock.txt": QualitySource(text="restock=R-371\r\nsleeves-available=13\r\nsleeves-requested=19\r\ncollection-confirmed=no\r\n")},
        parameters={"failure_subjects": {"inventory": "I-942", "restock": "R-371"}, "failure_reporting": {
            "inventory": "Report which bays this inventory page covers and which remain unverified.",
            "restock": "Report available and requested sleeves, their difference, and whether collection is confirmed.",
        }})


@pytest.mark.parametrize("frozen_reason, registration_error", [
    pytest.param(
        "The inventory phase includes a partial service failure. Verify its effects before the independent restock begins.",
        None, id="matching",
    ),
    pytest.param(None, "Missing declared domain output standards", id="missing"),
    pytest.param(
        "This template has no save/reopen or interrupted-service branch.",
        "Frozen per-task domain standard conflicts", id="legacy-reason-bound-to-phase-reporting",
    ),
])
def test_qg5_frozen_restock_reporting_registration(frozen_reason, registration_error):
    from tests.support.agent_acceptance import builtin_registry, freeze_quality
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_packet import PACKETS, QualityPacketRef, read_quality_packet

    packets = SOURCE / PACKETS
    packet = packets / ("qg5-restock-registration-" + uuid4().hex)
    assert not packet.exists()
    try:
        baseline = read_quality_packet(build_implementation_quality_packet(packet))
        rows = [task.model_dump(mode="json") for task in baseline.tasks]
        visible = restock_reporting_input()
        row = next(task for task in rows if task["task_id"] == visible.task_id)
        row.update(objective=visible.goal, authorization=visible.goal, allowed_paths=list(visible.sources))
        (SOURCE / row["input_path"]).write_text(visible.model_dump_json(), encoding="utf-8")
        (SOURCE / row["gold_path"]).write_text(json.dumps({
            "task_id": visible.task_id, "review_only": True, "inventory_bays_checked": 11,
            "restock_shortfall": 6, "collection_confirmed": False,
        }), encoding="utf-8")
        if frozen_reason is None:
            row["domain_standards"] = {}
        else:
            row["domain_standards"]["main_answer"]["dimensions"]["recovery_truth"]["reason"] = frozen_reason

        destination = packet / "freeze.json"
        destination.unlink()
        frozen = freeze_quality(rows, destination=destination, provenance=baseline.provenance,
            source_version=baseline.source_version, environment=baseline.environment, models=baseline.models,
            criteria_version=baseline.criteria_version, qualification_families=list(baseline.qualification_families),
            intended_use=baseline.intended_use)
        reference = QualityPacketRef(path=frozen, sha256=sha256(frozen.read_bytes()).hexdigest())
        if registration_error:
            with pytest.raises(ValueError, match=registration_error):
                builtin_registry(quality_packet=reference)
        else:
            case = builtin_registry(quality_packet=reference).cases["quality.qg5.formal.10"]
            assert case.formal_live
            assert case.output_standards is not None
            recovery = case.output_standards["main_answer"]["dimensions"]["recovery_truth"]
            assert recovery["applicable"] and recovery["critical"] and recovery["reason"] == frozen_reason
    finally:
        if packet.exists():
            assert packet.resolve().is_relative_to(packets.resolve())
            assert not packet.is_symlink() and not packet.is_junction()
            shutil.rmtree(packet)


@pytest.mark.parametrize("task_id, adapter, current_source, later_sources, parameters", [
    ("QG5.formal.2", "qg5-2", "F18.txt", ("F29.txt", "F34.txt"), {"queue_invoices": [
        {"code": "F18", "amount": "92.40"},
        {"code": "F29", "amount": "15.60"},
        {"code": "F34", "amount": "203.10"},
    ]}),
    ("QG5.formal.3", "qg5-3", "A/PAYLOAD.TXT", ("b/payload.txt",), {"parallel_subjects": {
        "a_subject": "museum", "a_code": "M-284",
        "b_subject": "warehouse", "b_code": "W-691",
    }}),
    ("QG5.formal.4", "qg5-4", "a/notes.txt", ("a/payload.txt", "b/payload.txt"), {"parallel_subjects": {
        "a_subject": "delivery", "a_code": "C-572",
        "b_subject": "purchase", "b_code": "P-918",
    }}),
    ("QG5.tuning.2", "qg5-tuning-2", "a/payload.txt", ("b/payload.txt",), {"parallel_subjects": {
        "a_subject": "greenhouse_plant", "a_code": "G-421",
        "b_subject": "greenhouse_equipment", "b_code": "T-807",
    }}),
])
def test_qg5_first_prompt_lists_only_current_source_encoding(task_id, adapter, current_source, later_sources, parameters):
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput, model_prompt

    visible = QualityVisibleInput(
        task_id=task_id, adapter=adapter, goal="Read the currently authorized material.",
        sources={current_source: QualitySource(text="Current original", encoding="utf-8-sig"),
                 **{name: QualitySource(text="Later original") for name in later_sources}},
        parameters=parameters,
    )

    prompt = model_prompt(visible)
    current = json.loads(prompt.split("\n", 1)[1])
    assert [name.casefold() for name in current["authorized_source_order"]] == [current_source.casefold()]
    assert current["source_encodings"] == {current_source: "utf-8-sig"}
    assert all(name not in prompt for name in later_sources)


def test_qg5_independent_restock_prepares_only_each_phases_questions(tmp_path):
    from tests.support.agent_acceptance import quality_recovery
    from tests.support.agent_acceptance.quality_runtime import model_prompt

    registered = restock_reporting_input()
    projected = quality_recovery.project_input(registered)
    assert projected.goal == registered.goal
    assert projected.sources == registered.sources

    current = json.loads(model_prompt(projected).split("\n", 1)[1])
    assert current["source_encodings"] == {"inventory-page1.txt": "utf-8-sig"}
    assert "which bays this inventory page covers" in current["goal"]
    assert "R-371" not in current["goal"] and "requested sleeves" not in current["goal"]
    assert "bays-checked=11" not in current["goal"]

    request = quality_recovery.manifest(projected, tmp_path, "deepseek")
    queued = request["quality_recovery"]["queue"][0]
    assert queued["scope"] == request["scope"]
    assert "restock.txt" in queued["prompt"] and "R-371" in queued["prompt"]
    assert "requested sleeves, their difference, and whether collection is confirmed" in queued["prompt"]
    assert "sleeves-available=13" not in queued["prompt"] and "I-942" not in queued["prompt"]

    recovery = quality_recovery.output_standard(projected)["main_answer"]["dimensions"]["recovery_truth"]
    assert recovery["applicable"] and recovery["critical"]
    assert recovery["reason"] == "The inventory phase includes a partial service failure. Verify its effects before the independent restock begins."
    legacy = projected.model_copy(update={"parameters": {"failure_subjects": projected.parameters["failure_subjects"]}})
    legacy_recovery = quality_recovery.output_standard(legacy)["main_answer"]["dimensions"]["recovery_truth"]
    assert legacy_recovery["reason"] == "This template has no save/reopen or interrupted-service branch."

    missing = registered.model_copy(update={"sources": {"inventory-page1.txt": registered.sources["inventory-page1.txt"]}})
    assert not quality_recovery.supports(missing)
    assert not quality_recovery.supports(quality_recovery.project_input(missing))


def test_qg5_scope_rejects_a_completed_restock_read_during_inventory(tmp_path):
    from tests.support.agent_acceptance import quality_recovery

    def read(action_id, name, output):
        return {"id": action_id, "kind": "read", "status": "completed", "effect": "completed",
            "prepared_parameters": {"path": str(tmp_path / "materials" / name)}, "output": output}

    next_task = {"id": "restock", "goal": "Read only restock", "scope": {},
        "approval": {}, "provider_config": {}}
    inventory = {"task": {"id": "inventory"}, "status": "failed", "service_failure": True,
        "error": "Service failure", "answer": None, "actions": [read("valid-read", "inventory-page1.txt", "inventory=I-942")],
        "request_attempts": []}
    restock = {"task": next_task, "status": "completed",
        "actions": [read("restock-read", "restock.txt", "restock=R-371")]}
    observation = {"queued": {"task": next_task}, "stop_events": [], "verified_before_next": True,
        "recovery": {"task_id": "inventory", "can_resume": True},
        "original_finished": {"saved": True, "task_id": "inventory", "turn": inventory}}

    def score_saved_turns():
        (tmp_path / "quality-failure-observation.json").write_text(json.dumps(observation), encoding="utf-8")
        (tmp_path / "quality-failure-original.json").write_text(json.dumps({"turns": [inventory]}), encoding="utf-8")
        return quality_recovery.score(restock_reporting_input(), tmp_path, [{"turns": [inventory, restock]}], [])

    complete = score_saved_turns()
    assert complete["tool_contract_passed"]
    assert complete["critical_contracts"]["K1"] == "passed"

    inventory["actions"].append(read("cross-read", "restock.txt", "restock=R-371"))
    crossed = score_saved_turns()
    assert not crossed["tool_contract_passed"]
    assert crossed["critical_contracts"]["K1"] == "failed"


@pytest.mark.parametrize("reporting", [False, True], ids=["legacy", "phase-reporting"])
def test_qg5_inventory_failure_retains_its_error_and_independent_restock_identity(acceptance_public_root, monkeypatch, reporting):
    from tests.support.agent_acceptance import quality_recovery
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-inventory-" + uuid4().hex)
    try:
        if reporting:
            registered = restock_reporting_input()
            original_projection = quality_recovery.project_input
            monkeypatch.setattr(quality_recovery, "project_input", lambda visible: original_projection(
                registered if visible.task_id == registered.task_id else visible))
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(quality_recovery.exercise_recovery(reference, "QG5.formal.10", "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        assert outcome.product_status == "failed"
        assert_frozen_sources_match(packet / "input/QG5.formal.10.json", acceptance_public_root / "materials")
        assert outcome.facts["real_http_attempts"] == 0
        contract = outcome.facts["quality_workflow_contract"]
        assert contract["subjects"] == (["I-942", "R-371"] if reporting else ["I-837", "R-246"])
        assert contract["raw_failure_not_relabelled_stop"] is True
        assert contract["independent_queue_identity_preserved"] is True
        assert contract["unknown_effects_verified_before_next"] is True
        assert contract["original_not_retried"] is True
        assert contract["original_failed_read_count"] == 1
        assert contract["next_completed"] and contract["next_source_read"]
        assert outcome.critical_contracts["K4"] == "unknown"
        bodies = [item["request_utf8"] for item in json.loads(outcome.evidence["service.exchange"].read_bytes())["exchanges"]]
        assert len(bodies) == 4
        review_guard = json.loads((packet / "gold/QG5.formal.10.json").read_bytes())["private_review_guard"]
        assert all(review_guard not in body for body in bodies)
        assert all("restock.txt" not in body and contract["subjects"][1] not in body for body in bodies[:2])
        if reporting:
            assert "bays-checked=11" not in bodies[0] and "bays-checked=11" in bodies[1]
            assert all("requested sleeves" not in body for body in bodies[:2])
            assert "requested sleeves, their difference, and whether collection is confirmed" in bodies[2]
            assert all("sleeves-available=13" not in body for body in bodies[:3])
            assert "sleeves-available=13" in bodies[3]
    finally:
        if packet.exists():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG5.formal.10.json", "gold/QG5.formal.10.json"):
                destination = original / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, destination)
            shutil.rmtree(packet)
@pytest.mark.parametrize("registered_name", ["absent", "exact", "other-case"])
@pytest.mark.parametrize("task_id, adapter, legacy_source, expected_sources", [
    ("QG5.formal.2", "qg5-2", "F18金额92.40；F29金额15.60；F29原件缺失；F34金额203.10",
     {"F18.txt": b"invoice=F18\namount=92.40\n", "F34.txt": b"invoice=F34\namount=203.10\n"}),
    ("QG5.formal.4", "qg5-4", "配送A箱号C-572；采购B采购单P-918",
     {"a/notes.txt": b"delivery=pending\n", "a/payload.txt": b"chat=A\nsubject=delivery\ncode=C-572\n",
      "b/payload.txt": b"chat=B\nsubject=purchase\ncode=P-918\n"}),
    ("QG5.formal.10", "qg5-10", "库存I-837的部分故障；继续独立补货单R-246",
     {"inventory-page1.txt": b"inventory=I-837\npage=1\n", "restock.txt": b"restock=R-246\nstatus=unverified\n"}),
])
def test_qg5_legacy_preparation_derives_only_missing_materials(
    tmp_path, task_id, adapter, legacy_source, expected_sources, registered_name,
):
    from tests.support.agent_acceptance import quality_recovery
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    materials = tmp_path / "materials"
    materials.mkdir()
    expected = {"source.txt": legacy_source.encode("utf-8"), **expected_sources}
    sources = {"source.txt": QualitySource(text=legacy_source)}
    if registered_name != "absent":
        name = next(iter(expected_sources))
        original = expected.pop(name) + b"registered-reference=approved\n"
        saved_name = name.swapcase() if registered_name == "other-case" else name
        sources[saved_name] = QualitySource(text=original.decode("utf-8"))
        expected[saved_name] = original
    for name, source in sources.items():
        path = materials / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.text.encode(source.encoding))
    visible = QualityVisibleInput(task_id=task_id, adapter=adapter,
        goal="Use the separately authorized originals.", sources=sources)

    quality_recovery.setup(visible, tmp_path, "deepseek")

    saved = [path for path in materials.rglob("*") if path.is_file() and path.name != "AGENTS.md"]
    assert len(saved) == len(expected)
    assert {path.relative_to(materials).as_posix().casefold(): path.read_bytes() for path in saved} == {
        name.casefold(): original for name, original in expected.items()
    }


@pytest.mark.parametrize("registered_path", [
    "b/payload.txt",
    pytest.param("B/PAYLOAD.TXT", marks=pytest.mark.skipif(os.name != "nt", reason="Windows path aliases")),
])
def test_qg5_parallel_stop_uses_the_frozen_bytes_for_the_expected_send(tmp_path: Path, registered_path: str) -> None:
    from tests.support.agent_acceptance import quality_recovery, quality_recovery_parallel
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    source = QualitySource(text="chat=B\nsubject=purchase\ncode=P-918\n", encoding="utf-8-sig")
    materials = tmp_path / "materials"
    purchase = materials / registered_path
    purchase.parent.mkdir(parents=True)
    original = source.text.encode(source.encoding)
    purchase.write_bytes(original)
    visible = QualityVisibleInput(task_id="QG5.formal.4", adapter="qg5-4",
        goal="Send only the exact registered purchase source after approval.", sources={registered_path: source},
        parameters={"parallel_subjects": {"a_subject": "delivery", "a_code": "C-572", "b_subject": "purchase", "b_code": "P-918"}})

    quality_recovery.setup(visible, tmp_path, "deepseek")

    assert purchase.read_bytes() == original == quality_recovery_parallel.payload(visible, "b")
    info = quality_recovery.manifest(visible, tmp_path, "deepseek")["quality_recovery"]["parallel"]["b"]
    assert info["source"] == str(purchase.resolve())
    assert info["scope"]["root"] == str(purchase.parent.resolve())
    assert info["source_sha256"] == sha256(original).hexdigest()
    assert quality_recovery.project_input(visible).sources[registered_path] == source


def test_qg5_parallel_stop_edit_keeps_the_rest_of_the_frozen_note(tmp_path: Path) -> None:
    from tests.support.agent_acceptance import quality_recovery, quality_recovery_parallel
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    note = QualitySource(text="delivery=pending\r\nkeep=archive\r\n", encoding="utf-8-sig")
    original = tmp_path / "materials/a/notes.txt"
    original.parent.mkdir(parents=True)
    original.write_bytes(note.text.encode(note.encoding))
    visible = QualityVisibleInput(task_id="QG5.formal.4", adapter="qg5-4",
        goal="Replace only pending with the registered delivery code.", sources={"a/notes.txt": note},
        parameters={"parallel_subjects": {"a_subject": "delivery", "a_code": "C-572", "b_subject": "purchase", "b_code": "P-918"}})
    assert quality_recovery.project_input(visible).sources["a/notes.txt"] == note

    updated = note.text.replace("pending", "C-572", 1)
    original.write_bytes(updated.encode("utf-8-sig"))
    assert quality_recovery_parallel.stop_note_preserved(visible, tmp_path, "C-572")

    original.write_bytes(updated.replace("\r\n", "\n").encode("utf-8"))
    assert not quality_recovery_parallel.stop_note_preserved(visible, tmp_path, "C-572")


@pytest.mark.parametrize("task_id,codes", [("QG5.formal.7", ["H-462"]), ("QG5.tuning.1", ["V-125", "S-663"]), ("QG5.tuning.2", ["G-421", "T-807"])])
def test_qg5_other_substantive_families_keep_their_actual_event_contracts(acceptance_public_root, task_id, codes):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-distinct-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, task_id, "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        contract = outcome.facts["quality_workflow_contract"]
        if task_id == "QG5.tuning.1":
            assert contract["queued_actual_subject"] == codes[1]
            assert contract["queue_follows_completed_original"] is True
            assert contract["queued_distinct_identity_scope"] is True
        elif task_id == "QG5.tuning.2":
            assert contract["codes"] == codes
            assert contract["separately_stopped"] is True
            assert contract["no_request_after_each_stop"] is True
        else:
            assert contract["supplement_applied_once"] is True
            assert contract["no_future_update_in_first_input"] is True
        assert outcome.facts["independent_quality_samples_added"] == 0
    finally:
        if packet.exists():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/"+task_id+".json", "gold/"+task_id+".json"):
                destination = original / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, destination)
            shutil.rmtree(packet)

def test_qg5_parallel_actual_tasks_retain_the_two_canonical_scope_roots(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    from tests.support.agent_acceptance.quality_runtime import product_permission_status
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("qg5-roots-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, "QG5.formal.3", "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        records = [json.loads(outcome.evidence[key + ".conversation"].read_bytes()) for key in ("a", "b")]
        actual = product_permission_status("quality.qg5.formal.3", acceptance_public_root, "deepseek", records)
        assert actual["status"] == "passed", actual
        assert len(actual["evaluated_task_ids"]) == 2
    finally:
        if packet.exists():
            original = acceptance_public_root / "review-context"
            original.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG5.formal.3.json", "gold/QG5.formal.3.json"):
                destination = original / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, destination)
            shutil.rmtree(packet)
