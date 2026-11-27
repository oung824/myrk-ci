"""Read-only reuse of complete historical execution groups; never grants sends."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tests.support.agent_acceptance.output_review import ReviewOriginal, digest, export_review, read_package, review_report


FACETS = ("source", "model", "prompt", "protocol", "permissions", "scope", "save", "summary", "environment", "material", "date", "criteria")


class HistoricalChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    author: str = Field(min_length=1)
    current: dict[str, Any]
    reason: str = Field(min_length=1)



class HistoricalImpact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["unaffected", "affected", "unknown"]
    reason: str = Field(min_length=1)
    evidence_keys: tuple[str, ...] = ()


class HistoricalFactDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str
    fact_path: str = Field(pattern=r"^/")
    matrix: dict[str, HistoricalImpact]
    regrade_id: str | None = None

    @model_validator(mode="after")
    def affected_dimensions(self) -> "HistoricalFactDecision":
        if set(self.matrix) - set(FACETS):
            raise ValueError("Unknown historical impact facet.")
        return self


class HistoricalApplicabilityReview(BaseModel):
    """Independent applicability review; not an original HumanReview."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    review_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    reviewer: str = Field(min_length=1)
    authorship: Literal["ai_applicability", "human_applicability"]
    group_binding: str
    scope: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    facts: tuple[HistoricalFactDecision, ...] = Field(min_length=1)
    complete_group_original_key: str | None = None
    historical_source: Literal["unknown", "historical_real", "actual_local", "offline"] = "unknown"
    source_evidence_keys: tuple[str, ...] = ()



class HistoricalExtractionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    original_key: str
    pointer: str = Field(pattern=r"^/")
    expected: Any


class ReadOnlyHistoricalRegrade(BaseModel):
    """A versioned interpretation over preserved bytes, not a new product run."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    regrade_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    author: str = Field(min_length=1)
    group_binding: str
    execution_id: str
    fact_path: str = Field(pattern=r"^/")
    extraction_version: str = Field(min_length=1)
    criteria_version: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    conclusion: Literal["passed", "failed", "unknown", "not_applicable"]
    correction_kind: Literal["grader_error", "criteria_revision", "extraction_repair"]
    checks: tuple[HistoricalExtractionCheck, ...] = Field(min_length=1)


def import_historical_regrade(path: Path, regrade: ReadOnlyHistoricalRegrade) -> dict[str, Any]:
    regrade = ReadOnlyHistoricalRegrade.model_validate(regrade.model_dump())
    value = read_historical_group(path)
    originals = {row["execution_id"]: row for row in value["original_cases"]}
    if regrade.group_binding != value["binding"] or regrade.execution_id not in originals:
        raise ValueError("Read-only regrade requires the original complete-group execution identity.")
    original = originals[regrade.execution_id]
    previous = original_value(original, regrade.fact_path)
    if historical_reuse_report(path)["missing_originals"]:
        raise ValueError("Missing original bytes cannot establish a new interpretation.")
    case_number = next(index for index, row in enumerate(value["original_cases"]) if row["execution_id"] == regrade.execution_id)
    owned_keys = {key for output in value["outputs"] if output["execution_id"] == regrade.execution_id for key in output["required_originals"]}
    for check in regrade.checks:
        if (check.original_key == "source-result" and not check.pointer.startswith(f"/cases/{case_number}/")
                or check.original_key != "source-result" and check.original_key not in owned_keys):
            raise ValueError("A grader correction must inspect the same execution's actual fact originals.")
        if check.original_key not in value["originals"]:
            raise ValueError("Re-extraction must use a bound original, not a filename statistic.")
        source = path.parent / value["originals"][check.original_key]["path"]
        if original_value(json.loads(source.read_bytes()), check.pointer) != check.expected:
            raise ValueError("The re-extracted value does not match the actual original bytes.")
    known = original.get("facts", {})
    if (previous == "failed" and regrade.conclusion != "failed"
            and (known.get("known_product_failure") is True or known.get("known_defect")
                or known.get("critical_violation") is True or known.get("controlled_product_failure_observed") is True
                or regrade.fact_path in {"/product_status", "/case_status", "/target_status"}
                or any(original.get("critical_contracts", {}).get(name) == "failed" for name in ("K1", "K2"))
                or regrade.correction_kind != "grader_error")):
        raise ValueError("Changed criteria or extraction status cannot clear a known product failure.")
    destination = path.parent / "regrades" / (regrade.regrade_id + ".json")
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(regrade.model_dump_json(indent=2) + "\n")
    return historical_reuse_report(path)


class HistoricalMaterialUseReview(BaseModel):
    """Connect immutable old task identities to the canonical current-use journal."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    review_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    reviewer: str = Field(min_length=1)
    authorship: Literal["ai_applicability", "human_applicability"]
    group_binding: str
    materials_original_key: str
    historical_reference_original_key: str | None = None
    authority_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1)


