"""Complete observed reset chains remain bounded product failures."""

from hashlib import sha256
import json
import os
from pathlib import Path
from zipfile import ZipFile

import pytest


MATERIAL = Path(__file__).parent / "materials/shutdown"


@pytest.fixture(scope="session")
def classifier():
    from tests.support.agent_acceptance import shutdown
    return shutdown


def saved_case(tmp_path, kind):
    target = tmp_path / "saved-case"
    target.mkdir()
    if kind in {"direct-event", "direct-product"}:
        reference = (json.loads((MATERIAL / "reset-direct-observation.json").read_bytes()) if kind == "direct-product" else
            next(item for item in json.loads((MATERIAL / "reset-family.json").read_bytes())["provenance"] if item["source_check"] == "b-wq37du17"))
        archive = MATERIAL / reference["archive"]
        expected_sha = reference["archive_sha256"]
    else:
        names = {"literal-three": "signature.json", "literal-loop": "loop-signature.json", "literal-pair": "pair-signature.json"}
        reference = json.loads((MATERIAL / names[kind]).read_bytes())
        archive = MATERIAL / reference["original_archive"]
        expected_sha = reference["original_archive_sha256"]
    assert sha256(archive.read_bytes()).hexdigest() == expected_sha
    with ZipFile(archive) as original:
        for entry in reference["files"]:
            body = original.read(entry["path"])
            assert len(body) == entry["bytes"] and sha256(body).hexdigest() == entry["sha256"]
            path = target / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    nodeid = json.loads((target / "diagnostic/qualification_error.json").read_bytes())["nodeid"]
    return target, nodeid


@pytest.mark.parametrize("kind", ["literal-three", "literal-loop", "literal-pair"])
def test_frozen_literal_observations_still_report_product_failure(tmp_path, classifier, kind):
    root, nodeid = saved_case(tmp_path, kind)
    before = (root / "result.json").read_bytes()
    matched = classifier.classify_known_shutdown(root / "result.json", root / "diagnostic/qualification_error.json", nodeid=nodeid)
    assert matched is not None
    result = json.loads((root / "result.json").read_bytes())
    assert result["cases"][0]["product_status"] == "failed"
    assert result["cases"][0]["expectation_status"] == "review_required"
    assert (root / result["cases"][0]["evidence"]["result_before_async_shutdown"]).read_bytes() == before


def test_complete_fourth_observation_matches_the_same_bounded_reset_family(tmp_path, classifier):
    root, nodeid = saved_case(tmp_path, "direct-event")
    original = (root / "result.json").read_bytes()
    matched = classifier.classify_known_shutdown(root / "result.json", root / "diagnostic/qualification_error.json", nodeid=nodeid)
    assert matched is not None and matched["matched_by"] == "reset-family-v1"
    assert "two SDK" not in matched["current_symptom"]
    result = json.loads((root / "result.json").read_bytes())
    assert result["cases"][0]["product_status"] == "failed" and result["cases"][0]["expectation_status"] == "review_required"
    assert result["qualification"]["historical_relation"] == "unknown" and result["qualification"]["product_fix_claimed"] is False
    assert (root / result["cases"][0]["evidence"]["result_before_async_shutdown"]).read_bytes() == original


def test_complete_fifth_observation_keeps_original_exit1_and_derives_only_product_failure(tmp_path, classifier):
    root, nodeid = saved_case(tmp_path, "direct-product")
    original = (root / "result.json").read_bytes()
    matched = classifier.classify_known_shutdown(root / "result.json", root / "diagnostic/qualification_error.json", nodeid=nodeid)
    assert matched is not None and matched["matched_by"] == "reset-family-v1"
    assert matched["family_match"]["anchor"] == "reset-product-exit"
    actual = json.loads((root / "result.json").read_bytes())
    assert actual["cases"][0]["product_status"] == "failed"
    assert actual["cases"][0]["expectation_status"] == "review_required"
    assert actual["qualification"]["historical_relation"] == "unknown"
    assert (root / actual["cases"][0]["evidence"]["result_before_async_shutdown"]).read_bytes() == original
    manifest = json.loads((MATERIAL / "reset-direct-observation.json").read_bytes())
    assert manifest["original_pytest_exit_code"] == 1 and manifest["product_fix_claimed"] is False


