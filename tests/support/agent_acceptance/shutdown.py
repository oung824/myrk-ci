"""The observed cancelled-review shutdown remains a narrowly matched product failure."""

from collections import Counter
from hashlib import sha256
from importlib.metadata import version
from itertools import permutations
import json
from pathlib import Path
import re
import sys
from typing import Any

from pydantic import ConfigDict, ValidationError
from myrk.conversation import ConversationRecord
from myrk.task_control import RequestAttempt


ISSUE = ".scratch/acceptance-capture-teardown-diagnosis-20261113/issues/01-cross-context-close.md"
EXPECTATION = "success_or_current_known_shutdown_failure_historical_unknown"
REFERENCE = Path(__file__).resolve().parents[2] / "acceptance/materials/shutdown/signature.json"
LOOP_REFERENCE = REFERENCE.with_name("loop-signature.json")
PAIR_REFERENCE = REFERENCE.with_name("pair-signature.json")
FAMILY_REFERENCE = REFERENCE.with_name("reset-family.json")
NODE = "tests/acceptance/test_action_review.py::test_independent_review_preserves_actual_decision_and_failures[stop]"
SELECTED_NODE = "tests/acceptance/test_selected.py::test_selected_plan"
NODES = frozenset({NODE, SELECTED_NODE})


class ShutdownRequestAttempt(RequestAttempt):
    """Receive every saved exchange field; old missing step_id remains unknown."""
    model_config = ConfigDict(extra="forbid")


def _normalized(value: str) -> str:
    value = re.sub(r'File "[^"\n]*[\\/]((?:myrk[\\/]request_observation|pydantic_ai[\\/]models[\\/]_continuation|contextlib)\.py)"',
        lambda match: 'File "' + match[1].replace("\\", "/") + '"', value)
    return re.sub(r"0x[0-9A-Fa-f]+", "0x<address>", value)


def _normalized_loop_shutdown(value: str) -> str:
    return re.sub(r"name='Task-[0-9]+' coro=", "name='Task-<ordinal>' coro=", _normalized(value))


def literal_reference(records: list[dict[str, Any]], nodeid: str) -> dict[str, Any] | None:
    """Keep the three original complete literal record contracts unchanged."""
    if nodeid not in NODES:
        return None
    reference = json.loads(REFERENCE.read_bytes())
    normalize = _normalized
    if nodeid == SELECTED_NODE and len(records) == 1:
        reference = json.loads(LOOP_REFERENCE.read_bytes())
        normalize = _normalized_loop_shutdown
    elif nodeid == SELECTED_NODE and len(records) == 2:
        reference = json.loads(PAIR_REFERENCE.read_bytes())
    signatures = Counter((row.get("phase"), row.get("logger"), row.get("level"),
        normalize(row.get("message", "")), normalize(row.get("traceback", ""))) for row in records)
    expected = Counter((row["phase"], row["logger"], row["level"], row["message"], row["traceback"]) for row in reference["records"])
    return reference if signatures == expected else None


def complete_log_order(records: list[dict[str, Any]], log: str, prefix: str) -> list[int] | None:
    """Match every original byte against at most six complete record permutations."""
    if not 1 <= len(records) <= 3:
        return None
    entries = [prefix + row["message"] + "\n" + row["traceback"].rstrip("\n") for row in records]
    for order in permutations(range(len(entries))):
        if "\n".join(entries[index] for index in order) + "\n" == log:
            return list(order)
    return None