def material_use_report(path: Path, value: dict[str, Any], proof: HistoricalMaterialUseReview) -> dict[str, Any]:
    from tests.support.agent_acceptance.quality import read_quality_manifest, quality_usage_authority_path, quality_usage_status, quality_executions
    if (proof.group_binding != value["binding"] or proof.reviewer.strip().casefold() == value["change"]["author"].strip().casefold()):
        raise ValueError("Current-use proof needs the independent reviewer's complete original group binding.")
    if proof.materials_original_key not in value["originals"]:
        raise ValueError("Current-use proof needs the frozen original quality material manifest.")
    frozen = read_quality_manifest(path.parent / value["originals"][proof.materials_original_key]["path"])
    quality = quality_executions(value["planned_executions"])
    required = {(task.task_id, "deepseek") for task in frozen.tasks} | {(task.task_id, "google") for task in frozen.tasks if task.purpose == "formal" and task.slot in (3, 9)}
    if (len(quality) != 84 or {(row["scenario_id"], row["protocol"]) for row in quality} != required
            or any(row["material_sha256"] != frozen.files[next(task for task in frozen.tasks if task.task_id == row["scenario_id"]).input_path.as_posix()] for row in quality)
            or not set(frozen.files.values()) <= {row["sha256"] for row in value["originals"].values()}):
        raise ValueError("Current-use proof must retain the complete original quality group and its actual input/gold bytes.")
    historical: Any = "unknown"
    if proof.historical_reference_original_key is not None:
        reference = json.loads((path.parent / value["originals"][proof.historical_reference_original_key]["path"]).read_bytes())
        if (reference["binding"] != digest({key: row for key, row in reference.items() if key != "binding"})
                or reference["materials_binding"] != frozen.binding
                or value["original_review_package"] is None or reference["package_binding"] != value["original_review_package"]["binding"]):
            raise ValueError("Historical accepted-as-of evidence must remain bound to its original materials and complete review package.")
        historical = reference.get("usage_snapshot", {"scope": "historical_as_of", "status": "legacy_accepted_reference", "kind": reference["kind"]})
    authority = quality_usage_authority_path()
    current = quality_usage_status(frozen.model_copy(update={"usage_authority": {"path": str(authority), "sha256": proof.authority_sha256}}))
    return {"review": proof.model_dump(mode="json"), "materials_binding": frozen.binding, "historical_as_of": historical,
        "current": current, "current_materials_used": bool(current["records"]) if current["status"] == "verified" else "unknown",
        "original_manifest_changed": False, "new_execution_qualification": "not_granted"}


def import_historical_material_use(path: Path, proof: HistoricalMaterialUseReview) -> dict[str, Any]:
    proof = HistoricalMaterialUseReview.model_validate(proof.model_dump())
    value = read_historical_group(path)
    if historical_reuse_report(path)["missing_originals"]:
        raise ValueError("Missing material originals keep current-use identity unknown.")
    material_use_report(path, value, proof)
    destination = path.parent / "material-use" / (proof.review_id + ".json")
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(proof.model_dump_json(indent=2) + "\n")
    return historical_reuse_report(path)

def historical_case(value: dict[str, Any], execution_id: str) -> dict[str, Any]:
    original = next(row for row in value["original_cases"] if row["execution_id"] == execution_id)
    human = value["original_human_report"]
    reviewed: dict[str, Any] = next((row for row in human["case_results"] if row["execution_id"] == execution_id), {}) if human else {}
    return {**original, "original_human": reviewed}


def complete_group_manifest(value: dict[str, Any], path: Path, key: str) -> bool:
    if key == "source-result" or key not in value["originals"]:
        raise ValueError("A legacy complete group needs its separately preserved original manifest.")
    manifest = json.loads((path.parent / value["originals"][key]["path"]).read_bytes())
    if (manifest.get("run_id") != value["run_id"] or manifest.get("execution_ids") != [row["execution_id"] for row in value["original_cases"]]):
        raise ValueError("The original complete-group manifest must preserve all original execution identities in order.")
    return True


