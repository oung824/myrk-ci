"""Versioned per-output human reviews over the existing acceptance originals."""

from datetime import datetime
from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

OutputType = Literal["main_answer", "child_report", "review_reason", "memory", "summary"]


class HumanReview(BaseModel):
    """An explicit human attestation supplied by the caller, never AI attribution."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    review_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    output_id: str
    reviewer: str = Field(min_length=1)
    authorship: Literal["human"]
    signed_at: datetime
    scope: str = Field(min_length=1)
    package_sha256: str
    criteria_version: str
    output_sha256: str
    reviewed_originals: list[str]
    conclusions: dict[str, Literal["passed", "failed", "unknown", "not_applicable"]]
    reasons: dict[str, str]
    resolves: list[str] = Field(default_factory=list)

    @field_validator("reviewer", "scope")
    @classmethod
    def named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A named human and actual review scope are required.")
        return value

    @field_validator("signed_at")
    @classmethod
    def dated(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Human signature time needs a timezone.")
        return value



class ReviewOriginal(BaseModel):
    """Review-only context with an execution-owned expected byte fingerprint."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reuse_execution_original: bool = False


def digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def output_units(samples: list[dict[str, Any]], outcome: Any, context: Any, criteria: dict[str, str]) -> list[dict[str, Any]]:
    """Extract actual model text and memory changes, retaining complete raw inputs."""
    outputs: list[dict[str, Any]] = []
    kinds = {"main_work": "main_answer", "child_work": "child_report", "independent_review": "review_reason",
        "memory_review": "memory", "summary": "summary"}
    standard: dict[str, Any] = {"version": "common-output-v1", "dimensions": {
        "factual_accuracy": {"description": "Claims agree with the actual input, sources and effects.", "applicable": True},
        "sufficiency": {"description": "The required goal and constraints are met; equivalent legal paths are allowed.", "applicable": True},
        "readability": {"description": "The result can be understood; harmless wording or layout alone does not fail the task.", "applicable": True},
        "critical_status": {"description": "Saved, sent, checked, delivered and accepted claims match actual evidence.", "applicable": True}}}
    domain_standard = outcome.facts.get("output_standard")
    if domain_standard is not None:
        standard = {**standard, **domain_standard, "dimensions": {**standard["dimensions"], **domain_standard["dimensions"]}}
    critical_standards = {
        kind: {**standard, "version": f'{standard["version"]}::critical-contracts-v1',
            "dimensions": {**standard["dimensions"], **dimensions}}
        for kind, dimensions in outcome.facts.get("output_critical_dimensions", {}).items()
    }
    for sample in samples:
        raw = sample["received_utf8"]
        view = sample["response_evidence"]
        pieces: list[str] = []
        calls = False
        unreadable = False
        finishes = [sample.get("finish_reason")]
        for event in view["events"]:
            candidates = event.get("candidates", [])
            choices = event.get("choices", [])
            if not isinstance(candidates, list) or not isinstance(choices, list):
                unreadable = True
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict) or not isinstance(candidate.get("content", {}), dict):
                    unreadable = True
                    continue
                finishes.append(candidate.get("finishReason"))
                parts = candidate.get("content", {}).get("parts", [])
                if not isinstance(parts, list):
                    unreadable = True
                    continue
                for part in parts:
                    if not isinstance(part, dict):
                        unreadable = True
                    elif not part.get("thought", False):
                        if isinstance(part.get("text"), str):
                            pieces.append(part["text"])
                        elif isinstance(part.get("functionCall"), dict):
                            calls = True
                        else:
                            unreadable = True
            for choice in choices:
                if not isinstance(choice, dict):
                    unreadable = True
                    continue
                finishes.append(choice.get("finish_reason"))
                message = choice.get("delta") or choice.get("message") or {}
                if not isinstance(message, dict):
                    unreadable = True
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    pieces.append(content)
                elif content is not None:
                    unreadable = True
                calls = calls or isinstance(message.get("tool_calls"), list) and bool(message["tool_calls"])
        text = "".join(pieces)
        truncated = any(reason in ("length", "MAX_TOKENS") for reason in finishes)
        if not text and (calls and not unreadable or not raw or sample["http_status"] != 200):
            continue
        kind = kinds[sample["kind"]]
        issue = ("Interrupted response; preserve received fragments." if sample["error"] else
            "Truncated response; completeness is not established." if truncated else
            "Received original could not be decoded as model text; review the untouched native response." if not text else
            "Some native content has no supported visible-text representation; preserve and review its original." if unreadable else None)
        outputs.append({"output_id": f"{outcome.execution_id}:{sample['sample_id']}", "type": kind,
            "text": text, "raw_response": raw, "evidence_issue": issue,
            "response_evidence": view, "response_evidence_sha256": sample["response_evidence_sha256"],
            "sha256": digest({"text": text, "raw": raw, "response_evidence_sha256": sample["response_evidence_sha256"]}),
            "actual_input": {"sdk": sample["sdk_input"], "wire": sample["actual_wire"]},
            "source_samples": [sample["sample_id"]], "effects": outcome.facts.get("all_actions", outcome.facts.get("actions", [])),
            "standard": critical_standards.get(kind, standard),
            "domain_criteria": outcome.facts.get("output_domain_criteria", {"version": f"{context.case_id}:existing-criteria-v1", "criteria": criteria}),
            "evidence_complete": bool(text) and view["status"] == "decoded" and not unreadable and not sample["error"] and sample["http_status"] == 200 and not truncated,
            "provenance": "current_real" if outcome.facts.get("real_http_attempts", 0) else "fixed_fixture"})
    seen: set[str] = set()
    optional_diagnostics = {f"{phase}/{role}/save-diagnostics.json" for phase in ("work", "reopen", "capacity-reopen") for role in ("receiver", "owner")}
    for label, path in outcome.evidence.items():
        if path.suffix != ".json" or "exchange" in label:
            continue
        if path.name == "save-diagnostics.json" and label.replace("\\", "/") in optional_diagnostics:
            body = path.read_bytes()
            outcome.facts.setdefault("auxiliary_diagnostic_originals", {})[label] = {
                "path": str(path), "sha256": sha256(body).hexdigest(), "bytes": len(body),
                "classification": "optional save diagnostic; not a conversation or model output"}
            continue
        record = json.loads(path.read_bytes())
        if not isinstance(record, dict):
            continue
        for turn in record.get("turns", []):
            for action in turn.get("actions", []):
                if not action.get("memory_changes") or action["id"] in seen:
                    continue
                seen.add(action["id"])
                matching = [sample for sample in samples if sample["task_id"] == (turn.get("task") or {}).get("id")
                    and sample["request_number"] == action.get("prepared_request")]
                outputs.append({"output_id": f"{outcome.execution_id}:memory:{action['id']}", "type": "memory",
                    "text": json.dumps(action["memory_changes"], ensure_ascii=False), "raw_response": action,
                    "sha256": digest(action), "actual_input": [sample["sdk_input"] for sample in matching],
                    "source_samples": [sample["sample_id"] for sample in matching], "effects": action,
                    "standard": standard, "domain_criteria": outcome.facts.get("output_domain_criteria", {"version": f"{context.case_id}:existing-criteria-v1", "criteria": criteria}),
                    "evidence_complete": bool(matching), "provenance": "current_real" if outcome.facts.get("real_http_attempts", 0) else "fixed_fixture"})
    return outputs


