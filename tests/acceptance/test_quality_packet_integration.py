"""Canonical quality execution uses the same public scheduler and native product."""

import asyncio
import json
import pytest
from pathlib import Path

from tests.support.agent_acceptance import AcceptancePlan, run_plan
from tests.acceptance.test_frozen_execution_control import execution, registry_for_product


def test_stage_selection_keeps_original_denominator_and_does_not_run_unselected_tasks(acceptance_public_root):
    rows = (execution("quality:tuning", scenario_id="QG1.tuning.1", stage="quality"),
        execution("quality:formal", scenario_id="QG1.formal.1", stage="quality", repeat=2))
    result = asyncio.run(run_plan(AcceptancePlan(executions=rows), root=acceptance_public_root,
        registry=registry_for_product(), execution_ids=("quality:tuning",)))
    assert [row.execution_id for row in result.cases] == ["quality:tuning", "quality:formal"]
    assert [row.product_status for row in result.cases] == ["passed", "not_executed"]
    assert result.cases[1].facts["reason"] == "stage not selected; remains eligible"
    saved = json.loads(result.result_path.read_bytes())
    assert saved["planned_denominator"] == 2
    assert len(saved["planned_executions"]) == 2
    assert not result.cases[1].evidence


def test_canonical_registry_rebuilds_frozen_quality_members_without_caller_bindings():
    from hashlib import sha256
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance import builtin_registry, freeze_quality, quality_plan, FrozenParentBudget
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_packet import QualityPacketRef

    root = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("synthetic-" + uuid4().hex)
    root.mkdir(parents=True)
    try:
        tasks = json.loads((SOURCE / "tests/acceptance/materials/quality/synthetic.json").read_bytes())
        for task in tasks:
            for kind in ("input", "gold"):
                path = root / kind / (task["task_id"] + ".txt")
                path.parent.mkdir(exist_ok=True)
                path.write_text(task.pop(kind), encoding="utf-8")
                task[kind + "_path"] = path
            task["prior_uses"] = ["implementation"]
        frozen = freeze_quality(tasks, destination=root / "freeze.json", provenance="synthetic",
            source_version="synthetic canonical packet experiment", environment={"kind": "synthetic"},
            models={"deepseek": {"model": "deepseek-flash"}, "google": {"model": "gemini-2.5-flash"}},
            criteria_version="quality-packet-control-v1", qualification_families=[], intended_use="implementation_regression")
        reference = QualityPacketRef(path=frozen, sha256=sha256(frozen.read_bytes()).hexdigest())
        registry = builtin_registry(quality_packet=reference)
        parent = FrozenParentBudget(parent_id="synthetic-canonical-" + root.name, accounting_root=root / "prepared",
            request_cap=2520, phase_microusd={"screening": 0, "quality": 39076920, "judge": 0}, reserve_microusd=0, profile="synthetic")
        plan = quality_plan(frozen, registry=registry, parent_budget=parent, mode="offline")
        assert plan.quality_packet == reference
        assert len(plan.executions) == 84
        assert len({row.case_id for row in plan.executions}) == 72
        assert len({row.scenario_id for row in plan.executions}) == 72
        assert registry.quality_packet == reference
        rebuilt = builtin_registry(quality_packet=reference)
        assert [row.case_id for row, _ in rebuilt.select(plan)] == [row.case_id for row in plan.executions]
        assert all((root / "gold" / (task["task_id"] + ".txt")).resolve() in
            {path.resolve() for path in registry.cases["quality." + task["task_id"].lower()].bindings} for task in tasks)
    finally:
        shutil.rmtree(root)


def test_implementation_packet_contains_real_qg1_qg2_sources_and_explicit_used_materials():
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    root = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(root)
        value = read_quality_packet(reference)
        assert len(value.tasks) == 72
        assert value.intended_use == "implementation_regression"
        assert all(task.prior_uses == ("implementation",) for task in value.tasks)
        inputs = {task.task_id: json.loads((SOURCE / task.input_path).read_bytes()) for task in value.tasks}
        assert inputs["QG1.formal.2"]["sources"]["ticket.txt"]["text"] != inputs["QG1.formal.2"]["sources"]["bulletin.txt"]["text"]
        assert inputs["QG1.formal.4"]["sources"]["encoding.txt"]["encoding"] == "utf-8-sig"
        assert len(inputs["QG1.formal.5"]["sources"]["archive.txt"]["text"]) > 4096
        assert inputs["QG2.formal.2"]["parameters"]["edits"] == [
            {"path": "manifest.txt", "old_text": "version=3", "new_text": "version=4"},
            {"path": "release.txt", "old_text": "release 3", "new_text": "release 4"}]
        assert inputs["QG2.formal.3"]["sources"]["amounts.csv"]["text"] == "item,amount\nwalnut,1.25\nmap,2.40\nrefund,-0.15\n"
        assert inputs["QG2.formal.4"]["sources"]["program.py"]["text"] == "def total(items):\n    return sum(items) - 1\n"
        assert len({task.objective for task in value.tasks}) == len({task.material_family_id for task in value.tasks}) == 72
    finally:
        if root.exists(): shutil.rmtree(root)




