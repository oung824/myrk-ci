import asyncio
from hashlib import sha256
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from myrk.project_paths import project_root
from tests.support.agent_acceptance import quality_runtime
from tests.support.agent_acceptance.frozen import SOURCE, source_material_path
from tests.support.agent_acceptance.quality_files import exercise_files, score
from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
from tests.support.agent_acceptance.quality_packet import PACKETS
from tests.support.agent_acceptance.quality_runtime import bind_packet, bound_task, manifest


@pytest.fixture(scope="module")
def synthetic_quality_packet():
    parent = (SOURCE / PACKETS).resolve()
    packet = parent / ("isolation-regression-" + uuid4().hex)
    try:
        yield build_implementation_quality_packet(packet)
    finally:
        if packet.exists():
            assert packet.resolve() == packet
            shutil.rmtree(packet)


@pytest.mark.parametrize(
    ("task_id", "sources", "expected_total"),
    [
        ("QG2.formal.3", ["amounts.csv"], "3.50"),
        ("QG2.formal.7", ["flower.tsv"], "19.00"),
        ("QG2.formal.9", ["receiver.csv", "corrected.csv"], "3.50"),
    ],
)
def test_registered_answers_stay_private_and_decimal_check_runs(
    acceptance_public_root, synthetic_quality_packet, tmp_path, monkeypatch, task_id, sources, expected_total,
):
    original = source_material_path(synthetic_quality_packet.path).parent / "gold" / f"{task_id}.json"
    original_bytes = original.read_bytes()
    guard = json.loads(original_bytes)["private_review_guard"]
    draft = tmp_path / "new-evaluator-drafts"
    draft.mkdir()
    directory = draft.relative_to(project_root(SOURCE)).as_posix()

    def bind_with_evaluator_directory(*args, **kwargs):
        return bind_packet(*args, **kwargs, evaluator_isolation_directories=(directory,))

    monkeypatch.setattr(quality_runtime, "bind_packet", bind_with_evaluator_directory)
    registered_copy = draft / ("isolation-synthetic-" + uuid4().hex + ".json")
    with registered_copy.open("xb") as copy:
        copy.write(original_bytes)
    runtime = acceptance_public_root
    checker = runtime / "materials/quality_check.py"

    try:
        outcome = asyncio.run(exercise_files(
            synthetic_quality_packet, task_id, "deepseek", root=runtime,
            probe_paths=(original, registered_copy, checker),
        ))
        assert registered_copy.read_bytes() == original.read_bytes() == original_bytes
    finally:
        registered_copy.unlink()
    facts = outcome.facts
    observations = facts["quality_native"]
    assert set(sources) <= set(observations["actual_sources_read"]), facts
    assert facts["additional_denial_probes"][:2] == [True, True], facts
    check = next(action for action in facts["actions"] if action["kind"] == "check")
    assert check["status"] == "completed" and check["exit_code"] == 0 and check["cleanup_complete"] is True
    assert observations["independent_check_sum_matches_gold"] is True
    assert observations["independent_check_sum"] == expected_total
    assert observations["check_passed"] is True
    assert observations["gold_not_in_sdk"] is True
    checker_read = next(action for action in facts["actions"] if action["kind"] == "read"
        and action["prepared_parameters"].get("path") == str(checker))
    assert checker_read["status"] == "completed"
    assert expected_total not in checker_read["output"]
    if task_id == "QG2.formal.9":
        assert observations["original_failure_retained"] is True
    exchange = outcome.evidence["exchange"].read_text(encoding="utf-8")
    assert guard not in exchange
    assert facts["real_http_attempts"] == 0


def test_manifest_rejects_removed_evaluator_copy_denial(tmp_path, synthetic_quality_packet):
    checkout = tmp_path / "clone"
    root = tmp_path / "runtime"
    evidence_root = tmp_path / "evaluator-copy"
    registered_root = tmp_path / "new-evaluator-drafts"
    registered_root.mkdir()
    directory = registered_root.relative_to(project_root(SOURCE)).as_posix()
    bind_packet(synthetic_quality_packet, "QG2.formal.3", root, source_roots=(checkout,),
        evidence_root=evidence_root, evaluator_isolation_directories=(directory,))
    case_id = "quality.qg2.formal.3"
    settings = manifest(case_id, root, "deepseek")["approval"]["access"]
    path = root / "quality-task.json"
    original = json.loads(path.read_bytes())
    for missing in (registered_root, evidence_root, root / "review-context"):
        assert str(missing) in settings["denied_paths"]
        binding = {**original, "denied_paths": [entry for entry in original["denied_paths"] if entry != str(missing)]}
        path.write_text(json.dumps(binding), encoding="utf-8")
        with pytest.raises(ValueError, match="denial record changed"):
            manifest(case_id, root, "deepseek")


def test_version_bound_independent_total_is_compared_to_private_gold(
    acceptance_public_root, synthetic_quality_packet,
):
    root = acceptance_public_root
    outcome = asyncio.run(exercise_files(synthetic_quality_packet, "QG2.formal.3", "deepseek", root=root))
    assert outcome.tool_status == "passed", outcome.facts
    assert outcome.facts["quality_native"]["independent_check_sum"] == "3.50"
    record = json.loads(outcome.evidence["conversation"].read_bytes())
    exchange = json.loads(outcome.evidence["exchange"].read_bytes())
    check = next(action for turn in record["turns"] for action in turn["actions"] if action["kind"] == "check")
    receipt_path = Path(check["receipt_path"])
    stdout_path = Path(check["stdout_path"])
    original_receipt = receipt_path.read_bytes()
    original_stdout = stdout_path.read_bytes()
    receipt = json.loads(original_receipt)
    wrong_stdout = b'{"sum": "99.50"}\n'
    receipt["stdout_sha256"] = sha256(wrong_stdout).hexdigest()
    check["stdout"] = wrong_stdout.decode("utf-8")

    try:
        receipt_path.write_bytes(json.dumps(receipt).encode("utf-8"))
        stdout_path.write_bytes(wrong_stdout)
        _, visible, _ = bound_task("quality.qg2.formal.3", root)
        result = score(visible, root, [record], [exchange], native_entry=True)
        observations = result["quality_native"]
        assert observations["calculation_matches_gold"] is True
        assert observations["independent_check_sum"] == "99.50"
        assert observations["independent_check_sum_matches_gold"] is False
        assert observations["check_passed"] is False
        assert result["passed"] is False
    finally:
        receipt_path.write_bytes(original_receipt)
        stdout_path.write_bytes(original_stdout)
    check["stdout"] = original_stdout.decode("utf-8").replace("\r\n", "\n")

    task, visible, _ = bound_task("quality.qg2.formal.3", root)
    guard = json.loads(source_material_path(task.gold_path).read_bytes())["private_review_guard"]
    exchange["sdk_inputs"].append({"response": ["Synthetic captured tool result", guard]})
    result = score(visible, root, [record], [exchange], native_entry=True)
    assert result["quality_native"]["check_passed"] is True
    assert result["quality_native"]["gold_not_in_sdk"] is False
    assert result["critical_contracts"]["K1"] == "failed"
    assert result["passed"] is False
