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


@pytest.mark.acceptance_desktop
@pytest.mark.parametrize(("protocol", "task_id", "source"), [
    pytest.param("deepseek", "QG1.tuning.1", "weather.txt", id="deepseek"),
    pytest.param("openai-chat", "QG1.tuning.1", "weather.txt", id="openai-chat"),
    pytest.param("google", "QG1.tuning.1", "weather.txt", id="google"),
    pytest.param("deepseek", "QG1.formal.10", "ferry.txt", id="interrupted-reading"),
    pytest.param("deepseek", "QG2.formal.3", "amounts.csv", id="decimal-independent-check"),
    pytest.param("deepseek", "QG2.formal.10", "version.txt", id="version-conflict"),
])
def test_canonical_reading_task_reaches_real_main_and_native_source_input(
    prepared_root, acceptance_public_root, protocol, task_id, source,
):
    from hashlib import sha256
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance import builtin_registry, quality_plan, FrozenParentBudget, prepare_live, LiveAuthorization, LiveGate
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.frozen import SOURCE
    from myrk.project_paths import project_root

    root = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    parent_id = "synthetic-quality-native-" + root.name
    index = project_root(SOURCE) / ".scratch/acceptance-parent-budgets" / (sha256(parent_id.encode()).hexdigest() + ".json")
    try:
        reference = build_implementation_quality_packet(root)
        registry = builtin_registry(quality_packet=reference)
        parent = FrozenParentBudget(parent_id=parent_id, accounting_root=prepared_root, request_cap=2520,
            phase_microusd={"screening": 0, "quality": 39076920, "judge": 0}, reserve_microusd=0, profile="synthetic")
        plan = quality_plan(root / "freeze.json", registry=registry, parent_budget=parent, mode="offline")
        selected = next(row for row in plan.executions if row.scenario_id == task_id)
        selected = selected.model_copy(update={"protocol": protocol, "cost_cap_microusd": max(row.cost_cap_microusd for row in plan.executions)})
        assert registry.cases[selected.case_id].formal_live is True
        parent = FrozenParentBudget(parent_id=parent_id, accounting_root=prepared_root, request_cap=selected.request_cap,
            phase_microusd={"screening": 0, "quality": selected.cost_cap_microusd, "judge": 0}, reserve_microusd=0, profile="synthetic")
        selected = selected.model_copy(update={"order": 1, "dependencies": ()})
        plan = AcceptancePlan(executions=(selected,), parent_budget=parent, mode="offline", quality_packet=reference)
        bundle = prepare_live(plan.model_copy(update={"mode": "prepare-live"}), root=prepared_root, registry=registry)
        grant = LiveAuthorization(authorization_id="synthetic-canonical-native", binding=bundle.binding, transport="mock",
            parent_budget_id=parent_id, total_limit=parent.request_cap, total_microusd=parent.total_microusd,
            case_limits={row.execution_id:row.request_cap for row in plan.executions},
            case_microusd={row.execution_id:row.cost_cap_microusd for row in plan.executions})
        import subprocess, sys
        from tests.support.agent_acceptance.formal import environment
        from tests.support.agent_acceptance.preparation import LiveExecutionRequest
        acceptance_public_root.mkdir(parents=True)
        request = acceptance_public_root / "execution-request.json"
        request.write_text(LiveExecutionRequest(run_root=prepared_root, grant=grant).model_dump_json(), encoding="utf-8")
        process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "execute", str(request),
            "--output", str(acceptance_public_root / "formal-result")], cwd=prepared_root,
            env={**environment(acceptance_public_root / "controller-environment"), "PYTHONPATH": str(prepared_root)}, capture_output=True)
        (acceptance_public_root / "controller.stdout").write_bytes(process.stdout)
        (acceptance_public_root / "controller.stderr").write_bytes(process.stderr)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        result = json.loads((acceptance_public_root / "formal-result/result.json").read_bytes())
        outcome = result["cases"][0]
        assert outcome["tool_status"] == outcome["product_status"] == "passed", outcome["facts"]
        assert outcome["semantic_status"] == "not_measured"
        assert outcome["facts"]["quality_native"]["actual_sources_read"] == [source]
        assert outcome["facts"]["quality_native"]["source_text_in_sdk"] is True
        assert outcome["facts"]["quality_native"]["gold_not_in_sdk"] is True
        assert outcome["facts"]["real_http_attempts"] == 0
        assert outcome["facts"]["processes_complete"] is True
        assert len(result["cases"]) == result["planned_denominator"] == 1
        if task_id == "QG1.formal.10":
            assert outcome["facts"]["quality_native"]["interruption_preserved"] is True
            assert outcome["facts"]["quality_native"]["open_sends"] == 0
            assert outcome["critical_contracts"]["K3"] == outcome["critical_contracts"]["K5"] == "passed"
        elif task_id == "QG2.formal.3":
            assert outcome["facts"]["quality_native"]["independent_check_sum"] == "3.50"
            assert outcome["facts"]["quality_native"]["independent_check_sum_matches_gold"] is True
        elif task_id == "QG2.formal.10":
            assert outcome["facts"]["quality_native"]["conflict_pause"] is True
            assert outcome["facts"]["quality_native"]["actual_files_match"] == {source: True}
        else:
            assert outcome["facts"]["budget"]["reserved_requests"] == 2
    finally:
        if (root / "freeze.json").is_file():
            archive = acceptance_public_root.with_name(acceptance_public_root.name + "-review-context")
            archive.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "freeze.json", archive / "freeze.original.json")
            for label in ("input", "gold"):
                source = root / label / "QG1.tuning.1.json"
                if source.is_file(): shutil.copyfile(source, archive / (label + ".original.json"))
            (archive / "purpose.json").write_text(json.dumps({"purpose": "review_context", "product_output": False, "human_review": "not_performed"}), encoding="utf-8")
        if index.exists(): index.unlink()
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


