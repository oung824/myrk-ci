"""The shared quality entry owns one current-task projection; no model runs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


CASES = [(f"QG6.formal.{slot}", "deepseek") for slot in range(1, 11)] + [
    ("QG6.tuning.1", "deepseek"), ("QG6.tuning.2", "deepseek"),
    ("QG6.formal.3", "google"), ("QG6.formal.9", "google")]


@pytest.fixture(scope="module")
def projection_packet():
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_packet import PACKETS, read_quality_packet

    packet = SOURCE / PACKETS / ("light-qg6-projection-" + uuid4().hex)
    assert not packet.exists()
    try:
        reference = build_implementation_quality_packet(packet)
        frozen = read_quality_packet(reference)
        report = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/qg6-manifest-context"
        report.mkdir(parents=True, exist_ok=False)
        (report / "freeze.original.json").write_bytes((packet / "freeze.json").read_bytes())
        (report / "usage-scope.json").write_text(json.dumps({"material_packet": reference.model_dump(mode="json"),
            "purpose": "implementation-seen manifest projection only", "model_requests": 0,
            "Main_or_compression_executed": False, "semantic_status": "not_measured"}), encoding="utf-8")
        yield reference, frozen
    finally:
        if packet.exists():
            assert packet.resolve().is_relative_to((SOURCE / PACKETS).resolve())
            assert not packet.is_symlink() and not packet.is_junction()
            shutil.rmtree(packet)


@pytest.mark.parametrize("task_id,protocol", CASES)
def test_canonical_qg6_projects_current_task_once_and_retains_its_contract(projection_packet, acceptance_public_root, task_id, protocol):
    from myrk.permissions import ApprovalSettings
    from tests.support.agent_acceptance import quality_compaction
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_packet import PACKETS
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest, model_prompt, required_outputs

    reference, frozen = projection_packet
    root = acceptance_public_root
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    task = next(row for row in frozen.tasks if row.task_id == task_id)
    request = manifest("quality." + task_id.lower(), root, protocol)
    family = quality_compaction.manifest(visible, root, protocol)
    assert {key: value for key, value in request.items() if key not in {"prompt", "approval", "quality_adapter"}} == {
        key: value for key, value in family.items() if key not in {"prompt", "approval"}}
    assert request["quality_adapter"] is True
    approval = ApprovalSettings.model_validate(request["approval"])
    assert approval.access.sandbox == "restricted" and approval.access.writable_roots == [str(root / "materials")]
    assert {str(SOURCE / PACKETS), str(SOURCE / "tests"), str(root / "review-only")} <= set(approval.access.denied_paths)
    assert request["scope"]["allowed_actions"] == ["read", "list"]
    assert required_outputs(visible) == task.required_outputs
    assert set(task.prior_uses) == {"implementation"}
    originals = {name: sha256((root / "materials" / name).read_bytes()).hexdigest() for name in visible.sources}
    assert originals == {name: sha256(source.text.encode(source.encoding)).hexdigest() for name, source in visible.sources.items()}
    control = request["quality_compaction"]
    if task_id in {"QG6.formal.6", "QG6.formal.10"}:
        assert len(control["sources"]) == 32 and task.required_outputs["summary"] >= 3
        assert "at least three normal summaries" in control["continuation"]
    if task_id == "QG6.formal.10":
        assert request["phases"] == ["work", "capacity-reopen", "reopen"]
    if task_id == "QG6.formal.9":
        assert len(control["sources"]) == 16 and control["correction_flow"] is True
        assert control["roster"] and control["primary"] and control["correction"]
    current = model_prompt(visible)
    observation = {"task_id": task_id, "protocol": protocol, "current_task_projection": current,
        "request": request, "source_sha256": originals, "required_outputs": task.required_outputs,
        "projection_copies": request["prompt"].count(current), "model_requests": 0, "semantic_status": "not_measured"}
    (root / "manifest-observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    assert request["prompt"].startswith(current)
    assert request["prompt"].count(current) == 1


def test_projection_fix_keeps_original_context_configuration_and_frozen_request_caps():
    from tests.support.agent_acceptance.formal import provider_config
    from tests.support.agent_acceptance.formal_scenarios import scenario_config
    from tests.support.agent_acceptance.frozen import full_plan_proposal

    for protocol in ("deepseek", "google"):
        config = scenario_config("quality.qg6.formal.6", provider_config(protocol))
        assert (config.context_window_override, config.output_token_reserve, config.context_safety_margin,
            config.compaction_trigger, config.compaction_target, config.compaction_recent) == (26000, 4096, 512, .7, .55, .1)
    proposal = full_plan_proposal()
    assert {row.request_cap for row in proposal.executions if row.scenario_id.startswith("QG6.")} == {60}
    assert {row.request_cap for row in proposal.executions if row.scenario_id in {f"R{number}" for number in range(19, 27)}} == {80}
