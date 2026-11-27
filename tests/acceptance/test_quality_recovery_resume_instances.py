"""Canonical QG5 recovery tasks retain original local product identities."""
import asyncio
import json
from uuid import uuid4
import shutil

import pytest


@pytest.mark.parametrize(("evidence", "handoff_status", "replayed"), [
    ("complete", "passed", False),
    ("full_output", "passed", False),
    ("failed_read", "failed", False),
    ("missing_original", "unknown", False),
    ("missing_output", "unknown", False),
    ("wrong_original_bytes", "failed", False),
    ("wrong_output", "failed", False),
    ("wrong_prepared_path", "failed", False),
    ("replayed_check", "passed", True),
    ("replayed_check_with_failed_status", "passed", True),
])
def test_saved_theatre_recovery_scores_the_original_handoff_read(tmp_path, evidence, handoff_status, replayed):
    from tests.support.agent_acceptance.quality_recovery_resume import score
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    visible = QualityVisibleInput(task_id="QG5.formal.5", adapter="qg5-5", goal="Resume the saved theatre inspection.",
        sources={}, parameters={"resume_facts": {
            "subject": "剧院灯光巡检", "target": "inspection.txt", "before": "A台12盏\n", "after": "A台15盏\n",
            "remaining": "B台的交接说明仍待核。",
        }})
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "inspection.txt").write_bytes("A台15盏\n".encode())
    handoff = materials / "handoff.txt"
    handoff_body = "剧院灯光巡检：B台的交接说明仍待核。用户接受仍待定。\n"
    if evidence != "missing_original":
        handoff.write_bytes((b"\xef\xbb\xbf" if evidence == "wrong_original_bytes" else b"") + handoff_body.encode())

    task = {"id": "theatre", "scope": {"root": str(materials)}, "approval": {}, "requests": 4}
    check = {"kind": "check", "status": "completed", "effect": "completed", "exit_code": 0, "cleanup_complete": True}
    original = {"status": "failed", "task": task, "request_owner_id": "theatre", "actions": [check]}
    (tmp_path / "original-before.json").write_text(json.dumps({"turns": [original]}), encoding="utf-8")
    (tmp_path / "recovery-observation.json").write_text('{"can_resume":true,"open_http_attempts":0}', encoding="utf-8")
    read = {"id": "resume-read", "kind": "read", "target": "handoff.txt", "status": "completed", "effect": "completed",
            "prepared_parameters": {"path": str(handoff)}, "output": "[handoff.txt:1] " + handoff_body.rstrip()}
    if evidence == "full_output":
        receipt = tmp_path / "records" / "resume-read.txt"
        receipt.parent.mkdir()
        receipt.write_text(read.pop("output"), encoding="utf-8")
        read["full_output_path"] = str(receipt)
    if evidence == "failed_read":
        read.update(status="failed", effect="not_started", output=None)
    if evidence == "missing_output":
        read.pop("output")
    if evidence == "wrong_output":
        read["output"] = "[handoff.txt:1] B台已完成。"
    if evidence == "wrong_prepared_path":
        read["prepared_parameters"] = {"path": str(materials / "different.txt")}
    final_actions = [read]
    if replayed:
        final_actions.append({**check, "status": "failed" if evidence == "replayed_check_with_failed_status" else "completed"})
    final = {"status": "completed", "task": {**task, "requests": 6}, "request_owner_id": "theatre", "actions": final_actions}

    result = score(visible, tmp_path, [{"turns": [original, final]}], [])

    assert result["passed"] is (handoff_status == "passed" and not replayed)
    assert result["quality_recovery_contract"]["continuation_handoff_status"] == handoff_status
    assert result["quality_recovery_contract"]["completed_effects_not_replayed"] is not replayed
    if handoff_status != "passed" or replayed:
        assert result["fact_contract_status"] == ("failed" if replayed else handoff_status)
    assert result["critical_contracts"]["K3"] == ("failed" if replayed else "passed")
    assert result["critical_contracts"]["K4"] == "unknown"


def test_saved_theatre_mock_reads_the_handoff_without_inventing_its_content(tmp_path):
    from tests.support.agent_acceptance.quality_recovery_resume import replies
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    visible = QualityVisibleInput(task_id="QG5.formal.5", adapter="qg5-5", goal="Resume the saved theatre inspection.",
        sources={}, parameters={"resume_facts": {
            "subject": "剧院灯光巡检", "target": "inspection.txt", "before": "A台12盏\n", "after": "A台15盏\n",
            "remaining": "仅查东侧关灯表的签核状态。",
        }})

    read, answer = replies(visible, tmp_path, "deepseek", "reopen")

    assert (read.tool, read.arguments) == ("read_text", {"path": "handoff.txt"})
    assert "交接记录" in answer.text and "B台" not in answer.text


