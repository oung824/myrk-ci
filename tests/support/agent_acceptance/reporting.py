"""Evidence association, static verdicts, and human review material."""

from collections import Counter
from collections.abc import Sequence
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import zipfile
from pathlib import Path
import platform
import sys
from typing import Any, TYPE_CHECKING

from tests.support.agent_acceptance.core import AcceptanceCase, AcceptancePlan, CaseContext, CaseOutcome, CaseRegistry, IncompleteEvidenceError, SharedControlError, StopViolation
from tests.support.agent_acceptance.recorded_response import response_evidence
from tests.support.agent_acceptance.question_contracts import CRITERIA_VERSION
from tests.support.agent_acceptance.frozen import source_material_path
from tests.support.agent_acceptance.stop_request_evidence import (
    IDENTITY_FIELDS, TERMINAL_STATES, StopExchange, StopRequestEvidence, StopSavedRequest, StopSdkInput,
    reservation_identity_conflicts, stop_reservations,
)


if TYPE_CHECKING:
    from tests.support.agent_acceptance.preparation import LiveAuthorization, _LiveLedger


def verify_formal_accounting_archive(root: Path, *, grant: "LiveAuthorization", execution_id: str | None) -> "_LiveLedger":
    """Verify the complete freeze chain and the grant active at each original reservation."""
    from tests.support.agent_acceptance.preparation import _LiveLedger, LiveAuthorization, read_prepared_archive, digest
    archive = root / "accounting.zip"
    prepared = read_prepared_archive(archive, binding=grant.binding)
    with zipfile.ZipFile(archive) as original_files:
        ledger = _LiveLedger.model_validate_json(original_files.read("ledger.json"))
    original = ledger.authorization
    if (original is None or original.binding != ledger.binding
            or ledger.authorization_binding != digest(original.model_dump(mode="json"))
            or execution_id is None or ledger.active_candidates.get(execution_id, ledger.binding) != grant.binding):
        raise SharedControlError("Formal archived active candidate grant differs.")
    allowed = {original.authorization_id: original}
    allowed.update({item["grant"]["authorization_id"]: LiveAuthorization.model_validate(item["grant"]) for item in ledger.candidates})
    if allowed.get(grant.authorization_id) != grant:
        raise SharedControlError("Formal archived candidate grant original changed.")
    rows = {item.execution_id: item for item in prepared.plan.executions}
    for number, attempt in enumerate(ledger.attempts, 1):
        current = original
        for candidate in ledger.candidates:
            if candidate["before_request"] <= number and attempt.execution_id in candidate["affected_execution_ids"]:
                current = LiveAuthorization.model_validate(candidate["grant"])
        row = rows.get(attempt.execution_id)
        if (attempt.number != number or attempt.authorization_id != current.authorization_id or row is None
                or (attempt.case_id, attempt.protocol, attempt.role) != (row.case_id, row.protocol, row.role)):
            raise SharedControlError("Historical reservation differs from its then-active execution grant.")
    return ledger


def environment_facts() -> dict[str, Any]:
    packages: dict[str, str] = {}
    for name in ("pydantic-ai-slim", "pydantic", "pydantic-core", "openai", "google-genai", "httpx2", "genai-prices", "pytest"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not_installed"
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(), "packages": packages}


def version_facts(plan: AcceptancePlan, registry: CaseRegistry) -> dict[str, Any]:
    from tests.support.agent_acceptance.preparation import bound_files

    return {"source_files": bound_files(plan, registry), **environment_facts(),
        "catalogue": json.loads(json.dumps(registry.catalogue(), ensure_ascii=False, default=str)),
        "plan": plan.model_dump(mode="json"), "plan_sha256": sha256(plan.model_dump_json().encode("utf-8")).hexdigest(),
        "source_identity": "actual consumed file bytes, including staged checkout contents; not a Git HEAD claim"}


