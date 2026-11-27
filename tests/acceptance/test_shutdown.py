"""Known cancelled-review shutdown is a product failure, never a silent ignore."""

from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import pytest


MATERIAL = Path(__file__).parent / "materials/shutdown"


def original_case(tmp_path, variant="generator-close"):
    references = {"generator-close": "signature.json", "loop-shutdown": "loop-signature.json", "generator-pair": "pair-signature.json"}
    signature = json.loads((MATERIAL / references[variant]).read_bytes())
    archive = MATERIAL / signature["original_archive"]
    assert sha256(archive.read_bytes()).hexdigest() == signature["original_archive_sha256"]
    root = tmp_path / "actual-observed-case"
    root.mkdir()
    with ZipFile(archive) as source:
        for entry in signature["files"]:
            body = source.read(entry["path"])
            assert sha256(body).hexdigest() == entry["sha256"]
            target = root / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    return root, signature


@pytest.mark.parametrize("variant", ["generator-close", "loop-shutdown", "generator-pair"])
def test_exact_current_shutdown_keeps_raw_result_and_marks_product_failed(tmp_path, variant):
    from tests.support.agent_acceptance.shutdown import classify_known_shutdown

    root, signature = original_case(tmp_path, variant)
    result_path = root / "result.json"
    original = result_path.read_bytes()
    diagnostic = root / "diagnostic/qualification_error.json"
    diagnostic_bytes = diagnostic.read_bytes()
    classified = classify_known_shutdown(result_path, diagnostic, nodeid=signature["nodeid"])
    assert classified is not None
    actual = json.loads(result_path.read_bytes())
    case = actual["cases"][0]
    assert case["product_status"] == "failed" and case["tool_status"] == "passed"
    assert case["expectation_status"] == "review_required"
    assert actual["qualification"]["status"] == "known_product_failure"
    assert actual["qualification"]["historical_relation"] == "unknown"
    row = next(item for item in actual["coverage"] if item["case_id"] == "review.stop")
    assert row["offline"] == row["execution"] == "failed"
    preserved = root / case["evidence"]["result_before_async_shutdown"]
    assert preserved.read_bytes() == original
    assert diagnostic.read_bytes() == diagnostic_bytes
    index = json.loads((root / case["evidence"]["evidence_index"]).read_bytes())
    for label in ("result_before_async_shutdown", "async_shutdown_diagnostic", "async_shutdown_original_log"):
        path = root / case["evidence"][label]
        assert path.is_file() and index["links"][label]["sha256"] == sha256(path.read_bytes()).hexdigest()
    human_path = root / case["evidence"]["semantic_review"]
    human = json.loads(human_path.read_bytes())
    assert human["program_product_status"] == "failed" and human["semantic_status"] == "not_measured"
    assert human["qualification"] == actual["qualification"]
    assert index["links"]["semantic_review"]["sha256"] == sha256(human_path.read_bytes()).hexdigest()
    before_human = root / case["evidence"]["human_review_before_async_shutdown"]
    assert json.loads(before_human.read_bytes())["semantic_status"] == "not_measured"


def test_stable_selected_entry_uses_the_same_narrow_observed_failure(tmp_path):
    from tests.support.agent_acceptance.shutdown import SELECTED_NODE, classify_known_shutdown

    root, _signature = original_case(tmp_path)
    path = root / "diagnostic/qualification_error.json"
    diagnostic = json.loads(path.read_bytes())
    diagnostic["nodeid"] = SELECTED_NODE
    path.write_text(json.dumps(diagnostic), encoding="utf-8")
    assert classify_known_shutdown(root / "result.json", path, nodeid=SELECTED_NODE) is not None
    result = json.loads((root / "result.json").read_bytes())
    assert result["cases"][0]["product_status"] == "failed"