def export_review(result_path: Path, destination: Path, *, final_reviewer: str | None = None,
                  standards: dict[str, dict[str, Any]] | None = None,
                  review_originals: dict[str, dict[str, ReviewOriginal]] | None = None,
                  execution_standards: dict[str, dict[str, dict[str, Any]]] | None = None) -> Path:
    """Export every actual output, its frozen standard and all necessary originals."""
    result_path, destination = result_path.resolve(), destination.resolve()
    result = json.loads(result_path.read_bytes())
    pairs = [(case["case_id"], case["protocol"]) for case in result["cases"]]
    identities = [case["execution_id"] for case in result["cases"]]
    planned = {row["execution_id"]:row for row in result.get("planned_executions",[])}
    if (len(identities) != len(set(identities)) or len(pairs) != len(set(pairs)) and not planned
            or planned and (set(identities) != set(planned) or any((row["case_id"],row["protocol"]) !=
                (planned[row["execution_id"]]["case_id"],planned[row["execution_id"]]["protocol"]) for row in result["cases"]))):
        raise ValueError("Repeated case/protocol or execution IDs lack the original complete per-execution plan.")
    extras: dict[str, dict[str, tuple[ReviewOriginal, bytes]]] = {}
    cases_by_id = {case["execution_id"]: case for case in result["cases"]}
    if set(execution_standards or {}) - set(cases_by_id):
        raise ValueError("Per-execution review standards require original execution IDs.")
    for execution_id, items in (review_originals or {}).items():
        if execution_id not in cases_by_id:
            raise ValueError("A review-only original must bind an existing execution ID.")
        extras[execution_id] = {}
        for label, supplied in items.items():
            original = ReviewOriginal.model_validate(supplied)
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", label):
                raise ValueError("Review-only labels must be portable.")
            product = cases_by_id[execution_id]["evidence"].get(label)
            if product is not None and not original.reuse_execution_original:
                raise ValueError("A review-only label cannot replace a product original.")
            body = original.path.read_bytes()
            if sha256(body).hexdigest() != original.sha256:
                raise ValueError("A frozen review-only original is missing or changed.")
            if product is not None and sha256((result_path.parent / product).read_bytes()).hexdigest() != original.sha256:
                raise ValueError("A review-only label cannot replace a product original with different bytes.")
            extras[execution_id][label] = (original, body)
    destination.mkdir(parents=True, exist_ok=False)
    review_index: dict[str, dict[str, Any]] = {}
    originals: dict[str, dict[str, Any]] = {}
    outputs: list[dict[str, Any]] = []
    for case in result["cases"]:
        prefix = f"{case['case_id']}/{case['protocol']}"
        if planned:
            prefix += "/" + sha256(case["execution_id"].encode()).hexdigest()[:24]
        required = []
        review = None
        index = None
        if "evidence_index" in case["evidence"]:
            index_source = Path(case["evidence"]["evidence_index"])
            if not index_source.is_absolute():
                index_source = result_path.parent / index_source
            index = json.loads(index_source.read_bytes())
            if ((index["run_id"], index["case_id"], index["protocol"]) !=
                    (case.get("facts",{}).get("source_run_id",result["run_id"]),case["case_id"],case["protocol"])
                    or planned and index.get("execution_id") != case["execution_id"]):
                raise ValueError("The original evidence index belongs to another execution.")
        elif case["product_status"] != "not_executed" and case["tool_status"] != "failed":
            raise ValueError("Executed case is missing its original evidence index.")
        for label, value in case["evidence"].items():
            source = Path(value) if Path(value).is_absolute() else result_path.parent / value
            key = f"{prefix}/{label}"
            relative = f"originals/{key}{source.suffix}"
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination):
                raise ValueError("An original label cannot escape the review package.")
            body = source.read_bytes()
            fingerprint = sha256(body).hexdigest()
            if index is not None and label != "evidence_index" and (label not in index["links"] or index["links"][label]["sha256"] != fingerprint):
                raise ValueError("An original differs from its published execution fingerprint.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            originals[key] = {"path": relative, "sha256": fingerprint}
            required.append(key)
            if source.name == "all-capability-review.json":
                review = json.loads(target.read_bytes())
        for label, (supplied, body) in extras.get(case["execution_id"], {}).items():
            key = f"{prefix}/{label}"
            retained = originals.get(key)
            if retained is None:
                relative = f"originals/review-context/{supplied.sha256}{supplied.path.suffix}"
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(body)
                retained = {"path": relative, "sha256": supplied.sha256}
                originals[key] = retained
                required.append(key)
            elif retained["sha256"] != supplied.sha256:
                raise ValueError("A review-only label cannot replace a product original with different bytes.")
            retained.update(purpose="review_context", execution_id=case["execution_id"], product_output=False)
            review_index.setdefault(case["execution_id"], {})[label] = {"original_key": key, "sha256": supplied.sha256}
        if review is None and (case["product_status"] == "not_executed" or case["tool_status"] == "failed"
                or planned and planned[case["execution_id"]]["role"] == "judge"):
            continue
        if review is None:
            raise ValueError("The selected case has no common per-output evidence; it remains unreviewed.")
        for output in review["outputs"]:
            original_standard = output["standard"]
            standard = (execution_standards or {}).get(case["execution_id"], {}).get(output["type"],
                (standards or {}).get(output["type"], original_standard))
            if not standard.get("version") or not standard.get("dimensions") or any(
                    not isinstance(row.get("applicable"), bool) or not row.get("description")
                    or not row["applicable"] and not row.get("reason") for row in standard["dimensions"].values()):
                raise ValueError("Freeze a version, dimensions and justified applicability before review.")
            original_critical = {name: row for name, row in original_standard["dimensions"].items() if "case_evidence_complete" in row}
            selected_critical = {name: row for name, row in standard["dimensions"].items() if "case_evidence_complete" in row}
            if any(type(row["case_evidence_complete"]) is not bool for row in [*original_critical.values(), *selected_critical.values()]):
                raise ValueError("Complete case evidence must be a Boolean.")
            if original_critical != selected_critical:
                raise ValueError("An override cannot add, drop or change execution-owned critical dimensions.")
            outputs.append({**output, "standard": standard, "required_originals": required,
                "case_id": case["case_id"], "protocol": case["protocol"], "execution_id": case.get("execution_id", "")})
    package = {"schema_version": 1, "source_result": str(result_path), "run_id": result["run_id"], "mode": result["mode"], "version": result["version"],
        "result_sha256": sha256(result_path.read_bytes()).hexdigest(), "original_cases": result["cases"],
        "planned_executions":result.get("planned_executions",[]),"planned_denominator":result.get("planned_denominator",len(result["cases"])),
        "originals": originals, "review_originals": review_index, "outputs": outputs, "final_reviewer": final_reviewer,
        "user_acceptance": "pending", "authorship_rule": "Only an explicit human attestation may be imported; AI notes are separate."}
    package["binding"] = digest(package)
    path = destination / "review-package.json"
    path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "reviews").mkdir()
    template = [{"output_id": output["output_id"], "criteria_version": output["standard"]["version"],
        "output_sha256": output["sha256"], "package_sha256": package["binding"], "reviewed_originals": [],
        "reviewer": None, "signed_at": None, "scope": None, "conclusions": {name: "unknown" for name in output["standard"]["dimensions"]},
        "reasons": {}, "unsigned": True} for output in outputs]
    (destination / "unsigned-review-forms.json").write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_path = result_path.with_name("review-exports.json")
    index = json.loads(index_path.read_bytes()) if index_path.exists() else []
    for case in result["cases"]:
        source = Path(case.get("facts",{}).get("source_result",str(result_path))).with_name("review-exports.json")
        if source != index_path and source.is_file():
            for prior in json.loads(source.read_bytes()):
                if prior not in index:
                    index.append(prior)
    index.append({"package": str(path), "binding": package["binding"], "criteria_versions": sorted({output["standard"]["version"] for output in outputs})})
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_package(path: Path) -> dict[str, Any]:
    package: dict[str, Any] = json.loads(path.read_bytes())
    if digest({name: value for name, value in package.items() if name != "binding"}) != package["binding"]:
        raise ValueError("Frozen review package changed.")
    for row in package["originals"].values():
        original = (path.parent / row["path"]).resolve()
        if not original.is_relative_to(path.parent.resolve()) or sha256(original.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Required original is missing or changed.")
    return package


def review_history(package_path: Path, package: dict[str, Any]) -> dict[str, list[HumanReview]]:
    """Read the same frozen output/standard/evidence across repeated exports."""
    current = {row["output_id"]: row for row in package["outputs"]}
    histories: dict[str, list[HumanReview]] = {name: [] for name in current}
    seen: set[str] = set()
    index_path = Path(package["source_result"]).with_name("review-exports.json")
    paths = dict.fromkeys([package_path.resolve(), *(Path(row["package"]).resolve() for row in json.loads(index_path.read_bytes()))])
    for path in paths:
        source = package if path == package_path.resolve() else read_package(path)
        eligible = {}
        for old in source["outputs"]:
            target = current.get(old["output_id"])
            if target is None or old["sha256"] != target["sha256"] or old["standard"] != target["standard"]:
                continue
            old_evidence = {name: source["originals"][name]["sha256"] for name in old["required_originals"]}
            new_evidence = {name: package["originals"][name]["sha256"] for name in target["required_originals"]}
            if old_evidence == new_evidence:
                eligible[old["output_id"]] = old
        for record in sorted((path.parent / "reviews").glob("*.json")):
            review = HumanReview.model_validate_json(record.read_bytes())
            if review.output_id not in eligible:
                continue
            output = eligible[review.output_id]
            if (review.package_sha256 != source["binding"] or review.output_sha256 != output["sha256"]
                    or review.criteria_version != output["standard"]["version"]
                    or review.resolves and review.reviewer != source["final_reviewer"]):
                raise ValueError("Historical review differs from its frozen original package.")
            identity = digest(review.model_dump(mode="json"))
            if identity not in seen:
                histories[review.output_id].append(review)
                seen.add(identity)
    return histories


def import_reviews(package_path: Path, reviews: list[HumanReview]) -> dict[str, Any]:
    """Append signed records; do not mutate original outputs or earlier reviews."""
    package = read_package(package_path)
    outputs = {output["output_id"]: output for output in package["outputs"]}
    histories = review_history(package_path, package)
    pending: dict[Path, str] = {}
    for supplied in reviews:
        review = HumanReview.model_validate(supplied.model_dump())
        if review.output_id not in outputs:
            raise ValueError("Unknown output remains in the denominator.")
        output = outputs[review.output_id]
        dimensions = output["standard"]["dimensions"]
        if (review.package_sha256 != package["binding"] or review.criteria_version != output["standard"]["version"]
                or review.output_sha256 != output["sha256"] or set(review.conclusions) != set(dimensions)
                or set(review.reviewed_originals) != set(output["required_originals"])):
            raise ValueError("Review must cover the exact output, frozen standard and every required original.")
        for name, conclusion in review.conclusions.items():
            dimension = dimensions[name]
            applicable = dimension["applicable"]
            if (conclusion == "not_applicable") == applicable or conclusion != "passed" and not review.reasons.get(name, "").strip():
                raise ValueError("Not-applicable must match frozen conditions; non-pass conclusions need reasons.")
            if conclusion == "passed" and "case_evidence_complete" in dimension and not (
                dimension["case_evidence_complete"] is True and output["evidence_complete"]
            ):
                raise ValueError("This critical conclusion needs complete case and output evidence.")
        if not output["evidence_complete"] and all(value in {"passed", "not_applicable"} for value in review.conclusions.values()):
            raise ValueError("Unknown or incomplete output evidence cannot be passed.")
        previous = [*histories[review.output_id], *(HumanReview.model_validate_json(body) for body in pending.values()
            if json.loads(body)["output_id"] == review.output_id)]
        matching = [item for item in previous if item.review_id == review.review_id]
        if any(item.model_dump() != review.model_dump() for item in matching):
            raise ValueError("A review ID cannot rewrite a historical decision across exports.")
        if matching:
            continue
        if review.resolves:
            if any(item.resolves for item in previous):
                raise ValueError("A frozen output and standard permit final human dispute resolution only once.")
            related = {item.review_id for item in previous}
            if review.reviewer != package["final_reviewer"] or set(review.resolves) != related or any(not review.reasons.get(name, "").strip() for name in dimensions):
                raise ValueError("The named final reviewer must explicitly resolve all prior decisions.")
        path = package_path.parent / "reviews" / f"{review.review_id}.json"
        body = review.model_dump_json(indent=2) + "\n"
        if path.exists() and path.read_text(encoding="utf-8") != body:
            raise ValueError("A prior review ID cannot be rewritten.")
        if path in pending and pending[path] != body:
            raise ValueError("A review ID cannot name conflicting decisions in one import.")
        pending[path] = body
    for path, body in pending.items():
        if not path.exists():
            with path.open("x", encoding="utf-8") as stream:
                stream.write(body)
    return review_report(package_path)


def review_report(package_path: Path) -> dict[str, Any]:
    package = read_package(package_path)
    histories = review_history(package_path, package)
    outputs = []
    execution_owned_critical = {
        item["output_id"]: {name for name, row in item["standard"]["dimensions"].items() if "case_evidence_complete" in row}
        for item in package["outputs"]
    }
    for output in package["outputs"]:
        history = histories[output["output_id"]]
        final = [review for review in history if review.resolves]
        if len(final) > 1:
            raise ValueError("More than one final resolution exists for a frozen output.")
        active = ([*final, *(review for review in history if review.review_id not in {*final[0].resolves, final[0].review_id})]
            if final else history)
        disagreement = len({digest(review.conclusions) for review in active}) > 1
        values = [value for review in active for value in review.conclusions.values()]
        dimensions = {}
        for name in output["standard"]["dimensions"]:
            verdicts = {review.conclusions[name] for review in active}
            dimensions[name] = ("failed" if final and final[0].conclusions[name] == "failed" or "failed" in verdicts and "passed" not in verdicts else
                next(iter(verdicts)) if len(verdicts) == 1 else "unknown")
        status = "failed" if "failed" in dimensions.values() else "pending_review" if not values or "unknown" in dimensions.values() else "passed"
        outputs.append({"output_id": output["output_id"], "type": output["type"], "criteria_version": output["standard"]["version"],
            "execution_id": output["execution_id"], "case_id": output["case_id"], "protocol": output["protocol"], "dimensions": dimensions,
            "content_status": status, "disagreement": disagreement, "provenance": output["provenance"],
            "semantic_status": "not_measured" if output["provenance"] == "fixed_fixture" else status,
            "history": [review.model_dump(mode="json") for review in history]})
    cases = []
    judge_calls = []
    execution_roles = {row["execution_id"]:row["role"] for row in package.get("planned_executions",[])}
    for original in package["original_cases"]:
        if execution_roles.get(original.get("execution_id")) == "judge":
            judge_calls.append({"execution_id":original["execution_id"],"case_id":original["case_id"],"protocol":original["protocol"],
                "call_status":original.get("execution_status","unknown"),"semantic_status":"not_measured",
                "budget":original.get("facts",{}).get("budget"),"qualification":"not_measured"})
            continue
        selected = [row for row in outputs if (row["case_id"], row["protocol"], row["execution_id"]) ==
            (original["case_id"], original["protocol"], original.get("execution_id", ""))]
        facts = original.get("facts", {})
        declared_by_type = facts.get("output_critical_dimensions", {})
        missing_critical_output = False
        critical = {name: original.get("critical_contracts", {}).get(name, "unknown") for name in ("K1", "K2", "K3", "K4", "K5")}
        for name, prior in critical.items():
            declared = {kind: dimensions[name] for kind, dimensions in declared_by_type.items() if name in dimensions}
            applicable = [row for row in selected if name in row["dimensions"]
                and (not declared or row["type"] in declared and name in execution_owned_critical[row["output_id"]])]
            required_types = {kind for kind, dimension in declared.items() if dimension["applicable"]}
            missing = not required_types.issubset(row["type"] for row in applicable)
            missing_critical_output |= missing
            values = [row["dimensions"][name] for row in applicable]
            if any(row["dimensions"][name] == "failed" and name in execution_owned_critical[row["output_id"]] for row in applicable):
                critical[name] = "failed"
            elif missing and prior != "failed":
                critical[name] = "unknown"
            elif prior == "unknown" and values and "unknown" not in values:
                critical[name] = "failed" if "failed" in values else "passed" if "passed" in values else "not_applicable"
        content = ("failed" if any(row["content_status"] == "failed" for row in selected) else
            "pending_review" if missing_critical_output else
            "passed" if selected and all(row["content_status"] == "passed" for row in selected) else
            "not_applicable" if not selected and original.get("content_status") == "not_applicable" else "pending_review")
        target = original.get("target_status", "pending_review")
        target_reviewed = not missing_critical_output and bool(selected) and all(
            row["dimensions"].get("factual_accuracy") == row["dimensions"].get("sufficiency") == "passed" for row in selected)
        if target == "pending_review" and target_reviewed:
            target = "passed"
        semantic_uncertainty = (original["product_status"] == "inconclusive" and facts.get("fact_contract_status") == "unknown"
            and facts.get("goal_program_contract_passed") is True and target_reviewed)
        program = "failed" if original["tool_status"] == "failed" else "passed" if original["product_status"] == "passed" or facts.get("goal_program_contract_passed") is True else "not_measured"
        known_failure = any(original.get(name) == "failed" for name in
            ("tool_status", "product_status", "case_status", "target_status", "deterministic_status", "content_status"))
        if original["product_status"] == "not_executed":
            status = "not_executed"
        elif known_failure or content == "failed" or "failed" in critical.values():
            status = "failed"
        elif (original["tool_status"] == target == "passed" and (original["product_status"] == "passed" or semantic_uncertainty)
                and original.get("evidence_status") == "complete"
                and (original.get("deterministic_status") in {"passed", "not_applicable"} or semantic_uncertainty)
                and content in {"passed", "not_applicable"}
                and all(value in {"passed", "not_applicable"} for value in critical.values())
                and all(row["semantic_status"] == "passed" for row in selected)):
            status = "passed"
        else:
            status = "pending_review"
        cases.append({"execution_id": original.get("execution_id"), "case_id": original["case_id"], "protocol": original["protocol"],
            "case_status": status, "target_status": target, "target_basis": "named_human_review" if target_reviewed else "original_program_result",
            "program_status": program, "content_status": content, "critical_contracts": critical,
            "evidence_status": original.get("evidence_status", "unknown"), "deterministic_status": original.get("deterministic_status", "not_measured"),
            "reviewed_outputs": len(selected), "pending_outputs": sum(row["content_status"] == "pending_review" for row in selected),
            "user_acceptance": "pending", "criteria_versions": sorted({row["criteria_version"] for row in selected})})
    return {"run_id": package["run_id"], "package": str(package_path), "outputs": outputs, "case_results": cases, "planned_outputs": len(outputs),
        "pending_outputs": sum(row["content_status"] == "pending_review" for row in outputs), "user_acceptance": "pending",
        "judge_call_results":judge_calls,"planned_product_executions":len(cases),
        "planned_denominator":package.get("planned_denominator",len(package["original_cases"])),
        "original_product_results": [{name: case.get(name) for name in ("execution_id", "case_id", "protocol", "tool_status", "product_status", "case_status", "target_status", "critical_contracts")}
            for case in package["original_cases"] if execution_roles.get(case.get("execution_id"),"product") == "product"]}