def assess(case: AcceptanceCase, outcome: CaseOutcome) -> None:
    outcome.expectation = case.expectation
    facts = outcome.facts
    if outcome.product_status == "inconclusive" and facts.get("fact_contract_status") == "unknown" and outcome.tool_status == "passed":
        outcome.expectation_status = "review_required"
        return
    if outcome.product_status == "not_executed":
        outcome.expectation_status = "not_executed"
        return
    if case.expectation == "planned_record_obstruction":
        expected_error = "[Errno 13] Permission denied: " + repr(str(outcome.evidence["conversation"].with_suffix(".json.tmp")))
        met = (outcome.product_status == "failed" and facts.get("planned_record_obstruction") is True
            and facts.get("record_write_error_type") == "PermissionError" and bool(facts.get("save_failures"))
            and facts.get("record_write_error") == expected_error
            and outcome.evidence["conversation"].with_suffix(".json.tmp").is_dir()
            and facts.get("turn_status") == "failed" and facts.get("product_attempts") == facts.get("mock_http_attempts") == 0)
    elif case.expectation == "planned_missing_receipt":
        receipt = json.loads(outcome.evidence["removed_receipt"].read_bytes()) if "removed_receipt" in outcome.evidence else {}
        met = (outcome.product_status == "failed" and "removed_receipt" in outcome.evidence
            and any(item["field"] == "receipt" and item["required"] and item["action_id"] == receipt.get("action_id")
                for item in facts.get("missing_action_evidence", []))
            and any(action["id"] == receipt.get("action_id") and action["kind"] == "check" and action["status"] == "completed"
                and action["exit_code"] == receipt.get("exit_code") == 0 and action["cleanup_complete"] is True
                for action in facts.get("actions", [])))
    elif case.expectation == "planned_false_acceptance":
        criteria_path = Path(__file__).resolve().parents[2] / "acceptance/materials/delegation/criteria.json"
        criteria = json.loads(criteria_path.read_bytes())
        met = (outcome.product_status == "failed" and facts.get("artifact_contract") == "passed"
            and facts.get("answer_fact_contract") == "failed" and facts.get("delivery_status") == "awaiting_acceptance"
            and str(facts.get("answer", "")).startswith(criteria["false_main_answer"])
            and any(item["actor"] == "delegation" and item["text"] == criteria["false_child_answer"] for item in facts.get("answer_reviews", []))
            and facts.get("scripted_claims") == {"main_user_accepted": True, "child_user_accepted": True})
    elif case.expectation == "success_or_evidenced_historical_save_failure":
        if (case.case_id != "review.service-retry" or outcome.protocol == "deepseek") and facts.get("known_defect") is not None:
            met = (outcome.product_status == "failed" and facts["known_defect"].get("matched_public_symptom") is True
                and facts["known_defect"].get("id") == "conversation-progress-save-failure" and outcome.tool_status == "passed")
            outcome.expectation_status = "review_required" if met else "unmet"
            return
        met = outcome.product_status == "passed"
    elif case.expectation == "success_or_current_offline_formal_google_send_save_failure":
        from tests.support.agent_acceptance.formal_save import known_failure
        if (case.case_id == "formal.send" and outcome.protocol == "google" and facts.get("mode") == "offline"
                and outcome.product_status == "failed" and outcome.tool_status == "passed"
                and known_failure(facts.get("current_save_failure"))):
            outcome.expectation_status = "review_required"
            return
        met = outcome.product_status == "passed"
    elif case.expectation == "inconclusive_step_identity":
        met = facts.get("known_failure_matched") is True and outcome.tool_status == "passed"
        outcome.expectation_status = "review_required" if met else "unmet"
        return
    else:
        met = outcome.product_status == "passed"
    outcome.expectation_status = "met" if met and outcome.tool_status == "passed" else "unmet"