@pytest.mark.parametrize("change", ["ordinal", "other_entry", "other_task_name", "extra_log"])
def test_loop_shutdown_only_normalizes_the_default_numeric_task_ordinal(tmp_path, change):
    from tests.support.agent_acceptance.shutdown import NODE, classify_known_shutdown, literal_reference

    root, signature = original_case(tmp_path, "loop-shutdown")
    diagnostic_path = root / "diagnostic/qualification_error.json"
    log_path = diagnostic_path.with_name("asyncio-original.log")
    diagnostic = json.loads(diagnostic_path.read_bytes())
    nodeid = signature["nodeid"]
    if change == "other_entry":
        nodeid = diagnostic["nodeid"] = NODE
    elif change == "extra_log":
        log_path.write_bytes(log_path.read_bytes() + b"\nERROR asyncio: unrelated failure\n")
    else:
        replacement = "Task-9999" if change == "ordinal" else "another-worker"
        diagnostic["records"][0]["message"] = diagnostic["records"][0]["message"].replace("Task-105", replacement)
        log_path.write_bytes(log_path.read_bytes().replace(b"Task-105", replacement.encode()))
    diagnostic_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    before = (root / "result.json").read_bytes()
    if change == "other_entry":
        assert literal_reference(diagnostic["records"], nodeid) is None
    classified = classify_known_shutdown(root / "result.json", diagnostic_path, nodeid=nodeid)
    assert (classified is not None) is (change in {"ordinal", "other_entry"})
    if change == "other_entry":
        assert classified["matched_by"] == "reset-family-v1"
    if classified is None:
        assert (root / "result.json").read_bytes() == before


@pytest.mark.parametrize("change", ["reverse", "missing_chain", "extra_chain", "exception_type", "index_sha", "wire_sha", "other_entry"])
def test_pair_shutdown_matches_only_the_two_complete_independent_records(tmp_path, change):
    from copy import deepcopy
    from tests.support.agent_acceptance.shutdown import NODE, classify_known_shutdown, literal_reference

    root, signature = original_case(tmp_path, "generator-pair")
    diagnostic_path = root / "diagnostic/qualification_error.json"
    diagnostic = json.loads(diagnostic_path.read_bytes())
    nodeid = signature["nodeid"]
    if change == "reverse":
        diagnostic["records"].reverse()
    elif change == "missing_chain":
        diagnostic["records"].pop()
    elif change == "extra_chain":
        diagnostic["records"].append(deepcopy(diagnostic["records"][0]))
    elif change == "exception_type":
        diagnostic["records"][0]["traceback"] = diagnostic["records"][0]["traceback"].replace("GeneratorExit", "asyncio.exceptions.CancelledError")
    elif change == "other_entry":
        nodeid = diagnostic["nodeid"] = NODE
    else:
        result = json.loads((root / "result.json").read_bytes())
        case = result["cases"][0]
        index_path = root / case["evidence"]["evidence_index"]
        index = json.loads(index_path.read_bytes())
        if change == "index_sha":
            index["links"]["exchange"]["sha256"] = "0" * 64
        else:
            exchange_path = root / case["evidence"]["exchange"]
            exchange = json.loads(exchange_path.read_bytes())
            exchange["exchanges"][0]["request_utf8"] += " "
            exchange_path.write_text(json.dumps(exchange), encoding="utf-8")
            index["links"]["exchange"].update(sha256=sha256(exchange_path.read_bytes()).hexdigest(), bytes=exchange_path.stat().st_size)
        index_path.write_text(json.dumps(index), encoding="utf-8")
    diagnostic_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    before = (root / "result.json").read_bytes()
    if change == "other_entry":
        assert literal_reference(diagnostic["records"], nodeid) is None
    classified = classify_known_shutdown(root / "result.json", diagnostic_path, nodeid=nodeid)
    assert (classified is not None) is (change in {"reverse", "other_entry"})
    if change == "other_entry":
        assert classified["matched_by"] == "reset-family-v1"
    if classified is None:
        assert (root / "result.json").read_bytes() == before


@pytest.mark.parametrize("variant", ["generator-close", "loop-shutdown", "generator-pair"])
@pytest.mark.parametrize("damage", ["actor", "cancellation", "no_trace", "unrelated_error", "observer_reset",
    "sdk_version", "other_node", "candidate_written", "missing_sdk", "multiple_cases", "source_sha", "frame"])