def original_value(value: Any, pointer: str) -> Any:
    for part in pointer.lstrip("/").split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def import_historical_applicability(path: Path, review: HistoricalApplicabilityReview) -> dict[str, Any]:
    review = HistoricalApplicabilityReview.model_validate(review.model_dump())
    value = read_historical_group(path)
    if review.reviewer.strip().casefold() == value["change"]["author"].strip().casefold():
        raise ValueError("The applicability reviewer must be independent of the current change implementer.")
    if review.historical_source in {"historical_real", "actual_local"}:
        if historical_reuse_report(path)["historical_source_provenance"] == "fixed_fixture":
            raise ValueError("Native fixed_fixture originals cannot become actual evidence through applicability review.")
        if (not set(review.source_evidence_keys) - {"source-result", "original-review-package"}
                or not set(review.source_evidence_keys) <= value["originals"].keys()):
            raise ValueError("A legacy actual-source claim needs the actual bound execution originals, not source self-report.")
    if review.group_binding != value["binding"]:
        raise ValueError("Applicability must cover this same complete original group.")
    originals = {row["execution_id"]: row for row in value["original_cases"]}
    if review.complete_group_original_key is not None:
        complete_group_manifest(value, path, review.complete_group_original_key)
    seen = set()
    for fact in review.facts:
        identity = (fact.execution_id, fact.fact_path)
        if identity in seen or fact.execution_id not in originals:
            raise ValueError("Applicability cannot duplicate or invent a product execution.")
        seen.add(identity)
        original_value(historical_case(value, fact.execution_id), fact.fact_path)
        if fact.regrade_id is not None:
            grade = ReadOnlyHistoricalRegrade.model_validate_json((path.parent / "regrades" / (fact.regrade_id + ".json")).read_bytes())
            if ((grade.execution_id, grade.fact_path) != identity or grade.author.strip().casefold() == review.reviewer.strip().casefold()):
                raise ValueError("The regrade needs its exact fact and an independent applicability reviewer.")
        for impact in fact.matrix.values():
            if (impact.status == "unaffected" and not impact.evidence_keys
                    or not set(impact.evidence_keys) <= value["originals"].keys()):
                raise ValueError("Unaffected facts require specific preserved original evidence.")
    if historical_reuse_report(path)["missing_originals"]:
        raise ValueError("Missing original bytes keep applicability unknown.")
    destination = path.parent / "applicability" / (review.review_id + ".json")
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(review.model_dump_json(indent=2) + "\n")
    return historical_reuse_report(path)