def test_canonical_file_instance_applies_both_real_version_changes(acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_files import exercise_files

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_files(reference, "QG2.formal.2", "deepseek", root=acceptance_public_root))
        assert outcome.product_status == outcome.tool_status == "passed", outcome.facts
        assert (acceptance_public_root / "materials/manifest.txt").read_bytes() == b"package=cedar\nversion=4\n"
        assert (acceptance_public_root / "materials/release.txt").read_bytes() == b"cedar release 4\nstable=true\n"
        assert outcome.facts["quality_native"]["actual_sources_read"] == ["manifest.txt", "release.txt"]
        assert outcome.facts["real_http_attempts"] == 0
        assert outcome.semantic_status == "not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_canonical_decimal_instance_uses_real_extension_and_independent_check(acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_files import exercise_files

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_files(reference, "QG2.formal.3", "deepseek", root=acceptance_public_root))
        assert outcome.product_status == outcome.tool_status == "passed", outcome.facts
        assert outcome.facts["quality_native"]["calculation_sum"] == "3.50"
        assert outcome.facts["quality_native"]["check_passed"] is True
        checks = [row for row in outcome.facts["actions"] if row["kind"] == "check"]
        assert len(checks) == 1 and checks[0]["exit_code"] == 0 and checks[0]["cleanup_complete"] is True
    finally:
        if packet.exists(): shutil.rmtree(packet)




@pytest.mark.parametrize(("fragment", "evidence_change"), [
    pytest.param("Linden Ferry partial source explanation retained.", None, id="original-wording"),
    pytest.param("A different fragment arrived before the break.", None, id="different-wording"),
    pytest.param("The original fragment is present.", "empty fragment", id="empty-saved-fragment"),
    pytest.param("The original fragment is present.", "wrong hash", id="wrong-snapshot-hash"),
])
def test_canonical_interrupted_reading_retains_fragment_and_resumes_original_task(
    acceptance_public_root, monkeypatch, fragment, evidence_change,
):
    from dataclasses import replace
    from hashlib import sha256
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance import quality_files
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    canned = quality_files.replies

    def replies(*args, **kwargs):
        return [replace(reply, text=fragment) if reply.interrupt else reply for reply in canned(*args, **kwargs)]

    monkeypatch.setattr(quality_files, "replies", replies)
    if evidence_change:
        original_score = quality_files.score

        def score(visible, root, records, exchanges, **kwargs):
            path = root / "interruption-observation.json"
            observation = json.loads(path.read_bytes())
            if evidence_change == "empty fragment":
                original = root / "interrupted.original.json"
                saved = json.loads(original.read_bytes())
                saved["turns"][0]["answer"] = ""
                original.write_text(json.dumps(saved), encoding="utf-8")
                records[0]["turns"][0]["answer"] = ""
                observation["original_sha256"] = sha256(original.read_bytes()).hexdigest()
            else:
                observation["original_sha256"] = "0" * 64
            path.write_text(json.dumps(observation), encoding="utf-8")
            return original_score(visible, root, records, exchanges, **kwargs)

        monkeypatch.setattr(quality_files, "score", score)
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(quality_files.exercise_files(reference, "QG1.formal.10", "deepseek", root=acceptance_public_root))
        saved = json.loads(outcome.evidence["conversation"].read_bytes())
        assert len(saved["turns"]) == 2
        first, second = saved["turns"]
        assert first["status"] == "failed" and first["service_failure"] and first["answer"] == fragment
        assert second["status"] == "completed" and second["task"]["id"] == first["task"]["id"]
        assert second["task"]["requests"] > first["task"]["requests"]
        assert second["actions"] == []
        assert outcome.facts["quality_native"]["interruption_preserved"] is (evidence_change is None)
        assert outcome.facts["quality_native"]["open_sends"] == 0
        assert outcome.product_status == ("passed" if evidence_change is None else "failed")
    finally:
        if packet.exists(): shutil.rmtree(packet)






