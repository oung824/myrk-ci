"""Recognize exact closed Mock observations without changing their product result."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.acceptance.test_formal_scenarios import assert_formal_qualification
from tests.support.agent_acceptance.core import CaseOutcome


ORIGINALS = Path(__file__).parent / "materials/formal-observations-4ef"
OWNERS = Path(__file__).parent / "materials/deliverables/owner-observation-4ef"


def closed_outcome(tmp_path, name, originals=ORIGINALS):
    index = json.loads((originals / "index.json").read_bytes())
    row = index["entries"][name]
    paths = {}
    for label, original in row["files"].items():
        archive = originals / original.get("archive", "originals.zip")
        archive_row = index["supplemental_archive"] if "archive" in original else index["archive"]
        assert archive.stat().st_size == archive_row["bytes"]
        assert sha256(archive.read_bytes()).hexdigest() == archive_row["sha256"]
        with ZipFile(archive) as zipped:
            raw = zipped.read(original["member"])
        assert len(raw) == original["bytes"] and sha256(raw).hexdigest() == original["sha256"]
        path = tmp_path / original["member"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        paths[label] = path
    original = json.loads(paths["result"].read_bytes())["cases"][0]
    fields = deepcopy(original)
    fields["evidence"] = {label: paths.get(label, Path(path)) for label, path in original["evidence"].items()}
    return CaseOutcome(**fields), paths


@pytest.mark.parametrize("name", ["memory-cross", "memory-changed"])
def test_complete_closed_memory_observation_preserves_unknown_or_failed_qualification(tmp_path, name):
    outcome, _ = closed_outcome(tmp_path, name)
    before = asdict(outcome)
    assert_formal_qualification(outcome)
    assert asdict(outcome) == before
    assert outcome.product_status == ("inconclusive" if name == "memory-cross" else "failed")
    assert outcome.critical_contracts["K1"] == outcome.critical_contracts["K2"] == ("unknown" if name == "memory-cross" else "failed")


@pytest.mark.parametrize("name", ["memory-cross", "memory-changed"])
@pytest.mark.parametrize("damage", ["other-failure", "subject", "body", "approval-id", "prepared-hash", "context-hash", "sdk-body", "wire-body", "source-content", "missing-source"])
def test_other_memory_failure_subject_bytes_or_binding_cannot_match_known_observation(tmp_path, name, damage):
    outcome, paths = closed_outcome(tmp_path, name)
    contract = outcome.facts["memory_contract"]
    recall = next(action for action in outcome.facts["actions"] if action["kind"] == "memory_recall")
    if damage == "other-failure":
        recall.update(status="failed", effect="not_started", error="A different local memory failure")
        contract["cross_project_effect"] = "failed"
    elif damage == "subject":
        recall["target"] += "-other-project"
        contract["current_facts"][0]["project"] = recall["target"]
    elif damage == "body":
        contract["current_facts"][0]["text"] = recall["output"] = "A different experience: 9999."
    elif damage in {"approval-id", "prepared-hash", "context-hash"}:
        key = {"approval-id": "approval_id", "prepared-hash": "prepared_action_sha256", "context-hash": "approval_context_sha256"}[damage]
        recall[key] = "0" * len(recall[key])
    elif damage in {"sdk-body", "wire-body"}:
        exchange = json.loads(paths["work/receiver/exchange.json"].read_bytes())
        if damage == "sdk-body":
            for message in exchange["sdk_inputs"][-1]["messages"]:
                for part in message["parts"]:
                    if part.get("part_kind") == "tool-return" and part.get("tool_name") == "recall_project_memory":
                        part["content"] += " changed bytes"
        else:
            body = json.loads(exchange["exchanges"][-1]["request_utf8"])
            next(message for message in reversed(body["messages"]) if message["role"] == "tool")["content"] += " changed bytes"
            exchange["exchanges"][-1]["request_utf8"] = json.dumps(body)
        paths["work/receiver/exchange.json"].write_text(json.dumps(exchange), encoding="utf-8")
    elif damage == "source-content":
        driver = json.loads(paths["work/receiver/state.json"].read_bytes())
        driver["approval_decisions"][0]["source_content"] = ["A different experience: 9999."]
        paths["work/receiver/state.json"].write_text(json.dumps(driver), encoding="utf-8")
    else:
        paths["records/memories/facts.json"].unlink()
    review = json.loads(paths["memory_review"].read_bytes())
    review["actual_facts"] = deepcopy(contract)
    paths["memory_review"].write_text(json.dumps(review), encoding="utf-8")
    for label, path in paths.items():
        if label.startswith("records/") and label.count("/") == 1 and path.suffix == ".json":
            record = json.loads(path.read_bytes())
            for turn in record["turns"]:
                turn["actions"] = [deepcopy(recall) if action["id"] == recall["id"] else action for action in turn["actions"]]
            path.write_text(json.dumps(record), encoding="utf-8")
    if damage in {"subject", "body"}:
        current = json.loads(paths["records/memories/facts.json"].read_bytes())
        current["facts"] = deepcopy(contract["current_facts"])
        paths["records/memories/facts.json"].write_text(json.dumps(current), encoding="utf-8")
    with pytest.raises((AssertionError, FileNotFoundError)):
        assert_formal_qualification(outcome)


@pytest.mark.parametrize("phase", ["before", "after"])
def test_changed_memory_source_requires_the_exact_observed_source_bytes(tmp_path, phase):
    outcome, paths = closed_outcome(tmp_path, "memory-changed")
    source = paths["source-" + phase + ".json"]
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(AssertionError):
        assert_formal_qualification(outcome)


@pytest.mark.parametrize("protocol", ["deepseek", "google"])
@pytest.mark.parametrize("control", ["original", "missing-reopen", "missing-counter", "ordinary-profile"])
def test_complete_old_deliverables_result_adds_only_sourced_counter_projection(tmp_path, protocol, control):
    from tests.support.agent_acceptance.deliverables import score

    outcome, _ = closed_outcome(tmp_path, "deliverables-" + protocol)
    original = asdict(outcome)
    assert "normal_profile_accesses" not in outcome.facts
    index = json.loads((OWNERS / "index.json").read_bytes())
    owners = {}
    for phase, row in index["entries"][protocol]["phases"].items():
        raw = (OWNERS / row["member"]).read_bytes()
        assert len(raw) == row["bytes"] and sha256(raw).hexdigest() == row["sha256"]
        target = tmp_path / "counter-projection" / phase / "owner/state.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(raw)
        owners[phase] = target
    if control == "missing-reopen":
        owners["reopen"].unlink()
    elif control in {"missing-counter", "ordinary-profile"}:
        state = json.loads(owners["work"].read_bytes())
        if control == "missing-counter":
            state.pop("normal_profile_accesses")
        else:
            state.update(synthetic_profile=False, normal_profile_accesses=1)
        owners["work"].write_text(json.dumps(state), encoding="utf-8")
    projection = score("formal.deliverables", tmp_path / "counter-projection", [], [])
    assert projection["passed"] is False and projection["product_execution"] == "incomplete"
    counter = projection["normal_profile_accesses"]
    assert counter == (0 if control == "original" else 1 if control == "ordinary-profile" else None)
    outcome.facts["normal_profile_accesses"] = counter
    assert_formal_qualification(outcome)
    after = asdict(outcome)
    after["facts"].pop("normal_profile_accesses")
    assert after == original
    if control == "original":
        assert outcome.facts["real_http_attempts"] == counter == 0
    else:
        assert counter != 0
    assert outcome.product_status == "passed" and outcome.semantic_status == "not_measured"