def match_reset_family(records: list[dict[str, Any]], log: str, nodeid: str) -> dict[str, Any] | None:
    """Consume complete observed chains and every raw log byte in a finite rule table."""
    rules = json.loads(FAMILY_REFERENCE.read_bytes())
    if rules["rule_set"] != "reset-family-v1" or nodeid not in rules["nodes"] or not 1 <= len(records) <= rules["maximum_records"]:
        return None
    anchors: list[str] = []
    auxiliaries: list[str] = []
    wrappers: list[str] = []
    chains = [*rules["anchors"], *rules["auxiliaries"]]
    anchor_ids = {item["id"] for item in rules["anchors"]}
    for row in records:
        if (any(row.get(key) != value for key, value in rules["record_header"].items())
                or not isinstance(row.get("message"), str) or not isinstance(row.get("traceback"), str)):
            return None
        matches = [chain for chain in chains if _normalized(row["traceback"]) == chain["traceback"]]
        if len(matches) != 1:
            return None
        chain = matches[0]
        generator = re.escape(chain["closing_asyncgen"])
        closing = re.fullmatch(r"an error occurred during closing of asynchronous generator <async_generator object "
            + generator + r" at (0x[0-9A-Fa-f]+)>\nasyncgen: <async_generator object " + generator + r" at \1>", row["message"])
        if closing and "asyncgen_close" in chain["wrapper_kinds"]:
            wrapper = "asyncgen_close"
        elif ("loop_shutdown" in chain["wrapper_kinds"] and re.search(r"name='Task-[0-9]+' coro=", row["message"])
                and _normalized_loop_shutdown(row["message"]) == rules["loop_message"]):
            wrapper = "loop_shutdown"
        else:
            return None
        (anchors if chain["id"] in anchor_ids else auxiliaries).append(chain["id"])
        wrappers.append(wrapper)
    if (len(anchors) != rules["required_anchors"] or len(set(auxiliaries)) != len(auxiliaries)
            or {"anchor": anchors[0], "auxiliaries": sorted(auxiliaries)} not in rules["allowed_combinations"]):
        return None
    order = complete_log_order(records, log, rules["log_prefix"])
    return None if order is None else {"matched_by": rules["rule_set"], "anchor": anchors[0], "auxiliaries": sorted(auxiliaries),
        "wrappers": wrappers, "log_record_order": order}


def _single_case(result: dict[str, Any], nodeid: str) -> dict[str, Any] | None:
    if (nodeid not in NODES or result.get("mode") != "offline" or len(result.get("cases", [])) != 1):
        return None
    case: dict[str, Any] = result["cases"][0]
    if (case.get("case_id") != "review.stop" or case.get("protocol") != "deepseek"
            or case.get("tool_status") != "passed" or case.get("product_status") != "passed"
            or case.get("semantic_status") != "not_measured" or case.get("expectation_status") != "met"):
        return None
    return case