def test_canonical_calculation_failure_precedes_the_authorized_alternative_read(acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_files import exercise_files

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_files(reference, "QG2.formal.9", "deepseek", root=acceptance_public_root))
        actions = outcome.facts["actions"]
        failure = next(row for row in actions if row["kind"] == "calculate" and row["status"] == "failed")
        alternative = next(row for row in actions if row["kind"] == "read" and row["target"] == "corrected.csv")
        assert failure["finished_at"] <= alternative["started_at"]
        assert outcome.product_status == "passed" and outcome.facts["quality_native"]["calculation_sum"] == "3.50"
        assert outcome.facts["quality_native"]["original_failure_retained"] is True
    finally:
        if packet.exists(): shutil.rmtree(packet)




@pytest.mark.parametrize(("task_id", "answer_tokens"), [
    ("QG1.formal.1", ["18:30", "notice.txt:1"]),
    ("QG1.formal.2", ["28.00", "31.50", "March", "April"]),
    ("QG1.formal.3", ["1450", "purchase.md:2"]),
    ("QG1.formal.4", ["UTF-8 BOM", "CRLF", "27"]),
    ("QG1.formal.5", ["Observation 001: cabinet 2; sealed folders 14", "Observation 096: cabinet 6; sealed folders 109", "Limitations:"]),
    ("QG1.formal.6", ["76", "35", "420", "原始发票"]),
    ("QG1.formal.7", ["4000", "4 L", "East"]),
    ("QG1.formal.8", ["June 21", "June 14", "May 9"]),
    ("QG1.formal.9", ["missing-contract.txt", "unavailable"]),
    ("QG1.formal.10", ["06:40", "20:15"]),
    ("QG1.tuning.1", ["13", "4.5", "unknown"]),
    ("QG1.tuning.2", ["K11", "K13", "K12", "status not recorded"]),
    ("QG2.formal.1", []), ("QG2.formal.2", []), ("QG2.formal.3", ["3.50"]),
    ("QG2.formal.4", []), ("QG2.formal.5", []), ("QG2.formal.6", []),
    ("QG2.formal.7", ["19.00"]), ("QG2.formal.8", []),
    ("QG2.formal.9", ["failed", "3.50"]), ("QG2.formal.10", ["paused"]),
    ("QG2.tuning.1", ["14.00"]), ("QG2.tuning.2", []),
])
def test_each_reading_and_file_task_consumes_its_distinct_material(task_id, answer_tokens, acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_files import exercise_files

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_files(reference, task_id, "deepseek", root=acceptance_public_root))
        assert outcome.product_status == "passed", outcome.facts
        saved = json.loads(outcome.evidence["conversation"].read_bytes())
        answer = saved["turns"][-1]["answer"]
        for token in answer_tokens:
            assert token in answer
        assert outcome.facts["quality_native"]["source_text_in_sdk"] is True
        assert outcome.facts["quality_native"]["gold_not_in_sdk"] is True
        assert outcome.facts["real_http_attempts"] == 0
        assert outcome.facts["independent_quality_sample_increment"] == 0
        assert outcome.semantic_status == "not_measured"
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_reimbursement_terminal_answer_quotes_only_the_current_policy(tmp_path):
    from tests.support.agent_acceptance.quality_files import replies
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    source = "交通上限90元，餐费自付，住宿需原始发票。乘车67元，餐费39元，住宿430元只有截图。"
    visible = QualityVisibleInput(task_id="QG1.formal.6", adapter="qg1-6", goal="解释当前政策及缺证项。",
        sources={"政策.txt": QualitySource(text=source)}, parameters={"reads": ["政策.txt"]})
    read, final = replies(visible, tmp_path, "deepseek")

    assert read.tool == "read_text"
    assert source in final.text
    assert all(old not in final.text for old in ("76元", "80元", "35元", "420元"))


def test_interrupted_terminal_fragment_uses_the_current_source(tmp_path):
    from tests.support.agent_acceptance.quality_files import replies
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput

    source = "Quarry tram QT3 may skip the 09:20 station; the stop is unconfirmed."
    visible = QualityVisibleInput(task_id="QG1.formal.10", adapter="qg1-10", goal="Report the current tram notice.",
        sources={"tram.txt": QualitySource(text=source)},
        parameters={"reads": ["tram.txt"], "interrupt_then_continue": True})
    read, fragment, final = replies(visible, tmp_path, "deepseek")

    assert read.tool == "read_text"
    assert fragment.interrupt and source in fragment.text
    assert "Linden Ferry" not in fragment.text
    assert source in final.text