@pytest.mark.parametrize("evidence_change", [
    pytest.param(None, id="bound-receipt"),
    pytest.param("different hashes", id="different-hashes"),
    pytest.param("later SDK input", id="later-sdk-input"),
    pytest.param("unknown write", id="unknown-write"),
])
def test_canonical_version_conflict_preserves_real_external_bytes_and_failed_edit(
    acceptance_public_root, monkeypatch, evidence_change,
):
    from hashlib import sha256
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance import quality_files
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet

    original_score = quality_files.score
    if evidence_change:
        def score(visible, root, records, exchanges, **kwargs):
            receipt = root / "external-conflict.json"
            evidence = json.loads(receipt.read_bytes())
            if evidence_change == "different hashes":
                evidence["before_sha256"] = sha256(b"unrelated original").hexdigest()
                evidence["after_sha256"] = sha256(b"unrelated replacement").hexdigest()
            elif evidence_change == "later SDK input":
                later = exchanges[0]["sdk_inputs"][-1]["sdk_input_id"]
                assert later != evidence["sdk_input_id"]
                evidence["sdk_input_id"] = later
            else:
                records[0]["turns"][0]["actions"].append({
                    "id": "controlled-unknown-write", "kind": "write", "status": "failed", "effect": "unknown",
                })
            receipt.write_text(json.dumps(evidence), encoding="utf-8")
            return original_score(visible, root, records, exchanges, **kwargs)

        monkeypatch.setattr(quality_files, "score", score)
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(quality_files.exercise_files(reference, "QG2.formal.10", "deepseek", root=acceptance_public_root))
        assert (acceptance_public_root / "materials/version.txt").read_bytes() == b"Osprey external version=11\n"
        edits = [row for row in outcome.facts["actions"] if row["kind"] == "edit"]
        assert len(edits) == 1 and edits[0]["status"] == "conflict" and edits[0]["effect"] == "not_started"
        assert edits[0]["error"]
        assert outcome.facts["quality_native"]["conflict_pause"] is (evidence_change is None)
        assert outcome.product_status == ("passed" if evidence_change is None else "failed")
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_canonical_delivery_uses_actual_change_ids_for_both_files(acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_files import exercise_files

    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("implementation-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_files(reference, "QG2.formal.6", "deepseek", root=acceptance_public_root))
        assert outcome.product_status == "passed", outcome.facts
        saved = json.loads(outcome.evidence["conversation"].read_bytes())
        artifacts = saved["turns"][-1]["artifacts"]
        assert len(artifacts) == 2 and {row["name"] for row in artifacts} == {"summary.txt", "handoff.txt"}
        assert len({row["file_change_id"] for row in artifacts}) == 2
        assert saved["turns"][-1]["delivery_status"] == "awaiting_acceptance"
        assert outcome.facts["quality_native"]["explicit_delivery"] is True
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