def import_historical_group(result_path: Path, *, destination: Path, change: HistoricalChange,
                            review_package: Path | None = None, state_originals: dict[str, ReviewOriginal] | None = None) -> Path:
    """Freeze the original complete group, reviews and state before applicability review."""
    change = HistoricalChange.model_validate(change.model_dump())
    raw = result_path.read_bytes()
    result = json.loads(raw)
    ids = [row["execution_id"] for row in result["cases"]]
    planned = result.get("planned_executions", [])
    frozen_plan = result["version"].get("plan")
    if frozen_plan is not None:
        frozen_ids = [row["execution_id"] for row in frozen_plan.get("executions", [])]
        frozen_pairs = {(case, protocol) for case in frozen_plan.get("cases", []) for protocol in frozen_plan["protocols"]}
        observed_pairs = {(row["case_id"], row["protocol"]) for row in result["cases"]}
        if (frozen_ids and (set(frozen_ids) != set(ids) or frozen_plan["executions"] != planned)
                or not frozen_ids and frozen_pairs != observed_pairs):
            raise ValueError("Preserve the complete group from the original frozen plan, including failures and unexecuted members.")
    if (not ids or len(ids) != len(set(ids)) or result.get("planned_denominator", len(ids)) != len(ids)
            or planned and (len(planned) != len(ids) or {row["execution_id"] for row in planned} != set(ids))
            or any(row.get("facts", {}).get("source_run_id", result["run_id"]) != result["run_id"] for row in result["cases"])):
        raise ValueError("Import the original complete group; copied executions and cross-batch success collections are invalid.")
    if review_package is not None:
        source_package = read_package(review_package)
        if (source_package["result_sha256"] != sha256(raw).hexdigest() or source_package["original_cases"] != result["cases"]
                or source_package["planned_executions"] != planned):
            raise ValueError("The original human package must cover this complete group.")
        human = review_report(review_package)
    else:
        human = None
    package_path = export_review(result_path, destination / "review-originals")
    package = read_package(package_path)
    frozen_outputs = source_package["outputs"] if review_package is not None else package["outputs"]
    originals = {name: {**row, "path": "review-originals/" + row["path"]} for name, row in package["originals"].items()}
    if review_package is not None:
        provided = destination / "provided-review"
        provided.mkdir()
        (provided / "review-package.json").write_bytes(review_package.read_bytes())
        originals["original-review-package"] = {"path": "provided-review/review-package.json", "sha256": sha256(review_package.read_bytes()).hexdigest()}
        for key, item in source_package["originals"].items():
            body = (review_package.parent / item["path"]).read_bytes()
            if key in originals and originals[key]["sha256"] != item["sha256"]:
                raise ValueError("The supplied original review cannot replace a product original.")
            target = "provided-review/" + item["sha256"]
            if not (destination / target).exists():
                (destination / target).write_bytes(body)
            originals[key] = {**item, "path": target}
        assert human is not None
        history_index = Path(source_package["source_result"]).with_name("review-exports.json")
        package_paths = {review_package, *(Path(row["package"]) for row in json.loads(history_index.read_bytes()))}
        accepted_review_fields = {digest(review) for output in human["outputs"] for review in output["history"]}
        for old_package in package_paths:
            for old_review in (old_package.parent / "reviews").glob("*.json"):
                body = old_review.read_bytes()
                if digest(json.loads(body)) in accepted_review_fields:
                    fingerprint = sha256(body).hexdigest()
                    target = "provided-review/human-original-" + fingerprint + ".json"
                    (destination / target).write_bytes(body)
                    originals["original-human-bytes:" + fingerprint] = {"path": target, "sha256": fingerprint}
        for output in human["outputs"]:
            for review in output["history"]:
                target = "provided-review/human-" + digest(review) + ".json"
                (destination / target).write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                originals["original-human:" + digest(review)] = {"path": target, "sha256": sha256((destination / target).read_bytes()).hexdigest(), "purpose": "unchanged original human attestation fields"}
    (destination / "source-result.json").write_bytes(raw)
    originals["source-result"] = {"path": "source-result.json", "sha256": sha256(raw).hexdigest()}
    for label, row in (state_originals or {}).items():
        supplied = ReviewOriginal.model_validate(row)
        body = supplied.path.read_bytes()
        if label in originals or sha256(body).hexdigest() != supplied.sha256:
            raise ValueError("Historical state requires distinct labels and the exact original bytes.")
        target = "state/" + supplied.sha256
        (destination / "state").mkdir(exist_ok=True)
        (destination / target).write_bytes(body)
        originals[label] = {"path": target, "sha256": supplied.sha256}
    contexts = {}
    for case in result["cases"]:
        selected = [row for row in frozen_outputs if row["execution_id"] == case["execution_id"]]
        keys = sorted({key for row in selected for key in row["required_originals"]})
        records = [json.loads((destination / originals[key]["path"]).read_bytes()) for key in keys if key.endswith("/conversation")]
        tasks = [turn["task"] for record in records for turn in record.get("turns", []) if turn.get("task")]
        contexts[case["execution_id"]] = {"source": "version.source_files", "protocol": case["protocol"],
            "model": [(task.get("provider_config") or {}).get("model", "unknown") for task in tasks] or "unknown",
            "prompt": {"original_keys": keys, "input_sha256": [digest(row["actual_input"]) for row in selected]},
            "scope": [task.get("scope", "unknown") for task in tasks] or "unknown",
            "permissions": [task.get("approval", "unknown") for task in tasks] or "unknown",
            "save": {key: originals[key]["sha256"] for key in keys if key.endswith("/conversation")},
            "summary": [row["output_id"] for row in selected if row["type"] == "summary"],
            "environment": "version.python/platform/packages", "material": [row for row in planned if row["execution_id"] == case["execution_id"]],
            "date": [record.get("created_at", "unknown") for record in records] or "unknown",
            "criteria": [row["standard"] for row in selected] or "unknown"}
    value = {"schema_version": 1, "source_result_sha256": sha256(raw).hexdigest(), "run_id": result["run_id"],
        "mode": result["mode"], "version": result["version"], "original_cases": result["cases"],
        "planned_executions": planned, "original_denominator": len(ids), "outputs": frozen_outputs, "execution_contexts": contexts,
        "originals": originals, "original_human_report": human,
        "original_review_package": source_package if review_package is not None else None,
        "user_acceptance": source_package["user_acceptance"] if review_package is not None else package["user_acceptance"],
        "change": change.model_dump(mode="json"), "imported_at": datetime.now(timezone.utc).isoformat(),
        "original_context": {name: result["version"].get(name, "unknown") for name in FACETS},
        "sending_authorized": False}
    value["original_context"].update(source=result["version"].get("source_files", "unknown"),
        environment={name: result["version"].get(name, "unknown") for name in ("python", "platform", "packages")},
        protocol=sorted({row["protocol"] for row in result["cases"]}),
        criteria=sorted({row["standard"]["version"] for row in frozen_outputs}))
    value["binding"] = digest(value)
    path = destination / "historical-group.json"
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "applicability").mkdir()
    (destination / "regrades").mkdir()
    (destination / "material-use").mkdir()
    return path


