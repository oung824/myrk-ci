"""Missing domain records retain uncertainty instead of fabricated critical results."""
import asyncio
import json
from pathlib import Path
import shutil
from uuid import uuid4
import pytest


@pytest.mark.parametrize("task_id", ["QG5.formal.1", "QG5.formal.2", "QG5.formal.3", "QG5.formal.4", "QG5.formal.10"])
def test_qg5_missing_records_do_not_invent_critical_pass_or_failure(task_id, acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    from tests.support.agent_acceptance.quality_runtime import score
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("uncertainty-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, task_id, "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        exchange = json.loads(outcome.evidence["service.exchange"].read_bytes())
        incomplete = score("quality." + task_id.lower(), acceptance_public_root, [], [exchange])
        assert incomplete["critical_contracts"]["K1"] == "unknown", incomplete
        if task_id in {"QG5.formal.3", "QG5.formal.4"}:
            assert incomplete["critical_contracts"]["K2"] == "unknown"
        if task_id == "QG5.formal.4":
            assert incomplete["critical_contracts"]["K3"] == "unknown"
    finally:
        if packet.exists():
            preserved = acceptance_public_root / "review-context"
            preserved.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/" + task_id + ".json", "gold/" + task_id + ".json"):
                target = preserved / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, target)
            shutil.rmtree(packet)


def test_qg6_never_started_read_only_task_does_not_vacuously_pass_critical_scope(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, score
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("uncertainty-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        acceptance_public_root.mkdir(parents=True, exist_ok=False)
        bind_packet(reference, "QG6.formal.6", acceptance_public_root)
        observed = score("quality.qg6.formal.6", acceptance_public_root, [], [])
        assert observed["critical_contracts"]["K1"] == "unknown"
    finally:
        if packet.exists(): shutil.rmtree(packet)


def test_qg5_partial_stop_observation_keeps_missing_continuation_unknown(acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    from tests.support.agent_acceptance.quality_runtime import score
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("uncertainty-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, "QG5.formal.4", "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        observation = acceptance_public_root / "quality-parallel-observation.json"
        actual = json.loads(observation.read_bytes())
        (acceptance_public_root / "complete-parallel-observation.original.json").write_bytes(observation.read_bytes())
        actual.pop("b_running_after_a_stop")
        observation.write_text(json.dumps(actual), encoding="utf-8")
        records = [json.loads(outcome.evidence[key + ".conversation"].read_bytes()) for key in ("a", "b")]
        exchange = json.loads(outcome.evidence["service.exchange"].read_bytes())
        incomplete = score("quality.qg5.formal.4", acceptance_public_root, records, [exchange])
        assert incomplete["passed"] is False
        assert incomplete["critical_contracts"]["K3"] == "unknown", incomplete
    finally:
        if packet.exists():
            preserved = acceptance_public_root / "review-context"; preserved.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG5.formal.4.json", "gold/QG5.formal.4.json"):
                target = preserved / name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(packet / name, target)
            shutil.rmtree(packet)


def test_qg5_caller_permission_marker_cannot_invent_a_known_product_failure(acceptance_public_root):
    from hashlib import sha256
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
    from tests.support.agent_acceptance.quality_recovery import exercise_recovery
    from tests.support.agent_acceptance.quality_runtime import score
    packet = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/quality-packets" / ("uncertainty-" + uuid4().hex)
    try:
        reference = build_implementation_quality_packet(packet)
        outcome = asyncio.run(exercise_recovery(reference, "QG5.formal.4", "deepseek", root=acceptance_public_root))
        assert outcome.tool_status == "passed", outcome.facts
        records = [json.loads(outcome.evidence[key + ".conversation"].read_bytes()) for key in ("a", "b")]
        initial_raw = outcome.evidence["a.conversation"].read_bytes()
        (acceptance_public_root / "quality-permission-initial-before-new.original.json").write_bytes(initial_raw)
        marker = {"origin": "public_new_after_temporary_configuration", "explicit_mode_negative": False,
            "initial_before_new_sha256": sha256(initial_raw).hexdigest(), "initial_task_id": records[0]["turns"][-1]["task"]["id"],
            "new_task_id": records[1]["turns"][-1]["task"]["id"], "before_http_count": 3, "after_http_count": 3}
        (acceptance_public_root / "quality-permission-change.json").write_text(json.dumps(marker), encoding="utf-8")
        observation = acceptance_public_root / "quality-parallel-observation.json"
        actual = json.loads(observation.read_bytes()); actual["permission_change"] = marker
        observation.write_text(json.dumps(actual), encoding="utf-8")
        exchange = json.loads(outcome.evidence["service.exchange"].read_bytes())
        observed = score("quality.qg5.formal.4", acceptance_public_root, records, [exchange])
        assert observed["tool_contract_passed"] is False
        assert observed["critical_contracts"]["K1"] == "unknown", observed
    finally:
        if packet.exists():
            preserved = acceptance_public_root / "review-context"; preserved.mkdir(parents=True, exist_ok=True)
            for name in ("freeze.json", "input/QG5.formal.4.json", "gold/QG5.formal.4.json"):
                target = preserved / name; target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(packet / name, target)
            shutil.rmtree(packet)