@pytest.mark.parametrize("kind", ["literal-three", "literal-loop", "literal-pair", "direct-event", "direct-product"])
@pytest.mark.parametrize("metadata", ["current_empty", "record_only", "facts_only", "unexpected_change"])
def test_optional_file_change_metadata_preserves_only_consistent_no_effect_observations(tmp_path, classifier, kind, metadata):
    root, nodeid = saved_case(tmp_path, kind)
    result_path = root / "result.json"
    result = json.loads(result_path.read_bytes())
    case = result["cases"][0]
    record_path = root / case["evidence"]["conversation"]
    record = json.loads(record_path.read_bytes())
    action = record["turns"][0]["actions"][0]
    changes = [] if metadata != "unexpected_change" else [{"id": "unexpected-change", "path": action["target"],
        "change_kind": "created", "before_sha256": None, "after_sha256": sha256(b"changed").hexdigest(),
        "size_bytes": 7, "backup_path": None}]
    if metadata != "facts_only":
        action["file_changes"] = changes
    if metadata != "record_only":
        case["facts"]["actions"][0]["file_changes"] = changes
    record_path.write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    result_path.write_bytes((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    index_path = root / case["evidence"]["evidence_index"]
    index = json.loads(index_path.read_bytes())
    index["links"]["conversation"].update(sha256=sha256(record_path.read_bytes()).hexdigest(), bytes=record_path.stat().st_size)
    index_path.write_bytes(json.dumps(index).encode("utf-8"))
    before = result_path.read_bytes()
    matched = classifier.classify_known_shutdown(result_path, root / "diagnostic/qualification_error.json", nodeid=nodeid)
    assert (matched is not None) is (metadata == "current_empty")
    if matched is None:
        assert result_path.read_bytes() == before and not list(root.rglob("result-before-async-shutdown.json"))


RULES = json.loads((MATERIAL / "reset-family.json").read_bytes())


def family_records(anchor_id, auxiliary_ids=(), *, wrapper="asyncgen_close"):
    records = []
    chains = {chain["id"]: chain for chain in [*RULES["anchors"], *RULES["auxiliaries"]]}
    for index, name in enumerate([anchor_id, *auxiliary_ids]):
        chain = chains[name]
        generator = chain["closing_asyncgen"]
        message = (RULES["loop_message"].replace("Task-<ordinal>", "Task-700").replace("0x<address>", "0x1234")
            if index == 0 and wrapper == "loop_shutdown" else
            f"an error occurred during closing of asynchronous generator <async_generator object {generator} at 0x1234>\nasyncgen: <async_generator object {generator} at 0x1234>")
        records.append({**RULES["record_header"], "message": message,
            "traceback": chain["traceback"].replace("0x<address>", "0x1234")})
    return records


def raw_log(records):
    return "\n".join(RULES["log_prefix"] + row["message"] + "\n" + row["traceback"].rstrip("\n") for row in records) + "\n"


@pytest.mark.parametrize("combination", RULES["allowed_combinations"], ids=lambda item: item["anchor"] + "+" + ",".join(item["auxiliaries"]))
@pytest.mark.parametrize("wrapper", ["asyncgen_close", "loop_shutdown"])
@pytest.mark.parametrize("nodeid", RULES["nodes"])
def test_all_twenty_complete_record_combinations_and_only_known_wrappers(classifier, combination, wrapper, nodeid):
    records = family_records(combination["anchor"], combination["auxiliaries"], wrapper=wrapper)
    matched = classifier.match_reset_family(records, raw_log(list(reversed(records))), nodeid)
    anchor = next(item for item in RULES["anchors"] if item["id"] == combination["anchor"])
    if wrapper not in anchor["wrapper_kinds"]:
        assert matched is None
        return
    assert matched is not None and matched["matched_by"] == "reset-family-v1"
    assert matched["anchor"] == combination["anchor"] and matched["auxiliaries"] == combination["auxiliaries"]
    assert matched["log_record_order"] == list(reversed(range(len(records))))


@pytest.mark.parametrize("damage", ["no_anchor", "duplicate_anchor", "duplicate_aux", "too_many", "missing_segment", "swapped_segment",
    "new_frame", "line", "statement", "type", "similar_message", "wrong_asyncgen", "wrong_task", "unknown_record", "extra_log_text", "json_only_missing_aux", "logger"])
@pytest.mark.parametrize("anchor_id", ["reset-event-exit", "reset-product-exit"])
def test_family_rejects_incomplete_or_unknown_chains_and_unconsumed_log(classifier, damage, anchor_id):
    from copy import deepcopy

    records = family_records(anchor_id, ["sdk-open-close", "sdk-event-close"])
    original_log = raw_log(records)
    if damage == "no_anchor":
        records.pop(0)
    elif damage == "duplicate_anchor":
        records = [records[0], deepcopy(records[0])]
    elif damage == "duplicate_aux":
        records = [records[0], records[1], deepcopy(records[1])]
    elif damage == "too_many":
        records.append(deepcopy(records[1]))
    elif damage == "missing_segment":
        lines = records[0]["traceback"].splitlines(keepends=True)
        records[0]["traceback"] = "".join(lines[:1] + lines[3:])
    elif damage == "swapped_segment":
        lines = records[0]["traceback"].splitlines(keepends=True)
        lines[1], lines[3] = lines[3], lines[1]
        records[0]["traceback"] = "".join(lines)
    elif damage == "new_frame":
        records[0]["traceback"] = records[0]["traceback"].replace("ValueError:", '  File "unknown.py", line 9, in unrelated\n    reset()\nValueError:')
    elif damage == "line":
        records[0]["traceback"] = records[0]["traceback"].replace("line 317", "line 318")
    elif damage == "statement":
        records[0]["traceback"] = records[0]["traceback"].replace("_active_capture.reset(token)", "_active_capture.reset(other)")
    elif damage == "type":
        records[0]["traceback"] = records[0]["traceback"].replace("ValueError:", "RuntimeError:")
    elif damage == "similar_message":
        records[0]["traceback"] = records[0]["traceback"].replace("was created in a different Context", "was created in another Context")
    elif damage == "wrong_asyncgen":
        generator = next(item["closing_asyncgen"] for item in RULES["anchors"] if item["id"] == anchor_id)
        records[0]["message"] = records[0]["message"].replace(generator, "Unknown.request_stream")
    elif damage == "wrong_task":
        records = family_records("reset-event-exit", wrapper="loop_shutdown")
        records[0]["message"] = records[0]["message"].replace("Task-700", "named-worker")
    elif damage == "unknown_record":
        records = records[:1] + [{**RULES["record_header"], "message": "unrelated failure", "traceback": "RuntimeError: unknown\n"}]
    elif damage == "logger":
        records[0]["logger"] = "unknown"
    elif damage == "json_only_missing_aux":
        records.pop()
    log = original_log if damage == "json_only_missing_aux" else raw_log(records)
    if damage == "extra_log_text":
        log += "Unknown trailing error text\n"
    assert classifier.match_reset_family(records, log, RULES["nodes"][0]) is None


@pytest.mark.parametrize("kind", ["literal-loop", "literal-pair"])
def test_other_supported_entry_is_a_family_positive_and_still_a_literal_negative(tmp_path, classifier, kind):
    root, _ = saved_case(tmp_path, kind)
    diagnostic_path = root / "diagnostic/qualification_error.json"
    diagnostic = json.loads(diagnostic_path.read_bytes())
    diagnostic["nodeid"] = classifier.NODE
    assert classifier.literal_reference(diagnostic["records"], classifier.NODE) is None
    diagnostic_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    matched = classifier.classify_known_shutdown(root / "result.json", diagnostic_path, nodeid=classifier.NODE)
    assert matched is not None and matched["matched_by"] == "reset-family-v1"
    assert json.loads((root / "result.json").read_bytes())["cases"][0]["product_status"] == "failed"


@pytest.mark.parametrize("consistent_log", [False, True])
def test_missing_independent_aux_requires_a_complete_consistent_original_log(tmp_path, classifier, consistent_log):
    root, nodeid = saved_case(tmp_path, "literal-pair")
    diagnostic_path = root / "diagnostic/qualification_error.json"
    diagnostic = json.loads(diagnostic_path.read_bytes())
    diagnostic["records"].pop()
    assert classifier.literal_reference(diagnostic["records"], nodeid) is None
    diagnostic_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    if consistent_log:
        diagnostic_path.with_name("asyncio-original.log").write_bytes(raw_log(diagnostic["records"]).encode("utf-8"))
    before = (root / "result.json").read_bytes()
    matched = classifier.classify_known_shutdown(root / "result.json", diagnostic_path, nodeid=nodeid)
    assert (matched is not None) is consistent_log
    if matched is not None:
        assert matched["matched_by"] == "reset-family-v1"
    else:
        assert (root / "result.json").read_bytes() == before


@pytest.mark.parametrize("kind", ["literal-three", "literal-loop", "literal-pair", "direct-event", "direct-product"])
@pytest.mark.parametrize("damage", ["actor", "cancellation", "index_sha", "wire_sha", "source_sha", "sdk_version",
    "missing_sdk", "effect", "other_node", "live_mode", "google_protocol", "human_sha", "unknown_record", "different_variable"])
def test_literal_and_family_routes_both_reject_broken_case_or_evidence(tmp_path, classifier, kind, damage):
    root, nodeid = saved_case(tmp_path, kind)
    result_path = root / "result.json"
    result = json.loads(result_path.read_bytes())
    case = result["cases"][0]
    record_path, exchange_path = (root / case["evidence"][name] for name in ("conversation", "exchange"))
    record, exchange = json.loads(record_path.read_bytes()), json.loads(exchange_path.read_bytes())
    diagnostic_path = root / "diagnostic/qualification_error.json"
    diagnostic = json.loads(diagnostic_path.read_bytes())
    if damage == "actor":
        record["turns"][0]["actions"][0]["review"]["request_attempts"][0]["actor"] = "main"
        exchange["product_attempts"][1]["actor"] = "main"
        exchange["exchanges"][1]["request_identity"]["actor"] = "main"
        exchange["sdk_inputs"][1]["request_identity"]["actor"] = "main"
        case["facts"]["actions"] = record["turns"][0]["actions"]
    elif damage == "cancellation":
        record["turns"][0]["status"] = case["facts"]["turn_status"] = "completed"
    elif damage == "wire_sha":
        exchange["exchanges"][0]["request_utf8"] += " "
    elif damage == "source_sha":
        result["version"]["source_files"]["myrk/request_observation.py"] = "0" * 64
    elif damage == "sdk_version":
        result["version"]["packages"]["pydantic-ai-slim"] = "2.65.0"
    elif damage == "missing_sdk":
        exchange["sdk_inputs"].pop()
    elif damage == "effect":
        record_path.with_name("candidate.txt").write_bytes(b"unexpected effect")
    elif damage == "other_node":
        nodeid = diagnostic["nodeid"] = "tests/acceptance/another.py::test_stop"
    elif damage == "live_mode":
        result["mode"] = "live"
    elif damage == "google_protocol":
        case["protocol"] = "google"
    elif damage == "unknown_record":
        diagnostic["records"].append({**RULES["record_header"], "message": "Unrelated runtime failure", "traceback": "RuntimeError: unknown\n"})
    elif damage == "different_variable":
        diagnostic["records"][0]["traceback"] = diagnostic["records"][0]["traceback"].replace("_active_capture.reset(token)", "acceptance_sdk_input.reset(token)")
    for path, data in ((result_path, result), (record_path, record), (exchange_path, exchange), (diagnostic_path, diagnostic)):
        path.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    index_path = root / case["evidence"]["evidence_index"]
    index = json.loads(index_path.read_bytes())
    for label, path in (("conversation", record_path), ("exchange", exchange_path)):
        index["links"][label].update(sha256=sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    if damage == "index_sha":
        index["links"]["exchange"]["sha256"] = "0" * 64
    index_path.write_bytes(json.dumps(index).encode("utf-8"))
    if damage == "human_sha":
        human = root / case["evidence"]["semantic_review"]
        human.write_bytes(human.read_bytes() + b" ")
    diagnostic_path.with_name("asyncio-original.log").write_bytes(raw_log(diagnostic["records"]).encode("utf-8"))
    before = result_path.read_bytes()
    assert classifier.classify_known_shutdown(result_path, diagnostic_path, nodeid=nodeid) is None
    assert result_path.read_bytes() == before and not list(root.rglob("result-before-async-shutdown.json"))


@pytest.mark.parametrize("damage", ["proof_sha", "index_bytes"])
def test_family_requires_all_independent_original_fingerprints_and_sizes(tmp_path, classifier, damage):
    root, nodeid = saved_case(tmp_path, "direct-event")
    result_path = root / "result.json"
    before = result_path.read_bytes()
    case = json.loads(before)["cases"][0]
    index_path = root / case["evidence"]["evidence_index"]
    index = json.loads(index_path.read_bytes())
    if damage == "proof_sha":
        proof = root / case["evidence"]["proof"]
        proof.write_bytes(proof.read_bytes() + b"changed")
    else:
        index["links"]["exchange"]["bytes"] += 1
        index_path.write_bytes(json.dumps(index).encode("utf-8"))
    assert classifier.classify_known_shutdown(result_path, root / "diagnostic/qualification_error.json", nodeid=nodeid) is None
    assert result_path.read_bytes() == before


def test_rule_table_keeps_closed_original_provenance_and_exact_twenty_combinations():
    assert RULES["rule_set"] == "reset-family-v1" and len(RULES["allowed_combinations"]) == 20
    assert len(RULES["anchors"]) == 5 and len(RULES["auxiliaries"]) == 2
    assert RULES["anchors"][-1]["wrapper_kinds"] == ["asyncgen_close"]
    for anchor in RULES["anchors"]:
        assert [item["auxiliaries"] for item in RULES["allowed_combinations"] if item["anchor"] == anchor["id"]] == [
            [], ["sdk-open-close"], ["sdk-event-close"], ["sdk-event-close", "sdk-open-close"]]
    for origin in RULES["provenance"]:
        archive = MATERIAL / origin["archive"]
        assert sha256(archive.read_bytes()).hexdigest() == origin["archive_sha256"]
        if "files" in origin:
            with ZipFile(archive) as originals:
                for entry in origin["files"]:
                    assert set(entry) == {"path", "bytes", "sha256"} and not Path(entry["path"]).is_absolute()
                    body = originals.read(entry["path"])
                    assert len(body) == entry["bytes"] and sha256(body).hexdigest() == entry["sha256"]


@pytest.mark.parametrize("kind", ["direct-event", "direct-product"])
def test_preserved_offline_report_keeps_known_product_failure_separate(classifier, kind):
    reports = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/reset-family-offline-derivation" / kind
    reports.mkdir(parents=True, exist_ok=False)
    root, nodeid = saved_case(reports, kind)
    before = (root / "result.json").read_bytes()
    matched = classifier.classify_known_shutdown(root / "result.json", root / "diagnostic/qualification_error.json", nodeid=nodeid)
    result = json.loads((root / "result.json").read_bytes())
    assert matched is not None and matched["matched_by"] == "reset-family-v1"
    assert result["cases"][0]["product_status"] == "failed" and result["cases"][0]["expectation_status"] == "review_required"
    assert (root / result["cases"][0]["evidence"]["result_before_async_shutdown"]).read_bytes() == before
    (reports / "scope.json").write_bytes((json.dumps({"source_run": "b-m3ff0qql" if kind == "direct-product" else "b-wq37du17", "source_case_teardown": "error_observed",
        "source_run_exit": "not_reclassified_by_candidate", "actual_original_execution_repeated": False,
        "candidate_contract": "passed", "product": "failed", "history": "unknown", "new_model_http_attempts": 0,
        "qualification": matched}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


@pytest.mark.parametrize("kind", ["literal-three", "literal-loop", "literal-pair", "direct-event", "direct-product"])
def test_no_route_can_hide_unknown_unconsumed_original_log_text(tmp_path, classifier, kind):
    root, nodeid = saved_case(tmp_path, kind)
    result_path = root / "result.json"
    before = result_path.read_bytes()
    log_path = root / "diagnostic/asyncio-original.log"
    log_path.write_bytes(log_path.read_bytes() + b"RuntimeError: unknown additional failure without the expected logger prefix\n")
    assert classifier.classify_known_shutdown(result_path, root / "diagnostic/qualification_error.json", nodeid=nodeid) is None
    assert result_path.read_bytes() == before


@pytest.mark.parametrize("kind", ["literal-three", "literal-loop", "literal-pair"])
@pytest.mark.parametrize("damage", ["proof_bytes", "proof_path", "proof_size"])
def test_literal_routes_share_all_original_proof_guards(tmp_path, classifier, kind, damage):
    root, nodeid = saved_case(tmp_path, kind)
    result_path = root / "result.json"
    before = result_path.read_bytes()
    case = json.loads(before)["cases"][0]
    index_path = root / case["evidence"]["evidence_index"]
    index = json.loads(index_path.read_bytes())
    if damage == "proof_bytes":
        proof = root / case["evidence"]["proof"]
        proof.write_bytes(proof.read_bytes() + b"changed independent original")
    elif damage == "proof_path":
        index["links"]["proof"]["path"] = "another-original.txt"
        index_path.write_bytes(json.dumps(index).encode("utf-8"))
    else:
        index["links"]["proof"]["bytes"] += 1
        index_path.write_bytes(json.dumps(index).encode("utf-8"))
    assert classifier.classify_known_shutdown(result_path, root / "diagnostic/qualification_error.json", nodeid=nodeid) is None
    assert result_path.read_bytes() == before