def read_historical_group(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_bytes())
    if digest({key: row for key, row in value.items() if key != "binding"}) != value["binding"]:
        raise ValueError("The frozen historical group changed.")
    return value


def historical_reuse_report(path: Path) -> dict[str, Any]:
    value = read_historical_group(path)
    missing = []
    for key, row in value["originals"].items():
        source = (path.parent / row["path"]).resolve()
        if (not source.is_relative_to(path.parent.resolve()) or not source.is_file()
                or sha256(source.read_bytes()).hexdigest() != row["sha256"]):
            missing.append(key)
    human = value["original_human_report"]
    native = [row for key, item in value["originals"].items() if key.endswith("/exchange") and key not in missing
        for row in json.loads((path.parent / item["path"]).read_bytes()).get("exchanges", [])]
    provenance = ("fixed_fixture" if any(row.get("transport_provenance") == "fixed_fixture" for row in native)
        else "real_http" if native and all(row.get("transport_provenance") == "real_http" for row in native) and value["mode"] == "live"
        else "unknown")
    reviews = [HistoricalApplicabilityReview.model_validate_json(row.read_bytes()) for row in sorted((path.parent / "applicability").glob("*.json"))]
    if any(row.group_binding != value["binding"] or row.reviewer.strip().casefold() == value["change"]["author"].strip().casefold() for row in reviews):
        raise ValueError("Historical applicability lost its independent complete-group binding.")
    if provenance == "unknown":
        source_claims = {review.historical_source for review in reviews if review.historical_source != "unknown"}
        if len(source_claims) == 1 and all(set(review.source_evidence_keys) - {"source-result", "original-review-package"} for review in reviews if review.historical_source != "unknown"):
            provenance = {"historical_real": "independently_reviewed_real", "actual_local": "independently_reviewed_local", "offline": "fixed_fixture"}[next(iter(source_claims))]
    regrades = [ReadOnlyHistoricalRegrade.model_validate_json(row.read_bytes()) for row in sorted((path.parent / "regrades").glob("*.json"))]
    if any(row.group_binding != value["binding"] for row in regrades):
        raise ValueError("A historical regrade lost its original group binding.")
    grades = {row.regrade_id: row for row in regrades}
    complete = value["version"].get("plan") is not None or any(complete_group_manifest(value, path, review.complete_group_original_key) for review in reviews if review.complete_group_original_key is not None)
    facts = []
    for identity in dict.fromkeys((fact.execution_id, fact.fact_path) for review in reviews for fact in review.facts):
        original = next(row for row in value["original_cases"] if row["execution_id"] == identity[0])
        selected = [(review, fact) for review in reviews for fact in review.facts if (fact.execution_id, fact.fact_path) == identity]
        applicable = complete and not missing and all(set(fact.matrix) == set(FACETS) and all(impact.status == "unaffected" and impact.evidence_keys for impact in fact.matrix.values()) for _, fact in selected)
        prior = original_value(historical_case(value, identity[0]), identity[1])
        values = [grades[fact.regrade_id].conclusion if fact.regrade_id is not None else prior for _, fact in selected]
        interpretations = {digest(item): item for item in values}
        if len(interpretations) != 1:
            applicable = False
        interpreted = next(iter(interpretations.values()))
        status = interpreted if isinstance(interpreted, str) and interpreted in {"passed", "failed", "unknown", "not_applicable", "not_measured", "not_executed"} else "verified"
        facts.append({"execution_id": identity[0], "fact_path": identity[1], "original_status": prior, "original_value": prior,
            "current_value": interpreted if applicable else None, "current_status": status if applicable else "unknown", "applicability": "unaffected" if applicable else "unknown",
            "reviews": [{**review.model_dump(mode="json", exclude={"facts"}), "matrix": fact.model_dump(mode="json")["matrix"]} for review, fact in selected]})
    failures = [{"execution_id": row["execution_id"], "original_statuses": {name: row.get(name) for name in
        ("product_status", "case_status", "target_status", "deterministic_status", "content_status", "critical_contracts")}}
        for row in value["original_cases"] if any(row.get(name) == "failed" for name in
            ("product_status", "case_status", "target_status", "deterministic_status", "content_status")) or "failed" in row.get("critical_contracts", {}).values()]
    accepted_originals = bool(human) and len(human["case_results"]) == value["original_denominator"] and all(row["case_status"] == "passed" for row in human["case_results"])
    applicable_cases = {row["execution_id"] for row in facts if row["fact_path"] == "/original_human/case_status" and row["current_status"] == "passed"}
    group_passed = provenance in {"real_http", "independently_reviewed_real", "independently_reviewed_local"} and accepted_originals and not failures and applicable_cases == {row["execution_id"] for row in value["original_cases"]}
    return {"group_binding": value["binding"], "run_id": value["run_id"], "original_denominator": value["original_denominator"],
        "historical_source_provenance": provenance,
        "original_context": value["original_context"], "change": value["change"], "group_completeness": "verified" if complete else "unknown",
        "executions": [{"execution_id": row["execution_id"], "original": row, "original_context": value["execution_contexts"][row["execution_id"]],
            "applicability": "scoped_facts" if any(fact["execution_id"] == row["execution_id"] and fact["applicability"] == "unaffected" for fact in facts) else "unknown"} for row in value["original_cases"]],
        "facts": facts, "known_product_failures": failures,
        "material_use": [material_use_report(path, value, HistoricalMaterialUseReview.model_validate_json(row.read_bytes()))
            if not missing else {"current": {"status": "unknown", "reason": "Required original bytes are missing."}, "historical_as_of": "preserved_in_originals"}
            for row in sorted((path.parent / "material-use").glob("*.json"))],
        "regrades": [{**row.model_dump(mode="json"), "old_judgment": original_value(next(case for case in value["original_cases"] if case["execution_id"] == row.execution_id), row.fact_path),
            "new_judgment": row.conclusion, "product_executions_added": 0} for row in regrades],
        "original_human_report": human, "human_review": "preserved_original" if human and any(row["history"] for row in human["outputs"]) else "not_performed",
        "actual_human_reviews": sum(len(row["history"]) for row in human["outputs"]) if human else 0,
        "user_acceptance": value["user_acceptance"], "missing_originals": missing,
        "layers": {"offline": "historical_originals" if value["mode"] == "offline" or provenance == "fixed_fixture" else "not_measured",
            "historical_real": "scoped_facts" if provenance in {"real_http", "independently_reviewed_real"} and any(fact["applicability"] == "unaffected" for fact in facts) else "not_measured",
            "historical_reuse": "scoped_facts" if any(fact["applicability"] == "unaffected" for fact in facts) else "pending_review",
            "current_real": "not_measured", "untested": "required coverage not established"},
        "required_coverage_gaps": [{"execution_id": row["execution_id"], "original_status": row.get("execution_status", "unknown"),
            "reason": "Original failed, invalid, disputed, unexecuted or required facts remain unreviewed; scoped reuse does not complete a new batch."}
            for row in value["original_cases"] if row["execution_id"] not in applicable_cases],
        "new_product_executions": 0, "all_passed": group_passed, "version_admission": "not_established",
        "actual_baseline": "not_established", "judge_qualification": "not_established", "sending_authorized": False}


def export_historical_report(path: Path, destination: Path) -> Path:
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(historical_reuse_report(path), ensure_ascii=False, indent=2) + "\n")
    return destination


def historical_group_reports(paths: tuple[Path, ...]) -> list[dict[str, Any]]:
    from tests.support.agent_acceptance.core import SharedControlError
    reports = [historical_reuse_report(path) for path in paths]
    identities = [(report["run_id"], row["execution_id"]) for report in reports for row in report["executions"]]
    if len(identities) != len(set(identities)):
        raise SharedControlError("Historical groups cannot duplicate original execution identities in a current report.")
    return reports