def test_similar_or_unbound_errors_are_never_classified_as_the_known_failure(tmp_path, damage, variant):
    from copy import deepcopy
    from tests.support.agent_acceptance.shutdown import classify_known_shutdown

    root, signature = original_case(tmp_path, variant)
    result_path = root / "result.json"
    result = json.loads(result_path.read_bytes())
    case = result["cases"][0]
    record_path, exchange_path = (root / case["evidence"][name] for name in ("conversation", "exchange"))
    record, exchange = json.loads(record_path.read_bytes()), json.loads(exchange_path.read_bytes())
    diagnostic_path = root / "diagnostic/qualification_error.json"
    diagnostic = json.loads(diagnostic_path.read_bytes())
    nodeid = signature["nodeid"]
    if damage == "actor":
        record["turns"][0]["actions"][0]["review"]["request_attempts"][0]["actor"] = "main"
        exchange["product_attempts"][1]["actor"] = "main"
        exchange["exchanges"][1]["request_identity"]["actor"] = "main"
        exchange["sdk_inputs"][1]["request_identity"]["actor"] = "main"
        case["facts"]["actions"] = record["turns"][0]["actions"]
    elif damage == "cancellation":
        record["turns"][0]["status"] = "completed"
        case["facts"]["turn_status"] = "completed"
    elif damage == "no_trace":
        diagnostic["records"][0]["traceback"] = ""
    elif damage == "unrelated_error":
        extra = deepcopy(diagnostic["records"][0])
        extra.update(message="Task exception was never retrieved: unrelated failure", traceback="RuntimeError: unrelated failure\n")
        diagnostic["records"].append(extra)
    elif damage == "observer_reset":
        diagnostic["records"][0]["traceback"] += "\nacceptance_sdk_input.reset(token)\nValueError: observer token from another Context\n"
    elif damage == "sdk_version":
        result["version"]["packages"]["pydantic-ai-slim"] = "2.65.0"
    elif damage == "other_node":
        nodeid = diagnostic["nodeid"] = nodeid + "[different-entry]"
    elif damage == "candidate_written":
        record_path.with_name("candidate.txt").write_bytes(b"unexpected-effect")
    elif damage == "missing_sdk":
        exchange["sdk_inputs"].pop()
    elif damage == "source_sha":
        result["version"]["source_files"]["myrk/request_observation.py"] = "0" * 64
    elif damage == "frame":
        diagnostic["records"][0]["traceback"] = diagnostic["records"][0]["traceback"].replace("line 317", "line 316")
    else:
        result["cases"].append(deepcopy(case))
    for path, value in ((result_path, result), (record_path, record), (exchange_path, exchange), (diagnostic_path, diagnostic)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_path = root / case["evidence"]["evidence_index"]
    index = json.loads(index_path.read_bytes())
    for label, path in (("conversation", record_path), ("exchange", exchange_path)):
        index["links"][label].update(sha256=sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    before = result_path.read_bytes()
    assert classify_known_shutdown(result_path, diagnostic_path, nodeid=nodeid) is None
    assert result_path.read_bytes() == before
    assert not list(root.rglob("result-before-async-shutdown.json"))


def test_normal_cancelled_review_can_pass_while_history_stays_open(tmp_path):
    from tests.support.agent_acceptance.shutdown import record_open_history

    root, signature = original_case(tmp_path)
    path = root / "result.json"
    record_open_history(path, nodeid=signature["nodeid"])
    result = json.loads(path.read_bytes())
    assert result["cases"][0]["product_status"] == "passed"
    assert result["qualification"]["status"] == "no_shutdown_error_observed"
    assert result["qualification"]["historical_relation"] == "unknown"


@pytest.mark.parametrize("variant", ["generator-close", "loop-shutdown", "generator-pair"])
@pytest.mark.parametrize("damage", ["optional-step-none", "conflicting-step", "missing-actor", "missing-provider", "extra-field", "extra-observation"])
def test_historical_request_schema_preserves_every_original_field(tmp_path, variant, damage):
    from tests.support.agent_acceptance.shutdown import classify_known_shutdown
    root, signature = original_case(tmp_path, variant)
    result = json.loads((root / "result.json").read_bytes())
    case = result["cases"][0]
    path = root / case["evidence"]["exchange"]
    exchange = json.loads(path.read_bytes())
    attempt = exchange["product_attempts"][0]
    if damage in {"optional-step-none", "conflicting-step"}:
        attempt["step_id"] = None if damage == "optional-step-none" else "not-the-observed-step"
    elif damage == "missing-actor":
        attempt.pop("actor")
    elif damage == "missing-provider":
        attempt.pop("provider")
    elif damage == "extra-field":
        attempt["unknown_request_metadata"] = "must-not-be-ignored"
    else:
        attempt["observation"]["unknown_observation_metadata"] = "must-not-be-ignored"
    path.write_text(json.dumps(exchange), encoding="utf-8")
    index_path = root / case["evidence"]["evidence_index"]
    index = json.loads(index_path.read_bytes())
    index["links"]["exchange"].update(sha256=sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    before = (root / "result.json").read_bytes()
    matched = classify_known_shutdown(root / "result.json", root / "diagnostic/qualification_error.json", nodeid=signature["nodeid"])
    assert (matched is not None) is (damage == "optional-step-none")
    actual = json.loads((root / "result.json").read_bytes())
    if matched is not None:
        assert actual["cases"][0]["product_status"] == "failed"
        assert actual["qualification"]["historical_relation"] == "unknown"
    else:
        assert (root / "result.json").read_bytes() == before