def test_saved_theatre_inspection_keeps_real_check_and_continues_only_handoff(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        result = asyncio.run(exercise_recovery(reference, "QG5.formal.5", "deepseek", root=acceptance_public_root))
        assert result.product_status == "passed", result.facts
        proof = result.facts["quality_recovery_contract"]
        assert proof["same_task_and_scope"] is True
        assert proof["check_completed_before_reopen"] is True
        assert proof["completed_effects_not_replayed"] is True
        assert proof["open_http_attempts"] == 0
        assert proof["requests_after"] > proof["requests_before"]
        assert (acceptance_public_root / "materials/inspection.txt").read_text(encoding="utf-8") == "A台15盏\nB台交接说明：待核查\n"
        assert result.semantic_status == "not_measured"
        assert result.facts["real_http_attempts"] == result.facts["independent_quality_sample_increment"] == 0
    finally:
        if packet.exists(): shutil.rmtree(packet)



def test_archive_local_save_exit_requires_actual_verification_before_resume(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        result = asyncio.run(exercise_recovery(reference, "QG5.formal.6", "deepseek", root=acceptance_public_root))
        assert result.product_status == "passed", result.facts
        proof = result.facts["quality_recovery_contract"]
        assert proof["planned_local_exit"] == 71
        assert proof["reliable_and_pending_differ"] is True
        assert proof["open_http_attempts"] == proof["blocked_resume_http_attempts"] == 0
        assert proof["imported_unknown_blocks_resume"] is True
        assert proof["verified_effect"] == "completed"
        assert proof["same_task_and_scope"] is True
        assert proof["completed_effects_not_replayed"] is True
        assert result.facts["supplier_mock_fault"] is False
        assert (acceptance_public_root / "materials/east-ledger.txt").read_text(encoding="utf-8") == "东库账页=已改\n"
        assert result.semantic_status == "not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)



def test_stopped_exhibition_legacy_import_has_headroom_and_sends_nothing(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        result = asyncio.run(exercise_recovery(reference, "QG5.formal.9", "deepseek", root=acceptance_public_root))
        assert result.product_status == "passed", result.facts
        proof = result.facts["quality_recovery_contract"]
        assert proof["original_http_attempts"] == proof["requests_before"] == proof["requests_after"]
        assert 0 < proof["requests_after"] < proof["legacy_projection_request_limit"]
        assert proof["accepted_stop_binding"] is True
        assert proof["stopped_status_retained"] is True
        assert proof["open_import_and_blocked_resume_http_attempts"] == 0
        assert proof["same_task_scope_recipient"] is True
        assert proof["legacy_original_and_backup_preserved"] is True
        assert proof["legacy_missing_fields"]
        assert result.semantic_status == "not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)



def test_cargo_parent_reopen_keeps_one_supplement_and_side_child_original(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        result = asyncio.run(exercise_recovery(reference, "QG5.formal.8", "deepseek", root=acceptance_public_root))
        assert result.product_status == "passed", result.facts
        proof = result.facts["quality_recovery_contract"]
        assert proof["same_task_and_scope"] is True
        assert proof["applied_supplements"] == 1
        assert proof["supplement_id_and_applied_request_retained"] is True
        assert proof["child_scope_only_side"] is True
        assert proof["child_record_unchanged"] is True
        assert proof["child_cannot_resume_as_parent"] is True
        assert proof["completed_effects_not_replayed"] is True
        assert proof["child_requests"] == 2
        assert proof["open_http_attempts"] == 0
        assert (acceptance_public_root / "materials/cargo-label.txt").read_text(encoding="utf-8") == "货站F箱标签=更正版\n"
        assert result.semantic_status == "not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)


@pytest.fixture
def delegated_recovery_variant():
    from tests.support.agent_acceptance import quality_recovery_resume as recovery
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    facts = recovery.ResumeFacts(subject="Cabinet A latch", target="latch.txt",
        before="latch=pending\n", after="latch=secured\n", remaining="The cabinet alignment note is still pending.")
    side = QualitySource(text="Record Cabinet A alignment after the hinge inspection.\n", encoding="utf-8-sig")
    target = QualitySource(text=facts.before.replace("\n", "\r\n"), encoding="utf-8-sig")
    changed = QualityVisibleInput(task_id="QG5.formal.8", goal="Inspect Cabinet A", adapter="qg5-8",
        parameters={"resume_facts": facts.model_dump()}, sources={facts.target: target, "side/source.txt": side})
    return changed, facts, side


def test_delegated_recovery_projects_only_frozen_variant_facts(tmp_path, delegated_recovery_variant):
    from tests.support.agent_acceptance import quality_recovery_resume as recovery
    from tests.support.agent_acceptance.quality_runtime import model_prompt

    changed, facts, side = delegated_recovery_variant
    frozen = recovery.project_input(changed)
    request = recovery.manifest(frozen, tmp_path, "deepseek")
    current = model_prompt(frozen)
    supplement = request["quality_recovery"]["supplement"]
    assert "Cabinet A latch" in current and "latch.txt" in current
    first = json.loads(current.split("\n", 1)[1])
    assert all(value not in first["goal"] for value in ("latch=secured", facts.remaining, "side/source.txt", "委派", "F箱"))
    assert first["authorized_source_order"] == ["latch.txt"]
    assert all(value in supplement for value in (facts.subject, facts.target, facts.before, facts.after, "side/source.txt"))
    assert "F箱" not in supplement
    assert frozen.sources["side/source.txt"] == side
    assert frozen.sources[facts.target] == changed.sources[facts.target]
    assert "source.txt" in request["quality_recovery"]["child_goal"]
    assert "F箱" not in request["quality_recovery"]["child_goal"]
    work_replies = recovery.replies(frozen, tmp_path, "deepseek")
    assert side.text.strip() in work_replies[5].text
    assert all(value not in work_replies[5].text for value in ("F箱", facts.after.strip()))
    assert facts.remaining in recovery.replies(frozen, tmp_path, "deepseek", "reopen")[-1].text
    assert not recovery.supports(changed.model_copy(update={"sources": {}}))


def test_delegated_recovery_variant_reads_exact_frozen_side_and_handoff(acceptance_public_root, delegated_recovery_variant):
    from myrk.permissions import ApprovalSettings
    from tests.support.agent_acceptance import quality_recovery_resume as recovery
    from tests.support.agent_acceptance.core import CaseContext

    changed, item, side = delegated_recovery_variant
    frozen = recovery.project_input(changed)
    root = acceptance_public_root
    materials = root / "materials"
    (materials / "side").mkdir(parents=True)
    for name, source in frozen.sources.items():
        (materials / name).write_bytes(source.text.encode(source.encoding))
    recovery.setup(frozen, root, "deepseek")
    request = recovery.manifest(frozen, root, "deepseek")
    records = root / "records"
    settings = records / "settings" / "permissions.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(ApprovalSettings.model_validate({
        "mode": "request_approval", "access": {"writable_roots": [str(materials)]},
    }).model_dump_json(), encoding="utf-8")
    context = CaseContext(root, "deepseek", uuid4().hex, "quality.qg5.formal.8")
    result = asyncio.run(recovery.exercise_delegated(context, frozen, request, records))
    proof = result.facts["quality_recovery_contract"]
    assert result.product_status == "passed", result.facts
    assert proof["supplement_precedes_exact_edit_and_child"] and proof["edit_bound_to_frozen_target"]
    assert proof["child_scope_only_side"] and proof["child_read_matches_frozen_side"]
    assert proof["parent_read_matches_remaining_handoff"]
    assert proof["completed_effects_not_replayed"] and proof["child_record_unchanged"]
    assert proof["open_http_attempts"] == 0
    assert result.facts["real_http_attempts"] == result.facts["independent_quality_sample_increment"] == 0
    assert (materials / item.target).read_bytes() == item.after.replace("\n", "\r\n").encode("utf-8-sig")
    assert (materials / "side/source.txt").read_bytes() == side.text.encode(side.encoding)

    work, reopened = (json.loads((root / name / "exchange.json").read_bytes()) for name in ("initial", "reopened"))
    first_request = json.loads(work["exchanges"][0]["request_utf8"])
    first = "\n".join(message["content"] for message in first_request["messages"] if message["role"] == "user")
    assert all(value not in first for value in (item.after.strip(), side.text.strip(), item.remaining, "委派"))
    parent = json.loads(result.evidence["conversation"].read_bytes())
    parent["turns"][-1]["actions"][0]["output"] = "Another family's handoff."
    changed_handoff = recovery.score(frozen, root, [parent], [work, reopened])
    assert changed_handoff["passed"] is False
    assert changed_handoff["quality_recovery_contract"]["parent_read_matches_remaining_handoff"] is False

    alternate_child = root / "outside-records-child.json"
    alternate_child.write_bytes(result.evidence["child.conversation"].read_bytes())
    for path in (result.evidence["original"], root / "recovery-observation.json"):
        original = json.loads(path.read_bytes())
        if "turns" in original:
            original["turns"][0]["delegations"][0]["record_path"] = str(alternate_child)
        else:
            original["child_path"] = str(alternate_child)
        path.write_text(json.dumps(original), encoding="utf-8")
    parent = json.loads(result.evidence["conversation"].read_bytes())
    parent["turns"][0]["delegations"][0]["record_path"] = str(alternate_child)
    unbound_child = recovery.score(frozen, root, [parent], [work, reopened])
    assert unbound_child["passed"] is False
    assert unbound_child["quality_recovery_contract"]["child_record_unchanged"] is False



def test_missing_product_records_leave_recovery_contracts_unknown_after_real_positive_run(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE, source_material_path
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    from tests.support.agent_acceptance.quality_recovery_resume import score
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        actual = asyncio.run(exercise_recovery(reference, "QG5.formal.5", "deepseek", root=acceptance_public_root))
        assert actual.product_status == "passed", actual.facts
        task = next(item for item in read_quality_packet(reference).tasks if item.task_id == "QG5.formal.5")
        visible = QualityVisibleInput.model_validate_json(source_material_path(task.input_path).read_bytes())
        facts = score(visible, acceptance_public_root, [], [])
        assert facts["passed"] is False
        assert facts["fact_contract_status"] == "unknown"
        assert facts["missing_evidence"]
        assert set(facts["critical_contracts"].values()) == {"unknown"}
        assert actual.evidence["conversation"].is_file()
    finally:
        if packet.exists(): shutil.rmtree(packet)


@pytest.mark.parametrize(
    ("count", "accepted_task", "imported_status", "reopened_count", "original_limit", "projected_limit", "passed", "stop_preserved"),
    [
        (6, "original", "cancelled", 0, 24, 24, True, True),
        (7, "original", "cancelled", 0, 24, 24, True, True),
        (8, "original", "cancelled", 0, None, 24, True, True),
        (7, "another", "cancelled", 0, 24, 24, False, False),
        (7, "original", "running", 0, 24, 24, False, False),
        (7, "original", "cancelled", 1, 24, 24, False, False),
        (7, "original", "cancelled", 0, None, 999, False, True),
    ],
)
def test_stopped_score_binds_observed_requests_and_the_accepted_original_stop(
    tmp_path, count, accepted_task, imported_status, reopened_count, original_limit, projected_limit, passed, stop_preserved,
):
    from hashlib import sha256
    from tests.support.agent_acceptance.quality_recovery_resume import score_stopped
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    task = {"id": "original", "scope": {"recipients": ["deepseek"]}, "approval": {},
            "requests": count, "request_limit": original_limit}
    original = {"turns": [{"status": "cancelled", "task": task}]}
    legacy_task = {**task, "request_limit": projected_limit}
    imported_task = {**legacy_task, "request_policy": "shared", "total_requests": count}
    imported = {"turns": [{"status": imported_status, "task": imported_task}]}
    (tmp_path / "original-before.json").write_text(json.dumps(original), encoding="utf-8")
    legacy = json.dumps({"version": 5, "turns": [{"status": "cancelled", "task": legacy_task}]}).encode()
    (tmp_path / "legacy-source-v5.json").write_bytes(legacy)
    imported_path = tmp_path / "imported.json"
    imported_path.write_text(json.dumps(imported), encoding="utf-8")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups/imported-v5-probe.json").write_bytes(legacy)
    requests = [{"sdk_input_id": f"work-{number}"} for number in range(count)]
    observation = {
        "stop_sdk_input_id": requests[-1]["sdk_input_id"], "stop_accepted_task_id": accepted_task,
        "imported_path": str(imported_path), "legacy_sha256": sha256(legacy).hexdigest(),
        "legacy_missing_fields": [], "original_http_attempts": count, "reopened_http_attempts": reopened_count,
        "resume_blocked_reason": "不能续办", "legacy_projection_request_limit": projected_limit,
    }
    (tmp_path / "recovery-observation.json").write_text(json.dumps(observation), encoding="utf-8")
    visible = QualityVisibleInput(task_id="QG5.formal.9", goal="Read only", adapter="qg5-9", sources={})
    result = score_stopped(visible, tmp_path, [original, imported],
        [{"exchanges": requests}, {"exchanges": [{"sdk_input_id": "late"}] * reopened_count}])
    assert result["passed"] is passed
    assert result["critical_contracts"]["K3"] == ("passed" if stop_preserved else "failed")