def record_open_history(result_path: Path, *, nodeid: str) -> None:
    """No observed shutdown error does not close the separately recorded history."""
    result = json.loads(result_path.read_bytes())
    case = _single_case(result, nodeid)
    if case is None:
        return
    case["facts"].update(shutdown_historical_relation="unknown", shutdown_issue=ISSUE)
    result["qualification"] = {"status": "no_shutdown_error_observed", "issue": ISSUE,
        "historical_relation": "unknown", "product_fix_claimed": False}
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def classify_known_shutdown(result_path: Path, diagnostic_path: Path, *, nodeid: str) -> dict[str, Any] | None:
    """Preserve the before-result and raw errors before deriving a failed verdict."""
    original = result_path.read_bytes()
    result = json.loads(original)
    case = _single_case(result, nodeid)
    if case is None or diagnostic_path.name != "qualification_error.json":
        return None
    diagnostic_bytes = diagnostic_path.read_bytes()
    diagnostic = json.loads(diagnostic_bytes)
    records = diagnostic.get("records", [])
    reference = literal_reference(records, nodeid)
    literal = reference is not None
    if reference is None:
        reference = json.loads(REFERENCE.read_bytes())
    if (diagnostic.get("nodeid") != nodeid or diagnostic.get("category") != "unhandled_asyncio_failure"
            or diagnostic.get("original_log") != "asyncio-original.log"
            or result["version"]["packages"] != reference["packages"]
            or any(version(name) != expected for name, expected in reference["packages"].items())
            or result["version"]["python"] != reference["python"] or sys.version != reference["python"]
            or result["version"]["source_files"].get("myrk/request_observation.py") != reference["observed_model_source_sha256"]
            or sha256(Path(__file__).resolve().parents[3].joinpath("myrk/request_observation.py").read_bytes()).hexdigest() != reference["observed_model_source_sha256"]):
        return None
    log_path = diagnostic_path.parent / "asyncio-original.log"
    if not log_path.is_file():
        return None
    log_bytes = log_path.read_bytes()
    log = log_bytes.decode("utf-8")
    family_match = None
    if literal:
        if (len(re.findall(r"^ERROR\s+asyncio:", log, flags=re.MULTILINE)) != len(reference["records"])
                or any(row["message"] not in log or row["traceback"].rstrip("\n") not in log for row in records)):
            return None
        if complete_log_order(records, log, json.loads(FAMILY_REFERENCE.read_bytes())["log_prefix"]) is None:
            return None
    else:
        family_match = match_reset_family(records, log, nodeid)
        if family_match is None:
            return None
    root = result_path.parent.resolve()
    evidence = {label: (root / name).resolve() for label, name in case["evidence"].items()}
    if (not {"conversation", "exchange", "evidence_index", "semantic_review"}.issubset(evidence)
            or any(not path.is_relative_to(root) or not path.is_file() for path in evidence.values())):
        return None
    index = json.loads(evidence["evidence_index"].read_bytes())
    if index.get("run_id") != result["run_id"] or index.get("case_id") != "review.stop" or index.get("protocol") != "deepseek":
        return None
    if (set(index["links"]) != {"conversation", "exchange", "proof", "semantic_review"}
            or any(name not in evidence or (evidence["evidence_index"].parent / link["path"]).resolve() != evidence[name]
                or link["bytes"] != evidence[name].stat().st_size or link["sha256"] != sha256(evidence[name].read_bytes()).hexdigest()
                for name, link in index["links"].items())):
        return None
    human_bytes = evidence["semantic_review"].read_bytes()
    human = json.loads(human_bytes)
    if (human.get("run_id") != result["run_id"] or human.get("case_id") != "review.stop"
            or human.get("protocol") != "deepseek" or human.get("semantic_status") != "not_measured"):
        return None
    record = ConversationRecord.model_validate_json(evidence["conversation"].read_bytes())
    if len(record.turns) != 1:
        return None
    turn = record.turns[0]
    facts = case["facts"]
    if (turn.status != "cancelled" or turn.error is not None or turn.answer != "" or turn.task is None
            or turn.task.total_requests != 2 or turn.task.requests != 2 or turn.task.review_requests != 1
            or turn.task.provider_config.protocol != "deepseek" or len(turn.actions) != 1 or len(turn.request_attempts) != 1
            or any(facts.get(name) != value for name, value in {"turn_status": "cancelled", "error": None,
                "answer": "", "product_attempts": 2, "mock_http_attempts": 2, "main_product_attempts": 2,
                "review_status": "cancelled", "review_requests": 1, "after_stop_attempts": 2,
                "statuses": ["cancelled"], "missing_action_evidence": [], "known_defect": None}.items())):
        return None
    action = turn.actions[0]
    review = action.review
    if (action.kind != "create" or action.status != "cancelled" or action.effect != "not_started"
            or Path(action.target).name != "candidate.txt" or turn.task.scope is None
            or Path(action.target).parent != Path(turn.task.scope.root).parent
            or evidence["conversation"].with_name("candidate.txt").exists() or review is None or review.status != "cancelled"
            or review.attempts != 1 or len(review.request_attempts) != 1
            or action.file_changes or facts.get("actions") != [action.model_dump(mode="json", exclude_unset=True)]):
        return None
    attempts = [*turn.request_attempts, *review.request_attempts]
    identities = [(item.number, item.own_number, item.actor, item.purpose, item.status, item.http_status, item.delegation_id, item.error) for item in attempts]
    if identities != [(1, 1, "main", "work", "completed", None, None, None),
        (2, 2, "action_review", "work", "failed", None, None, "CancelledError（审核服务报告的错误种类）")]:
        return None
    exchange = json.loads(evidence["exchange"].read_bytes())
    raw_attempts = exchange.get("product_attempts")
    if not isinstance(raw_attempts, list) or len(raw_attempts) != len(attempts):
        return None
    try:
        received_attempts = [ShutdownRequestAttempt.model_validate(item, strict=True) for item in raw_attempts]
    except ValidationError:
        return None
    if any(received.model_dump(mode="json", exclude_unset=True) != raw
            or received.model_fields_set - {"step_id"} != expected.model_fields_set - {"step_id"}
            or received.model_dump(mode="json") != expected.model_dump(mode="json")
            for received, raw, expected in zip(received_attempts, raw_attempts, attempts, strict=True)):
        return None
    if (exchange.get("run_id") != result["run_id"] or exchange.get("case_id") != "review.stop"
            or exchange.get("protocol") != "deepseek" or exchange.get("task_id") != turn.task.id
            or len(exchange.get("exchanges", [])) != 2 or len(exchange.get("sdk_inputs", [])) != 2):
        return None
    sdk = {item["sdk_input_id"]: item for item in exchange["sdk_inputs"]}
    if len(sdk) != 2:
        return None
    for request, attempt in zip(exchange["exchanges"], attempts, strict=True):
        identity = {name: getattr(attempt, name) for name in ("number", "actor", "purpose", "delegation_id")}
        captured = sdk.get(request.get("sdk_input_id"))
        wire = attempt.observation.wire if attempt.observation is not None else None
        if (captured is None or set(captured) != {"messages", "settings", "parameters", "sdk_input_id", "request_identity"}
                or not captured["messages"] or not captured["parameters"] or captured["request_identity"] != identity
                or request.get("request_identity") != identity or request.get("status") != 200 or request.get("error") is not None
                or not request.get("received_utf8") or wire is None
                or wire.body_sha256 != sha256(request["request_utf8"].encode()).hexdigest()):
            return None
    coverage = [row for row in result["coverage"] if row["case_id"] == "review.stop" and row["protocol"] == "deepseek"]
    if len(coverage) != 1 or coverage[0]["offline"] != "passed" or coverage[0]["execution"] != "passed":
        return None
    case_root = evidence["conversation"].parent
    before = case_root / "result-before-async-shutdown.json"
    copied = case_root / ("async-shutdown-" + diagnostic_path.parent.name)
    if before.exists() or copied.exists():
        return None
    with before.open("xb") as output:
        output.write(original)
    copied.mkdir()
    (copied / diagnostic_path.name).write_bytes(diagnostic_bytes)
    (copied / log_path.name).write_bytes(log_bytes)
    (copied / "human-review-before-shutdown.json").write_bytes(human_bytes)
    added = {"result_before_async_shutdown": before, "async_shutdown_diagnostic": copied / diagnostic_path.name,
             "async_shutdown_original_log": copied / log_path.name,
             "human_review_before_async_shutdown": copied / "human-review-before-shutdown.json"}
    for name, path in added.items():
        case["evidence"][name] = str(path.relative_to(root))
        index["links"][name] = {"path": str(path.relative_to(case_root)), "sha256": sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    case.update(product_status="failed", tool_status="passed", expectation_status="review_required", expectation=EXPECTATION)
    case["facts"].update(shutdown_known_failure_matched=True, shutdown_issue=ISSUE, shutdown_historical_relation="unknown")
    coverage[0].update(offline="failed", execution="failed", expectation=EXPECTATION)
    variant = family_match["matched_by"] if family_match is not None else reference.get("variant", "generator-close-three-errors")
    symptom = ("cancelled independent review; single asyncio.run shutdown CancelledError through SDK continuation and original Myrk context reset"
        if variant == "loop-shutdown-single-cancelled-error" else
        "cancelled independent review; two exact SDK GeneratorExit records including original Myrk context reset and generator-close RuntimeError"
        if variant == "generator-close-two-errors" else "cancelled independent review; original Myrk context reset and two SDK generator-close failures")
    if family_match is not None:
        symptom = "cancelled independent review; exact known product context-reset family; auxiliary cleanup recorded separately"
    qualification = {"status": "known_product_failure", "issue": ISSUE, "current_symptom": symptom, "variant": variant,
        "matched_by": "literal" if family_match is None else family_match["matched_by"], "family_match": family_match,
        "historical_relation": "unknown", "product_fix_claimed": False,
        "original_result": {"path": case["evidence"]["result_before_async_shutdown"], "sha256": sha256(original).hexdigest()},
        "diagnostic": {"path": case["evidence"]["async_shutdown_diagnostic"], "sha256": sha256(diagnostic_bytes).hexdigest()}}
    result["qualification"] = qualification
    human.update(program_product_status="failed", qualification=qualification)
    evidence["semantic_review"].write_text(json.dumps(human, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index["links"]["semantic_review"].update(sha256=sha256(evidence["semantic_review"].read_bytes()).hexdigest(),
        bytes=evidence["semantic_review"].stat().st_size)
    evidence["evidence_index"].write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return qualification
