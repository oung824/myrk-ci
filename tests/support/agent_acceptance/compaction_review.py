"""Per-output review artifacts, separate from compaction execution and inputs."""

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from myrk.conversation import ConversationRecord
from tests.support.agent_acceptance.core import CaseContext
from tests.support.agent_acceptance.replay import AcceptanceSession
from tests.support.agent_acceptance.recorded_response import response_evidence


CRITERIA = ("numbers_subjects", "corrections_status", "source_use", "forbidden_actions", "pending_ownership", "no_new_authority")


def fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def write_review(context: CaseContext, record: ConversationRecord, session: AcceptanceSession, *,
                 provenance: Literal["fixed_fixture", "current_real"] = "fixed_fixture",
                 legacy_interpretations: dict[str, dict[str, Any]] | None = None) -> Path:
    """Bind each received output to its actual SDK and HTTP input, not coverage."""
    samples: list[dict[str, Any]] = []
    original_bytes = (context.root / "exchange.json").read_bytes()
    original = json.loads(original_bytes)
    original_sha256 = sha256(original_bytes).hexdigest()
    if original["exchanges"] != session.exchanges or original["sdk_inputs"] != session.sdk_inputs:
        raise ValueError("Review session differs from its original captured exchange.")
    interpretations = legacy_interpretations or {}
    if set(interpretations) - {item["sdk_input_id"] for item in session.exchanges}:
        raise ValueError("Legacy response interpretation has no matching original response.")
    used_exchanges: set[int] = set()
    used_sdk: set[str] = set()
    for turn in record.turns:
        for attempt in turn.request_attempts:
            observation = attempt.observation
            wire = observation.wire if observation is not None else None
            exchange = None
            sdk = None
            exchange_index = None
            identity = {name: getattr(attempt,name) for name in ("number","actor","purpose","delegation_id")}
            if wire is not None:
                matches = [(index,item) for index,item in enumerate(session.exchanges) if index not in used_exchanges
                    and sha256(item["request_utf8"].encode()).hexdigest() == wire.body_sha256 and item.get("request_identity") == identity]
                if len(matches) == 1:
                    exchange_index,exchange = matches[0]
                    used_exchanges.add(exchange_index)
                    sdk = next(item for item in session.sdk_inputs if item["sdk_input_id"] == exchange["sdk_input_id"])
                elif not matches and attempt.status == "failed" and attempt.http_status is None and (attempt.error or "").startswith("CaseBudgetExceeded"):
                    candidates = [item for item in session.sdk_inputs if item["request_identity"] == identity and item["sdk_input_id"] not in used_sdk]
                    if len(candidates) != 1:
                        raise ValueError("Pre-HTTP budget refusal lacks its unique actual SDK input.")
                    sdk = candidates[0]
                else:
                    raise ValueError("Actual HTTP evidence does not match the persisted request attempt.")
            if sdk is not None:
                used_sdk.add(sdk["sdk_input_id"])
            mapping = next((item.model_dump(mode="json", exclude={"messages", "view"}) for item in turn.compactions if attempt.number in item.request_numbers), None)
            response_events = []
            decoded_response = (response_evidence(exchange, exchange_sha256=original_sha256,
                legacy_interpretation=interpretations.get(exchange["sdk_input_id"])) if exchange is not None else None)
            if decoded_response is not None:
                response_events = decoded_response["events"]
            google_tool = any("functionCall" in part for event in response_events for candidate in event.get("candidates", [])
                              for part in candidate.get("content", {}).get("parts", []))
            kind = "summary" if attempt.purpose == "compaction" else "work" if google_tool or observation is not None and observation.finish_reason == "tool_calls" else "final"
            samples.append({"sample_id": f"{context.case_id}:{attempt.number}", "case_id": context.case_id,
                "exchange_index": exchange_index,
                "task_id": turn.task.id if turn.task else None, "number": attempt.number, "kind": kind,
                "actor": attempt.actor, "purpose": attempt.purpose, "formal_attempt": attempt.model_dump(mode="json"),
                "sdk_sha256": fingerprint(sdk), "wire_sha256": wire.body_sha256 if wire else None,
                "response_sha256": sha256(exchange["received_utf8"].encode()).hexdigest() if exchange else None,
                "actual_input": {"sdk_before_provider_adaptation": sdk,
                                 "actual_wire_body": json.loads(exchange["request_utf8"]) if exchange else None},
                "source_mapping": {"covered": mapping["covered"] if mapping else None,
                                   "retained": mapping["retained"] if mapping else None,
                                   "compaction": mapping,
                                   "note": "Coverage references are separate from actual summary input."},
                "visible_previous_summaries": [item.summary for previous in record.turns for item in previous.compactions
                    if item.summary and item.request_numbers and max(item.request_numbers) < attempt.number
                    and item.summary in json.dumps(sdk, ensure_ascii=False)],
                "response_utf8": exchange["received_utf8"] if exchange else None,
                "response_evidence": decoded_response,
                "response_evidence_sha256": fingerprint(decoded_response),
                "stage_result": "not_sent" if exchange is None else "transport_failure" if exchange.get("error") or exchange["status"] != 200 else
                    "incomplete" if decoded_response is not None and decoded_response["status"] == "incomplete" else
                    "truncated" if observation is not None and observation.finish_reason in {"length", "MAX_TOKENS"} else
                    "offline_fixture_only" if provenance == "fixed_fixture" else "current_real_unreviewed",
                "provenance": provenance, "semantic_status": "not_measured", "reviewer": None,
                "criteria": {name: "unknown" for name in CRITERIA}, "notes": {},
                "review_scope": "Review this output against its actual input; later corrections do not apply retroactively."})
    if len(used_exchanges) != len(session.exchanges) or used_sdk != {item["sdk_input_id"] for item in session.sdk_inputs}:
        raise ValueError("Unbound SDK or HTTP evidence remains.")
    path = context.root / "review.json"
    path.write_text(json.dumps({"run_id": context.run_id, "case_id": context.case_id, "protocol": context.protocol,
        "response_binding_version": 2,
        "legacy_interpretations": interpretations,
        "exchange_sha256": original_sha256,
        "semantic_status": "not_measured", "samples": samples}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class HumanReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str
    sdk_sha256: str
    wire_sha256: str | None
    response_sha256: str | None
    response_evidence_sha256: str | None = None
    reviewer: str
    criteria: dict[str, Literal["passed", "failed", "unknown"]]
    notes: dict[str, str]


def apply_reviews(report_path: Path, reviews: list[HumanReview]) -> dict[str, Any]:
    """Validate human attribution and input binding; fixtures never prove semantics."""
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    exchange_body = report_path.with_name("exchange.json").read_bytes()
    if sha256(exchange_body).hexdigest() != report["exchange_sha256"]:
        raise ValueError("Original exchange fingerprint changed.")
    original = json.loads(exchange_body)
    binding_version = report.get("response_binding_version", 1)
    if type(binding_version) is not int or binding_version not in {1, 2}:
        raise ValueError("Unknown response binding version.")
    if binding_version == 1 and (any("response_metadata" in exchange for exchange in original["exchanges"])
            or report.get("legacy_interpretations")
            or any("response_evidence" in sample for sample in report["samples"])):
        raise ValueError("New captured responses require version 2 evidence bindings.")
    if binding_version == 2 and any("response_evidence" not in sample or "response_evidence_sha256" not in sample for sample in report["samples"]):
        raise ValueError("Version 2 response binding fields are required.")
    samples = {row["sample_id"]: row for row in report["samples"]}
    reviewed: set[str] = set()
    for review in reviews:
        if review.sample_id in reviewed or review.sample_id not in samples:
            raise ValueError("Unknown or duplicate review sample.")
        sample = samples[review.sample_id]
        index = sample["exchange_index"]
        if index is None:
            raise ValueError("No original response exists for this review sample.")
        exchange = original["exchanges"][index]
        sdk = next(item for item in original["sdk_inputs"] if item["sdk_input_id"] == exchange["sdk_input_id"])
        if (fingerprint(sample["actual_input"]["sdk_before_provider_adaptation"]) != sample["sdk_sha256"]
                or sample["actual_input"]["sdk_before_provider_adaptation"] != sdk
                or sample["actual_input"]["actual_wire_body"] != json.loads(exchange["request_utf8"])
                or sha256(exchange["request_utf8"].encode()).hexdigest() != sample["wire_sha256"]
                or sample["response_utf8"] != exchange["received_utf8"]
                or sha256(sample["response_utf8"].encode()).hexdigest() != sample["response_sha256"]):
            raise ValueError("Review report differs from its original input/output binding.")
        if not review.reviewer.strip() or set(review.criteria) != set(CRITERIA):
            raise ValueError("A named human reviewer and all six criteria are required.")
        if any(getattr(review, name) != sample[name] for name in ("sdk_sha256", "wire_sha256", "response_sha256")):
            raise ValueError("Review does not match the original input and output.")
        if binding_version == 2:
            decoded = response_evidence(exchange, exchange_sha256=report["exchange_sha256"],
                legacy_interpretation=report.get("legacy_interpretations", {}).get(exchange["sdk_input_id"]))
            identity = exchange.get("request_identity")
            if (sample.get("response_evidence") != decoded
                    or sample.get("response_evidence_sha256") != fingerprint(decoded)
                    or review.response_evidence_sha256 != fingerprint(decoded)
                    or identity is not None and (sdk.get("request_identity") != identity
                        or any(sample.get(name) != identity[name] for name in ("number", "actor", "purpose"))
                        or any(sample["formal_attempt"].get(name) != value for name, value in identity.items()))):
                raise ValueError("Review differs from the original decoded response binding.")
        else:
            if review.response_evidence_sha256 is not None:
                raise ValueError("Version 2 review cannot be downgraded to a legacy report.")
        sample.update(reviewer=review.reviewer, criteria=review.criteria, notes=review.notes)
        reviewed.add(review.sample_id)
    return report