def observed_attempts(context: CaseContext, outcome: CaseOutcome, envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify uncommitted observations against their actual independent original."""
    attempts = list(envelope.get("product_attempts", []))
    for pending in envelope.get("pending_observations", []):
        binding = outcome.facts.get("pending_evidence_bindings", {}).get(pending["source_path"])
        if binding is None or pending["committed"] is not False or pending["source_kind"] != "uncommitted_pending_record":
            raise SharedControlError("Pending request observation lacks an explicit uncommitted source binding.")
        source = (context.root / binding).resolve()
        if not source.is_relative_to(context.root.resolve()) or not source.is_file() or sha256(source.read_bytes()).hexdigest() != pending["source_sha256"]:
            raise SharedControlError("Pending request observation differs from its preserved original bytes.")
        record = json.loads(source.read_bytes())
        actual = [{"task_id": turn["task"]["id"], **attempt} for turn in record["turns"] if turn.get("task")
            and turn["task"]["id"] == pending["task_id"] for attempt in [*turn.get("request_attempts", []),
                *(item for action in turn.get("actions", []) if action.get("review") for item in action["review"].get("request_attempts", []))]]
        if actual != pending["attempts"]:
            raise SharedControlError("Pending request ownership differs from the actual uncommitted record.")
        attempts.extend({**attempt, "record_stage": "uncommitted_pending_record"} for attempt in actual)
    return attempts


def publish_evidence(context: CaseContext, outcome: CaseOutcome, case: AcceptanceCase) -> None:
    try:
        outcome.facts.setdefault("source_run_id",context.run_id)
        if context.execution is not None:
            material = context.root / "frozen-material.original.txt"
            material.write_text(context.material_input,encoding="utf-8")
            outcome.evidence["frozen_material"] = material
            for alias in context.execution.shared_with:
                criteria = context.root / "shared-quality-criteria.json"
                criteria.write_bytes(source_material_path(alias.criteria_path).read_bytes())
                outcome.evidence["shared_criteria"] = criteria
            outcome.facts["frozen_execution"] = context.execution.model_dump(mode="json")
        if context.live_gate is not None:
            directory = context.root / "budget"
            directory.mkdir(exist_ok=False)
            for name in ("prepared", "ledger"):
                path = directory / (name + ".json")
                path.write_bytes((context.live_gate.root / (name + ".json")).read_bytes())
                outcome.evidence["budget." + name] = path
            report = context.live_gate.accounting_report()
            outcome.facts["budget"] = {**report, **report["case_stops"].get(context.live_gate.stop_key(context.case_id, context.protocol), {})}
            outcome.execution_id = context.live_gate.execution_id(context.case_id, context.protocol)
        validate_evidence(context, outcome)
        if not outcome.execution_id:
            outcome.execution_id = f"{context.run_id}:{context.case_id}:{context.protocol}:1"
        if case.screening and (context.execution is None or context.execution.stage == "screening"):
            selected = case.screening.get(context.protocol, next(iter(case.screening.values())))
            outcome.screening = {**selected, "planned_identity": context.case_id,
                "protocol_in_initial_screening": context.protocol in case.screening,
                "sending_authorized": False, "criteria_version": selected.get("criteria_version", CRITERIA_VERSION)}
        if context.case_id in {"formal.question", "formal.attachment"}:
            outcome.deterministic_status = outcome.facts["deterministic_status"]
            outcome.target_status = "passed" if outcome.product_status == "passed" else "pending_review" if outcome.product_status == "inconclusive" else "failed"
        outcome.evidence_status = "complete"
        if context.execution is not None and context.execution.role == "judge":
            path = context.root / "judge-call-boundary.json"
            path.write_text(json.dumps({"execution_id":context.execution_id,"role":"judge",
                "purpose":context.execution.judge_purpose,"semantic_status":"not_measured",
                "judge_qualification":"not_measured","provenance":"fixed_fixture",
                "sdk_exchange_originals":{name:str(value) for name,value in outcome.evidence.items() if "exchange" in name}},indent=2)+"\n",encoding="utf-8")
            outcome.evidence["judge_boundary"] = path
        else:
            write_semantic_review(context, outcome, case)
        index = context.root / "evidence-index.json"
        index.write_text(json.dumps({"run_id": context.run_id, "case_id": context.case_id, "protocol": context.protocol,
            "execution_id":outcome.execution_id,
            "links": {label: {"path": str(path.relative_to(context.root)) if path.is_relative_to(context.root) else str(path),
                "sha256": sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                for label, path in outcome.evidence.items() if label != "evidence_index"}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outcome.evidence["evidence_index"] = index
    except (OSError, ValueError, TypeError, KeyError, StopIteration) as error:
        raise SharedControlError(f"Evidence association cannot be published: {type(error).__name__}: {error}") from error


def validate_exchange_header(context: CaseContext, label: str, value: dict[str, Any], *, expected_protocol: str | None = None) -> bool:
    header = {"run_id": context.run_id, "case_id": context.case_id, "protocol": expected_protocol or context.protocol}
    stop_case = context.case_id == "quality.qg3.formal.9"
    if any(value.get(key) != expected for key, expected in header.items() if not stop_case or key in value):
        raise SharedControlError(f"SDK/HTTP evidence association changed: {label}")
    return header.keys() <= value.keys()


def _validate_stop_exchange(label: str, exchange: StopExchange,
                            captured_attempts: list[StopSavedRequest]) -> tuple[list[str], dict[str, StopSdkInput]]:
    identity_fields = set(IDENTITY_FIELDS)
    incomplete = [f"Required SDK/HTTP evidence has incomplete {field}: {label}" for field in exchange.missing_sections]
    sdk_inputs: dict[str, StopSdkInput] = {}
    ordinals: set[int] = set()

    def check_product(identity: dict[str, str | int | None], request: StopRequestEvidence | None = None) -> None:
        number = identity.get("number")
        step = request.step_id if request is not None else None
        fingerprint = request.body_sha256 if request is not None else None
        candidates = []
        for attempt in captured_attempts:
            if number is None or attempt.identity.get("number") != number:
                continue
            if exchange.task_id is not None and attempt.task_id not in (None, exchange.task_id):
                if step is not None and step == attempt.step_id:
                    raise SharedControlError(f"SDK/HTTP product request task changed: {label}")
                continue
            candidates.append(attempt)
        if not candidates:
            incomplete.append(f"SDK/HTTP evidence is missing its product attempt: {label}")
            return
        if len(candidates) != 1:
            raise SharedControlError(f"SDK/HTTP evidence has ambiguous product request ownership: {label}")
        attempt = candidates[0]
        conflict = attempt.conflict(identity, step, fingerprint)
        if conflict is not None:
            raise SharedControlError(f"HTTP evidence product request {conflict} changed: {label}")
        if (not identity_fields <= attempt.identity.keys()
                or step is not None and attempt.step_id is None
                or request is not None and attempt.body_sha256 is None):
            incomplete.append(f"SDK/HTTP evidence is missing its product attempt or wire observation: {label}")

    for sdk in exchange.sdk_inputs:
        if sdk.sdk_id is None or not identity_fields <= sdk.identity.keys():
            incomplete.append(f"Required evidence has an incomplete SDK input: {label}")
        if sdk.sdk_id is not None:
            if sdk.sdk_id in sdk_inputs:
                raise SharedControlError(f"SDK/HTTP evidence repeats an SDK input ID: {label}")
            sdk_inputs[sdk.sdk_id] = sdk
        check_product(sdk.identity)

    for request in exchange.requests:
        if exchange.stop_snapshot is not None and exchange.stop_snapshot.conflicts_with(request):
            raise SharedControlError(f"HTTP evidence conflicts with the stop snapshot: {label}")
        if request.ordinal is None:
            incomplete.append(f"HTTP evidence has no attempt number: {label}")
        elif request.ordinal in ordinals:
            raise SharedControlError(f"SDK/HTTP evidence repeats a terminal attempt number: {label}")
        else:
            ordinals.add(request.ordinal)
        if request.malformed_not_sent:
            incomplete.append(f"HTTP evidence has a malformed no-send flag: {label}")
        if request.not_sent and request.terminal_started is True:
            raise SharedControlError(f"HTTP evidence claims both terminal send and no send: {label}")
        if not request.not_sent and (request.terminal_started is not True or request.step_id is None):
            incomplete.append(f"HTTP evidence has no terminal start or product step: {label}")
        if not identity_fields <= request.identity.keys():
            incomplete.append(f"HTTP evidence has an incomplete request identity: {label}")
        if request.body_sha256 is None:
            incomplete.append(f"HTTP evidence has no request body: {label}")
        captured_sdk = sdk_inputs.get(request.sdk_id) if request.sdk_id is not None else None
        if captured_sdk is None:
            if (request.sdk_id is not None and identity_fields <= request.identity.keys()
                    and any(item.identity == request.identity for item in sdk_inputs.values())):
                raise SharedControlError(f"HTTP evidence has a different SDK input ID: {label}")
            incomplete.append(f"HTTP evidence is missing its SDK input: {label}")
        elif any(captured_sdk.identity[key] != field for key, field in request.identity.items() if key in captured_sdk.identity):
            raise SharedControlError(f"HTTP evidence lacks its unique complete SDK input: {label}")
        identity = {**captured_sdk.identity, **request.identity} if captured_sdk is not None else request.identity
        check_product(identity, request)
    return incomplete, sdk_inputs


def _validate_stop_reservations(context: CaseContext, ledger: "_LiveLedger",
                                exchanges: list[tuple[StopExchange, dict[str, StopSdkInput]]]) -> list[str]:
    assert context.live_gate is not None
    bound = stop_reservations(context, ledger)
    by_sdk = {item.sdk_input_id: item for item in bound.values() if item.sdk_input_id is not None}
    observed: set[int] = set()
    incomplete = []
    for exchange, sdk_inputs in exchanges:
        snapshot = exchange.stop_snapshot
        stopped_sdk_ids = set(snapshot.sdk_by_ordinal.values()) if snapshot is not None else set()
        sdk_count_by_product = Counter(sdk.identity.get("number") for sdk in exchange.sdk_inputs)
        for sdk_id, sdk_input in sdk_inputs.items():
            item = by_sdk.get(sdk_id)
            if item is not None and reservation_identity_conflicts(sdk_input.identity, item):
                raise SharedControlError("Gated SDK input and reservation identity changed.")
            product_number = sdk_input.identity.get("number")
            if (item is None and set(IDENTITY_FIELDS) <= sdk_input.identity.keys()
                    and sdk_count_by_product[product_number] == 1):
                candidates = [reservation for reservation in bound.values()
                              if reservation.role == "product" and reservation.product_number == product_number]
                if len(candidates) == 1:
                    reserved_sdk_id = candidates[0].sdk_input_id
                    if (reserved_sdk_id is not None and (sdk_id in stopped_sdk_ids or reserved_sdk_id in stopped_sdk_ids)
                            and not reservation_identity_conflicts(sdk_input.identity, candidates[0])):
                        raise SharedControlError("Gated SDK input ID differs from its unique stopped reservation.")
        for sdk_id in stopped_sdk_ids:
            item = by_sdk.get(sdk_id)
            if item is not None and item.state not in TERMINAL_STATES:
                raise SharedControlError("Gated terminal state changed against the stop snapshot.")
        for request in exchange.requests:
            number = request.ledger_number
            if number is None:
                incomplete.append("HTTP evidence is missing its reservation number.")
                item = by_sdk.get(request.sdk_id) if request.sdk_id is not None else None
                if item is None:
                    continue
            else:
                if number in observed or number not in bound:
                    raise SharedControlError("Gated SDK/HTTP observations and reservations are not one-to-one.")
                observed.add(number)
                item = bound[number]
            if snapshot is not None and snapshot.conflicts_with(request, item.sdk_input_id):
                raise SharedControlError("HTTP evidence conflicts with the stop snapshot and bound reservation.")
            sdk = sdk_inputs.get(request.sdk_id) if request.sdk_id is not None else None
            if (request.conflicts_with(item, sdk.identity if sdk is not None else None)
                    or item.state == "not_sent" and item.fault_origin != request.original.get("fault_origin")
                    or item.fault_origin == "accepted durable stop before terminal HTTP" and not (ledger.stopped or
                        context.live_gate.stop_key(context.case_id, context.protocol) in ledger.case_stops)):
                raise SharedControlError("Gated request identity or terminal state changed.")
            if (item.product_number is None or item.sdk_input_id is None or item.request_sha256 is None
                    or request.terminal_started is True and item.product_step_id is None):
                incomplete.append(f"HTTP evidence has an incomplete reservation {item.number}.")
    incomplete.extend(f"HTTP evidence is missing for reservation {number}." for number in sorted(bound.keys() - observed))
    return incomplete


def validate_evidence(context: CaseContext, outcome: CaseOutcome, *, missing_originals: Sequence[str] = ()) -> None:
    from myrk.checks import CheckReceipt

    stop_case = context.case_id == "quality.qg3.formal.9"
    incomplete = list(missing_originals)

    def missing(message: str) -> None:
        if not stop_case:
            raise IncompleteEvidenceError(message)
        incomplete.append(message)

    def relocated(value: str) -> Path:
        path = Path(value)
        for original, actual in outcome.facts.get("evidence_relocation", {}).items():
            if path.is_relative_to(original):
                base = Path(actual)
                return (base if base.is_absolute() else context.root / base) / path.relative_to(original)
        return path

    actions = [*outcome.facts.get("actions", []), *outcome.facts.get("all_actions", [])]
    for action in actions:
        if action.get("receipt_path") is None or action.get("status") != "completed":
            continue
        path = relocated(action["receipt_path"])
        if not path.is_file():
            if context.case_id != "actions.check-missing-receipt":
                missing(f"Required action receipt is missing: {action['id']}: {path}")
            continue
        try:
            receipt = CheckReceipt.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise SharedControlError(f"Required action receipt cannot be decoded: {action['id']}: {error}") from error
        if receipt.action_id != action["id"] or receipt.exit_code != action["exit_code"]:
            raise SharedControlError(f"Action and independent exit receipt disagree: {action['id']}")

    def validate_not_sent(request: dict[str, Any], label: str) -> None:
        if (not (
                context.case_id == "formal.restart" and request.get("fault_origin") == "planned local pre-send failure after the real completed check"
                or context.case_id in {"formal.recovery-family", "quality.qg5.formal.8"} and request.get("fault_origin") == "planned local pre-send failure after completed child"
                or context.mode == "offline" and context.live_gate is not None and context.live_gate.authorization is not None
                and context.live_gate.authorization.transport == "mock" and request.get("fault_origin") == "planned local pre-send failure from fixed_fixture"
                or context.live_gate is not None and request.get("fault_origin") == "accepted durable stop before terminal HTTP")
                or request.get("status") is not None
                or (request.get("received_utf8") if stop_case else request["received_utf8"])):
            raise SharedControlError("A not-sent request does not match the static controlled local transport fault.")
        if stop_case and not isinstance(request.get("received_utf8"), str):
            missing(f"HTTP evidence is missing its recorded empty response: {label}")

    http_rows: dict[str, dict[str, Any]] = {}
    stop_exchanges: list[tuple[StopExchange, dict[str, StopSdkInput]]] = []
    stop_originals: set[Path] = set()
    product_ids: set[tuple[Any, ...]] = set()
    sdk_ids: set[str] = set()
    envelope_count = 0
    budget_ledger = None
    for label, path in outcome.evidence.items():
        if not path.is_file():
            missing(f"Required evidence link is missing: {label}: {path}")
            continue
        if label == "budget.ledger":
            budget_ledger = path
        if (path.name in {"stderr.txt", "stderr.log", "runner.stderr"} or label.endswith("runner.stderr")) and not path.parent.name.endswith("-checks"):
            diagnostic = path.read_text(encoding="utf-8", errors="replace")
            markers = ("an error occurred during closing of asynchronous generator", "unhandled exception during asyncio.run() shutdown",
                "Task exception was never retrieved", "Exception ignored in: <async_generator")
            if any(marker in diagnostic for marker in markers):
                raise SharedControlError(f"Unhandled Python runtime/async-generator shutdown error; original stderr: {path}")
        if "exchange" not in label or path.suffix != ".json":
            continue
        if stop_case:
            if path in stop_originals:
                continue
            stop_originals.add(path)
        try:
            value = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            if stop_case:
                missing(f"Required evidence cannot be read: {label}: {error}")
                continue
            raise SharedControlError(f"Required evidence cannot be read: {label}: {error}") from error
        if not isinstance(value, dict) or (not stop_case and "exchanges" not in value):
            if stop_case:
                missing(f"Required evidence has no SDK/HTTP envelope: {label}")
            continue
        envelope_count += 1
        expected_protocol = outcome.facts["source"]["protocol"] if context.case_id == "provider.recipient-switch" and label == "original_exchange" else context.protocol
        if not validate_exchange_header(context, label, value, expected_protocol=expected_protocol):
            missing(f"Required SDK/HTTP evidence header is incomplete: {label}")
        if stop_case:
            for field in ("product_attempts", "pending_observations"):
                items = value.get(field, [] if field == "pending_observations" else None)
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    missing(f"Required SDK/HTTP evidence has incomplete {field}: {label}")
                    value[field] = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        attempts = observed_attempts(context, outcome, value)
        if stop_case:
            stop_exchange = StopExchange.from_original(value)
            captured_attempts = [StopSavedRequest.from_original(attempt) for attempt in attempts]
            product_ids.update((item.task_id or stop_exchange.task_id, *(item.identity.get(key) for key in IDENTITY_FIELDS))
                               for item in captured_attempts)
            gaps, stop_sdk_inputs = _validate_stop_exchange(label, stop_exchange, captured_attempts)
            stop_exchanges.append((stop_exchange, stop_sdk_inputs))
            incomplete.extend(gaps)
            sdk_ids.update(stop_sdk_inputs)
            for stop_request in stop_exchange.requests:
                if stop_request.not_sent:
                    validate_not_sent(stop_request.original, label)
                if stop_request.sdk_id is not None:
                    if stop_request.sdk_id in http_rows and http_rows[stop_request.sdk_id] != stop_request.original:
                        raise SharedControlError("One SDK input is linked to inconsistent terminal observations.")
                    http_rows[stop_request.sdk_id] = stop_request.original
            continue
        product_ids.update(tuple(attempt.get(name, value.get("task_id") if name == "task_id" else None)
            for name in ("task_id", "number", "actor", "purpose", "delegation_id")) for attempt in attempts)
        requests = value["exchanges"]
        sdk_inputs = value.get("sdk_inputs", [])
        sdk_ids.update(item["sdk_input_id"] for item in sdk_inputs)
        judge = context.execution is not None and context.execution.role == "judge"
        for request in requests:
            fingerprint = sha256(request["request_utf8"].encode()).hexdigest()
            matches = [attempt for attempt in attempts if (attempt.get("observation") or {}).get("wire", {})
                and attempt["observation"]["wire"]["body_sha256"] == fingerprint]
            if not matches and not judge:
                raise SharedControlError(f"HTTP evidence has no product attempt: {label}")
            identity = request.get("request_identity")
            sdk_id = request.get("sdk_input_id")
            sdk = [item for item in sdk_inputs if item.get("sdk_input_id") == sdk_id]
            if sdk_id is None or len(sdk) != 1 or sdk[0].get("request_identity") != identity:
                raise SharedControlError(f"HTTP evidence lacks its unique complete SDK input: {label}")
            if not judge and identity is not None and not any(all(attempt.get(key) == item for key, item in identity.items()) for attempt in matches):
                raise SharedControlError(f"HTTP request ownership changed: {label}")
            if request.get("not_sent"):
                validate_not_sent(request, label)
            if sdk_id in http_rows and http_rows[sdk_id] != request:
                raise SharedControlError("One SDK input is linked to inconsistent terminal observations.")
            http_rows[sdk_id] = request
        for sdk in sdk_inputs:
            identity = sdk.get("request_identity")
            if not judge and identity is not None and not any(
                all(attempt.get(key) == item for key, item in identity.items()) for attempt in attempts
            ):
                raise SharedControlError(f"SDK request ownership changed: {label}")
    if stop_case:
        stop_requests = [row for exchange, _ in stop_exchanges for row in exchange.requests]
        sent = [row for row in stop_requests if row.terminal_started is True and not row.not_sent]
        actual_http = len(sent)
        real_http = sum(row.original.get("transport_provenance") == "real_http" for row in sent)
        mock_http = sum(row.original.get("transport_provenance") == "fixed_fixture" for row in sent)
        not_sent_count = sum(row.not_sent for row in stop_requests)
    else:
        actual_http = sum(not row.get("not_sent", False) for row in http_rows.values())
        real_http = sum(not row.get("not_sent", False) and row.get("transport_provenance") == "real_http" for row in http_rows.values())
        mock_http = sum(not row.get("not_sent", False) and row.get("transport_provenance") == "fixed_fixture" for row in http_rows.values())
        not_sent_count = len(http_rows) - actual_http
    if envelope_count:
        outcome.facts["real_http_attempts"], outcome.facts["mock_http_attempts"] = real_http,mock_http
    outcome.facts["observed_counts"] = {"product_attempts": len(product_ids), "sdk_inputs": len(sdk_ids),
        "mock_model_http_attempts": mock_http, "real_model_http_attempts": real_http,
        "unclassified_transport_attempts": actual_http-real_http-mock_http,
        "planned_local_not_sent": not_sent_count} if envelope_count else {
            "status": "no model-exchange evidence; local facts remain separate", "product_attempts": None,
            "sdk_inputs": None, "mock_model_http_attempts": None, "real_model_http_attempts": None}
    if context.live_gate is not None and budget_ledger is not None:
        from tests.support.agent_acceptance.preparation import _LiveLedger
        ledger = _LiveLedger.model_validate_json(budget_ledger.read_bytes())
        if stop_case:
            incomplete.extend(_validate_stop_reservations(context, ledger, stop_exchanges))
        else:
            observed = {row["ledger_number"]: row for row in http_rows.values()}
            bound = [item for item in ledger.attempts if item.case_id == context.case_id and item.protocol == context.protocol
                and item.execution_id == context.live_gate.execution_id(context.case_id, context.protocol)]
            if {item.number for item in bound} != set(observed) or len(observed) != len(http_rows):
                raise SharedControlError("Gated SDK/HTTP observations and reservations are not one-to-one.")
            for item in bound:
                row = observed[item.number]
                identity = row["request_identity"]
                if (item.sdk_input_id != row["sdk_input_id"] or item.request_sha256 != sha256(row["request_utf8"].encode()).hexdigest()
                        or (item.actor, item.purpose, item.delegation_id, item.product_number) !=
                        (identity["actor"], identity["purpose"], identity["delegation_id"], identity["number"])
                        or item.judge_call_id != identity.get("judge_call_id")
                        or item.state == "not_sent" and item.fault_origin != row.get("fault_origin")
                        or item.fault_origin == "accepted durable stop before terminal HTTP" and not (ledger.stopped or
                            context.live_gate.stop_key(context.case_id, context.protocol) in ledger.case_stops)
                        or (item.state == "not_sent") != bool(row.get("not_sent"))):
                    raise SharedControlError("Gated request identity or terminal state changed.")
    if incomplete:
        raise IncompleteEvidenceError("; ".join(dict.fromkeys(incomplete)),
            stop_violation=StopViolation.from_outcome(outcome) if stop_case else None)
    if context.case_id.startswith("formal.") or context.case_id == "desktop.handoff":
        from tests.support.agent_acceptance.preparation import PreparedBundle, LiveAuthorization, _LiveLedger, bundle_fingerprint, digest

        prepared = PreparedBundle.model_validate_json((context.root / "prepared.json").read_bytes())
        ledger = _LiveLedger.model_validate_json((context.root / "ledger.json").read_bytes())
        authorities = [json.loads(path.read_bytes()) for path in outcome.evidence.values() if path.name == "authorization.json"]
        if not authorities:
            raise SharedControlError("Formal evidence is missing its original authorization.")
        grant = LiveAuthorization.model_validate(authorities[0]["grant"])
        if any(authority.get("execution_id") != (context.execution_id if context.execution is not None else None) for authority in authorities):
            raise SharedControlError("Formal authority belongs to a different frozen execution.")
        if ledger.candidates:
            verified = verify_formal_accounting_archive(context.root, grant=grant, execution_id=context.execution_id)
            with zipfile.ZipFile(context.root / "accounting.zip") as archived_files:
                archived_prepared = archived_files.read("prepared.json")
            if verified != ledger or (context.root / "prepared.json").read_bytes() != archived_prepared:
                raise SharedControlError("Formal archived genesis and original ledger differ.")
            if any(authority.get("candidate_binding") != grant.binding or authority["grant"] != authorities[0]["grant"] for authority in authorities):
                raise SharedControlError("Formal authority differs from its archived active candidate grant.")
        elif (prepared.binding != bundle_fingerprint(prepared) or grant.binding != prepared.binding or ledger.binding != prepared.binding
                or any(authority["grant"] != authorities[0]["grant"] for authority in authorities)
                or ledger.authorization_binding != (digest(grant.model_dump(mode="json")) if ledger.attempts else None)
                or any(item.number != index or item.authorization_id != grant.authorization_id for index, item in enumerate(ledger.attempts, 1))):
            raise SharedControlError("Formal prepared source, authorization and final ledger do not agree.")
        bound = [item for item in ledger.attempts if item.case_id == context.case_id and item.protocol == context.protocol
            and (context.execution is None or item.execution_id == context.execution_id)]
        observed = {row["ledger_number"]: row for row in http_rows.values()}
        if {item.number for item in bound} != set(observed) or len(observed) != len(http_rows):
            raise SharedControlError("Formal terminal observations and reserved ledger entries are not one-to-one.")
        for item in bound:
            identity = observed[item.number]["request_identity"]
            if (item.actor, item.purpose, item.delegation_id, item.product_number) != (identity["actor"], identity["purpose"], identity["delegation_id"], identity["number"]):
                raise SharedControlError("Formal ledger role, purpose or original product request ownership changed.")


def write_semantic_review(context: CaseContext, outcome: CaseOutcome, case: AcceptanceCase, *,
                          legacy_interpretations: dict[str, dict[str, Any]] | None = None) -> None:
    """Make each role's input and output reviewable, without inventing a semantic pass."""
    criteria = {
        "answer": "Check each claim against the actual input, action bytes, source, failures, and unfinished work.",
    }
    if case.group == "delegation":
        criteria["delegation"] = "Review parent and child separately; a correct artifact does not establish a correct explanation or user acceptance."
    if case.group == "review":
        criteria["review"] = "Check the independent rationale against the exact candidate, scope, source and approval evidence."
    if case.group == "memory" or context.case_id.startswith("memory."):
        from tests.support.agent_acceptance.memory import MEMORY_CRITERIA
        criteria = MEMORY_CRITERIA
    if case.group == "compaction" or "compaction" in context.case_id:
        from tests.support.agent_acceptance.compaction_review import CRITERIA
        criteria = {name: "Review this output against its actual input and the case's fixed compaction materials." for name in CRITERIA}
    samples = []
    received: set[str] = set()
    for label, path in outcome.evidence.items():
        if "exchange" not in label or path.suffix != ".json":
            continue
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or "exchanges" not in value:
            continue
        for index, exchange in enumerate(value["exchanges"]):
            if exchange["sdk_input_id"] in received:
                continue
            received.add(exchange["sdk_input_id"])
            identity = exchange.get("request_identity")
            fingerprint = sha256(exchange["request_utf8"].encode()).hexdigest()
            matching = [attempt for attempt in observed_attempts(context, outcome, value)
                if ((attempt.get("observation") or {}).get("wire") or {}).get("body_sha256") == fingerprint]
            attempt = next((item for item in matching if identity is None or all(item.get(key) == expected for key, expected in identity.items())), None)
            if attempt is None:
                raise SharedControlError("Semantic output lacks its product-owned input.")
            sdk = next(item for item in value["sdk_inputs"] if item["sdk_input_id"] == exchange["sdk_input_id"])
            decoded = response_evidence(exchange, exchange_sha256=sha256(path.read_bytes()).hexdigest(),
                legacy_interpretation=(legacy_interpretations or {}).get(exchange["sdk_input_id"]))
            samples.append({"sample_id": f"{label}:{index}", "task_id": attempt.get("task_id", value.get("task_id")),
                "protocol": value["protocol"],
                "product_record_stage": attempt.get("record_stage", "reliable_saved_record"),
                "kind": "summary" if attempt["purpose"] == "compaction" else "memory_review" if attempt["actor"] == "memory_review"
                    else "independent_review" if attempt["actor"] == "action_review" else "child_work" if attempt.get("delegation_id") else "main_work",
                "actor": attempt["actor"], "purpose": attempt["purpose"], "delegation_id": attempt.get("delegation_id"),
                "request_number": attempt["number"], "sdk_input": sdk, "actual_wire": json.loads(exchange["request_utf8"]),
                "sdk_input_id": exchange["sdk_input_id"],
                "sdk_sha256": sha256(json.dumps(sdk, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "exchange_path": str(path.relative_to(context.root)) if path.is_relative_to(context.root) else str(path),
                "exchange_sha256": sha256(path.read_bytes()).hexdigest(),
                "received_utf8": exchange["received_utf8"], "http_status": exchange.get("status"), "error": exchange.get("error"),
                "not_sent": exchange.get("not_sent", False), "fault_origin": exchange.get("fault_origin"),
                "finish_reason": (attempt.get("observation") or {}).get("finish_reason"),
                "semantic_applicability": "no received model output" if not exchange["received_utf8"] else "received output requires human review",
                "wire_sha256": fingerprint, "response_sha256": sha256(exchange["received_utf8"].encode()).hexdigest(),
                "response_evidence": decoded,
                "response_evidence_sha256": sha256(json.dumps(decoded, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "semantic_status": "not_measured", "reviewer": None, "criteria_results": {key: "unknown" for key in criteria}})
    from tests.support.agent_acceptance.output_review import output_units

    outputs = output_units(samples, outcome, context, criteria)
    if any(not output["evidence_complete"] for output in outputs):
        outcome.evidence_status = "incomplete"
    path = context.root / "all-capability-review.json"
    path.write_text(json.dumps({"run_id": context.run_id, "case_id": context.case_id, "protocol": context.protocol,
        "response_binding_version": 2, "legacy_interpretations": legacy_interpretations or {},
        "group": case.group, "criteria": criteria, "semantic_status": "not_measured", "reviewer": None,
        "provenance": "fixed_fixture" if outcome.facts.get("real_http_attempts", 0) == 0 else "current_real",
        "dedicated_reviews": {name: str(value.relative_to(context.root)) for name, value in outcome.evidence.items()
            if ("review" in name or name == "review") and value.is_relative_to(context.root)},
        "outputs": outputs, "execution_id": outcome.execution_id, "samples": samples, "applicability": "No received model output" if not samples else "Human review required"},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outcome.evidence["semantic_review" if "semantic_review" not in outcome.evidence else "all_capability_review"] = path
    outcome.semantic_status = "not_measured"
