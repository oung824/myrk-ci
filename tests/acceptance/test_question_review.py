"""Question conclusions and review lifecycle through the maintained public entry."""

import asyncio
import json
from hashlib import sha256

import pytest

from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, OfflineSession, Reply, builtin_registry, run_plan
from tests.support.agent_acceptance.catalogue import complete_catalogue
from tests.support.agent_acceptance.ordinary import register


def _ordinary_registry() -> CaseRegistry:
    registry = CaseRegistry()
    register(registry)
    complete_catalogue(registry)
    return registry


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_wrong_fact_is_a_product_failure_even_when_transport_and_evidence_work(tmp_path, protocol):
    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [Reply(text="The result is 41.")], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.tool_status == "passed"
    assert case.product_status == case.target_status == case.deterministic_status == "failed"
    assert case.content_status == "pending_review"
    assert case.user_acceptance == "pending"
    assert case.execution_id == f"{result.run_id}:ordinary.question:{protocol}:1"
    report = json.loads(result.result_path.read_bytes())
    assert report["cases"][0]["target_status"] == "failed"
    assert report["cases"][0]["screening"]["request_cap"] == 4
    from tests.support.agent_acceptance import export_review, review_report
    package = export_review(result.result_path, tmp_path / "review")
    assert review_report(package)["case_results"][0]["case_status"] == "failed"

@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_attachment_has_actual_read_only_approval_and_source_binding(tmp_path, protocol):
    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.attachment"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry()))
    case = result.cases[0]
    record = json.loads(case.evidence["conversation"].read_bytes())
    approval = record["turns"][0]["task"]["approval"]
    assert approval["access"]["read_only"] is True
    assert approval["condition"] == "never"
    assert case.facts["source"]["unchanged"] is True
    assert case.facts["source"]["citation"] == "purchase.md:1"
    assert case.facts["source"]["native_input_ids"]
    assert not record["turns"][0]["actions"]
    assert case.screening["scenario_id"] == ("G01" if protocol == "google" else "D02")


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_encoded_long_answer_keeps_original_bytes_and_complete_response(tmp_path, protocol, acceptance_public_root):
    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.encoded-long"], protocols=[protocol]),
        root=acceptance_public_root, registry=_ordinary_registry()))
    case = result.cases[0]
    assert case.product_status == "passed", case.facts
    assert case.evidence["attachment"].read_bytes().startswith(b"\xef\xbb\xbf")
    assert case.facts["source"]["encoding"] == "UTF-8 with BOM; CRLF line endings"
    assert len(case.facts["answer"]) > 4096
    assert case.facts["answer"].endswith("END-ORIGINAL-7319")
    assert case.screening["request_cap"] == 12

@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_review_export_import_preserves_originals_and_old_decisions(tmp_path, protocol):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews, review_report

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry()))
    original = result.result_path.read_bytes()
    package_path = export_review(result.result_path, tmp_path / "review-v1", final_reviewer="Final reviewer fixture")
    package = json.loads(package_path.read_bytes())
    output = package["outputs"][0]
    assert output["type"] == "main_answer"
    assert {row["content_status"] for row in review_report(package_path)["outputs"]} == {"pending_review"}
    review = HumanReview(review_id="first-human-form", output_id=output["output_id"], reviewer="Named human form fixture",
        authorship="human", signed_at="2026-11-16T06:00:00+08:00", scope="All listed original input, output, source and effects; synthetic form contract only",
        package_sha256=package["binding"], criteria_version=output["standard"]["version"],
        output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
        conclusions={name: "passed" for name in output["standard"]["dimensions"]}, reasons={})
    import_reviews(package_path, [review])
    imported = review_report(package_path)
    assert imported["outputs"][0]["content_status"] == "passed"
    assert imported["outputs"][0]["semantic_status"] == "not_measured"
    assert imported["user_acceptance"] == "pending"
    assert imported["case_results"][0]["case_status"] == "pending_review"
    assert imported["case_results"][0]["critical_contracts"]["K1"] == "unknown"
    import_reviews(package_path, [review])
    assert len(review_report(package_path)["outputs"][0]["history"]) == 1
    revised = review.model_copy(update={"review_id": "second-human-form", "reviewer": "Other human form fixture",
        "conclusions": {name: "failed" for name in review.conclusions}, "reasons": {name: "The fixture records a disagreement." for name in review.conclusions}})
    import_reviews(package_path, [revised])
    assert review_report(package_path)["outputs"][0]["content_status"] == "pending_review"
    assert len(review_report(package_path)["outputs"][0]["history"]) == 2
    assert result.result_path.read_bytes() == original
    assert json.loads((tmp_path / "run" / "review-exports.json").read_bytes())[0]["package"] == str(package_path)

def test_case_review_leaves_critical_status_unknown_without_its_source_output(tmp_path):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=["deepseek"]),
        root=tmp_path / "run", registry=_ordinary_registry()))
    original = json.loads(result.result_path.read_bytes())
    case = original["cases"][0]
    case["facts"]["output_critical_dimensions"] = {
        "review_reason": {"K2": {"description": "Check the actual review reason.",
            "applicable": True, "critical": True, "case_evidence_complete": False}},
    }
    assert case["critical_contracts"]["K2"] == "unknown"
    incomplete = result.result_path.with_name("synthetic-missing-review-reason.json")
    incomplete.write_text(json.dumps(original), encoding="utf-8")
    custom_standard = {"version": "synthetic-custom", "dimensions": {
        "K2": {"description": "Optional notes about approval in the main answer.", "applicable": True},
    }}
    package_path = export_review(incomplete, tmp_path / "review", standards={"main_answer": custom_standard})
    package = json.loads(package_path.read_bytes())
    output, = package["outputs"]
    assert output["type"] == "main_answer"
    form = HumanReview(review_id="synthetic-main-note", output_id=output["output_id"], reviewer="Synthetic review fixture",
        authorship="human", signed_at="2026-11-24T13:00:00+08:00", scope="Synthetic import and aggregation regression",
        package_sha256=package["binding"], criteria_version=output["standard"]["version"],
        output_sha256=output["sha256"], reviewed_originals=output["required_originals"], conclusions={"K2": "passed"}, reasons={})

    report = import_reviews(package_path, [form])

    assert report["outputs"][0]["dimensions"]["K2"] == "passed"
    assert report["case_results"][0]["critical_contracts"]["K2"] == "unknown"


@pytest.mark.parametrize("main_verdict,child_verdict,critical_status,case_status", [
    ("passed", None, "unknown", "pending_review"),
    ("failed", None, "failed", "failed"),
    ("passed", "passed", "passed", "passed"),
    ("passed", "unknown", "unknown", "pending_review"),
    ("passed", "failed", "failed", "failed"),
])
def test_shared_critical_dimension_requires_each_declared_output_type(
    tmp_path, main_verdict, child_verdict, critical_status, case_status,
):
    from tests.support.agent_acceptance import HumanReview, import_reviews
    from tests.support.agent_acceptance.output_review import digest

    dimension = {"description": "Check claims against the saved originals.", "applicable": True,
        "critical": True, "case_evidence_complete": True}
    standard = {"version": "critical-coverage-fixture", "dimensions": {"K4": dimension}}
    case = {"execution_id": "synthetic-1", "case_id": "synthetic", "protocol": "deepseek"}
    originals = {}
    outputs = []
    for kind, verdict in (("main_answer", main_verdict), ("child_report", child_verdict)):
        if verdict is None:
            continue
        path = tmp_path / f"{kind}.txt"
        body = f"Synthetic {kind} original".encode()
        path.write_bytes(body)
        fingerprint = sha256(body).hexdigest()
        originals[kind] = {"path": path.name, "sha256": fingerprint}
        outputs.append({**case, "output_id": kind, "type": kind, "sha256": fingerprint, "standard": standard,
            "required_originals": [kind], "evidence_complete": True, "provenance": "current_real"})

    package = {"run_id": "synthetic-coverage", "source_result": str(tmp_path / "result.json"),
        "originals": originals, "outputs": outputs, "original_cases": [{**case,
            "facts": {"output_critical_dimensions": {"main_answer": {"K4": dimension}, "child_report": {"K4": dimension}}},
            "critical_contracts": {"K1": "passed", "K2": "passed", "K3": "passed", "K4": "unknown", "K5": "passed"},
            "tool_status": "passed", "product_status": "passed", "target_status": "passed",
            "evidence_status": "complete", "deterministic_status": "passed"}]}
    package["binding"] = digest(package)
    package_path = tmp_path / "review-package.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    (tmp_path / "review-exports.json").write_text("[]", encoding="utf-8")
    (tmp_path / "reviews").mkdir()
    verdicts = {"main_answer": main_verdict, "child_report": child_verdict}
    forms = [HumanReview(review_id=output["output_id"], output_id=output["output_id"], reviewer="Synthetic form tester",
        authorship="human", signed_at="2026-11-26T12:00:00+08:00", scope="Synthetic coverage test",
        package_sha256=package["binding"], criteria_version=standard["version"], output_sha256=output["sha256"],
        reviewed_originals=output["required_originals"], conclusions={"K4": verdicts[output["type"]]},
        reasons={"K4": "Result supplied by the regression fixture."}) for output in outputs]

    reviewed = import_reviews(package_path, forms)["case_results"][0]
    assert reviewed["critical_contracts"]["K4"] == critical_status
    assert reviewed["case_status"] == case_status


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_five_output_types_use_actual_sources_and_keep_domain_criteria(tmp_path, protocol, acceptance_public_root):
    from tests.support.agent_acceptance import export_review, review_report

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question", "delegation.delivery", "review.evidence-recovery", "memory.personal", "compaction.short"], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry()))
    assert all(case.tool_status == "passed" for case in result.cases), [(case.case_id, case.facts) for case in result.cases]
    package_path = export_review(result.result_path, tmp_path / "review")
    package = json.loads(package_path.read_bytes())
    assert {output["type"] for output in package["outputs"]} == {"main_answer", "child_report", "review_reason", "memory", "summary"}
    assert all(output["actual_input"] and output["required_originals"] for output in package["outputs"])
    assert all(output["text"] and output["evidence_complete"] for output in package["outputs"])
    assert {output["type"] for output in package["outputs"] if output["case_id"] == "ordinary.question"} == {"main_answer"}
    assert all("pending_ownership" not in output["standard"]["dimensions"] for output in package["outputs"])
    assert review_report(package_path)["pending_outputs"] == len(package["outputs"])


def test_review_cannot_pass_unknown_missing_evidence_or_ai_authorship(tmp_path):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.interrupted"]), root=tmp_path / "run", registry=_ordinary_registry()))
    package_path = export_review(result.result_path, tmp_path / "review")
    package = json.loads(package_path.read_bytes())
    output = package["outputs"][0]
    values = dict(review_id="unsigned-test", output_id=output["output_id"], reviewer="Named form fixture", authorship="ai",
        signed_at="2026-11-16T06:00:00+08:00", scope="Synthetic import contract", package_sha256=package["binding"],
        criteria_version=output["standard"]["version"], output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
        conclusions={name: "passed" for name in output["standard"]["dimensions"]}, reasons={})
    with pytest.raises(ValueError):
        HumanReview(**values)
    values["authorship"] = "human"
    with pytest.raises(ValueError, match="incomplete"):
        import_reviews(package_path, [HumanReview(**values)])
    original = next(iter(package["originals"].values()))
    (package_path.parent / original["path"]).unlink()
    with pytest.raises((ValueError, OSError)):
        import_reviews(package_path, [HumanReview(**values)])


def test_catalogue_freezes_screening_caps_before_any_execution():
    rows = {row["case_id"]: row for row in _ordinary_registry().catalogue()}
    assert rows["ordinary.question"]["screening"] == {"deepseek": {"scenario_id": "D01", "request_cap": 4}}
    assert rows["ordinary.attachment"]["screening"] == {"deepseek": {"scenario_id": "D02", "request_cap": 6}, "google": {"scenario_id": "G01", "request_cap": 6}}
    assert rows["ordinary.encoded-long"]["screening"] == {"deepseek": {"scenario_id": "D03", "request_cap": 12}}

@pytest.mark.acceptance_desktop
@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_formal_attachment_uses_the_same_read_only_product_settings(tmp_path, protocol):
    result = asyncio.run(run_plan(AcceptancePlan(cases=["formal.attachment"], protocols=[protocol]),
        root=tmp_path / "run", registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == case.tool_status == "passed", case.facts
    assert case.facts["attachment_read_only"] is True
    assert case.facts["attachment_no_ordinary_approval"] is True
    assert case.facts["attachment_bytes_sha256"] == sha256(b"Purchase threshold: 800.\n").hexdigest()
    assert not case.facts["attachment_forbidden_effects"]

@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_correct_facts_and_recovered_transport_still_need_content_review(tmp_path, protocol):
    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [Reply(status=503), Reply(text="6 × 7 = 42.\n")], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.product_status == case.target_status == case.deterministic_status == "passed"
    assert case.case_status == "pending_review"
    assert case.semantic_status == "not_measured"
    assert case.facts["product_attempts"] == 2


def test_final_reviewer_resolves_disagreement_without_hiding_later_reviews(tmp_path):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews, review_report

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    package_path = export_review(result.result_path, tmp_path / "review", final_reviewer="Final form fixture")
    package = json.loads(package_path.read_bytes())
    output = package["outputs"][0]
    review = HumanReview(review_id="first", output_id=output["output_id"], reviewer="First form fixture", authorship="human",
        signed_at="2026-11-16T06:00:00+08:00", scope="Synthetic human form contract; all originals", package_sha256=package["binding"],
        criteria_version=output["standard"]["version"], output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
        conclusions={name: "passed" for name in output["standard"]["dimensions"]}, reasons={})
    import_reviews(package_path, [review])
    second = review.model_copy(update={"review_id": "second", "conclusions": {name: "failed" for name in review.conclusions},
        "reasons": {name: "A disputed conclusion in the synthetic form." for name in review.conclusions}})
    import_reviews(package_path, [second])
    final = review.model_copy(update={"review_id": "z-final", "reviewer": "Final form fixture", "resolves": ["first", "second"],
        "reasons": {name: "Read both prior forms and originals; resolving this test dispute." for name in review.conclusions}})
    import_reviews(package_path, [final])
    import_reviews(package_path, [final])
    assert review_report(package_path)["outputs"][0]["content_status"] == "passed"
    later = second.model_copy(update={"review_id": "third"})
    import_reviews(package_path, [later])
    assert review_report(package_path)["outputs"][0]["content_status"] == "pending_review"
    repeated_final = final.model_copy(update={"review_id": "another-final", "resolves": ["first", "second", "z-final", "third"]})
    with pytest.raises(ValueError, match="once"):
        import_reviews(package_path, [repeated_final])
    copied = export_review(result.result_path, tmp_path / "same-standard-copy", final_reviewer="Final form fixture")
    copied_final = repeated_final.model_copy(update={"package_sha256": json.loads(copied.read_bytes())["binding"]})
    with pytest.raises(ValueError, match="once"):
        import_reviews(copied, [copied_final])


def test_review_cli_exports_the_same_pending_originals(tmp_path):
    import subprocess
    import sys

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    exported = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance", "export-review",
        str(result.result_path), str(tmp_path / "review")], check=False, capture_output=True, text=True, encoding="utf-8")
    assert exported.returncode == 0, exported.stderr
    package_path = json.loads(exported.stdout)["package"]
    report = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance", "review-report", package_path],
        check=False, capture_output=True, text=True, encoding="utf-8")
    assert report.returncode == 0, report.stderr
    assert json.loads(report.stdout)["pending_outputs"] == 1


def test_export_rejects_an_original_changed_after_the_run(tmp_path):
    from tests.support.agent_acceptance import export_review

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    exchange = result.cases[0].evidence["exchange"]
    exchange.write_text(exchange.read_text(encoding="utf-8").replace("The result is 42.", "The result is 41."), encoding="utf-8")
    with pytest.raises(ValueError, match="original"):
        export_review(result.result_path, tmp_path / "review")


def test_import_rejects_two_different_decisions_with_the_same_id(tmp_path):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews, review_report

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    package_path = export_review(result.result_path, tmp_path / "review")
    package = json.loads(package_path.read_bytes())
    output = package["outputs"][0]
    review = HumanReview(review_id="same-id", output_id=output["output_id"], reviewer="Named form fixture", authorship="human",
        signed_at="2026-11-16T06:00:00+08:00", scope="Synthetic human form contract", package_sha256=package["binding"],
        criteria_version=output["standard"]["version"], output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
        conclusions={name: "passed" for name in output["standard"]["dimensions"]}, reasons={})
    changed = review.model_copy(update={"conclusions": {name: "failed" for name in review.conclusions},
        "reasons": {name: "Conflicting test form." for name in review.conclusions}})
    with pytest.raises(ValueError, match="ID"):
        import_reviews(package_path, [review, changed])
    assert not review_report(package_path)["outputs"][0]["history"]


def test_new_standard_keeps_old_package_and_rejects_false_not_applicable(tmp_path):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews, review_report

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    original = result.result_path.read_bytes()
    first = export_review(result.result_path, tmp_path / "v1")
    first_bytes = first.read_bytes()
    standards = {"main_answer": {"version": "question-review-v2", "dimensions": {
        "accuracy": {"description": "Six times seven equals 42.", "applicable": True},
        "attachment_citation": {"description": "Cite the selected attachment.", "applicable": False,
            "reason": "This frozen question has no selected attachment."}}}}
    second = export_review(result.result_path, tmp_path / "v2", standards=standards)
    package = json.loads(second.read_bytes())
    output = package["outputs"][0]
    old_output = json.loads(first_bytes)["outputs"][0]
    assert output["output_id"] == old_output["output_id"]
    assert output["sha256"] == old_output["sha256"]
    review = HumanReview(review_id="new-standard-form", output_id=output["output_id"], reviewer="Named form fixture", authorship="human",
        signed_at="2026-11-16T06:00:00+08:00", scope="Synthetic form contract; complete listed originals", package_sha256=package["binding"],
        criteria_version=output["standard"]["version"], output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
        conclusions={"accuracy": "not_applicable", "attachment_citation": "not_applicable"},
        reasons={"accuracy": "This condition is deliberately false.", "attachment_citation": "No selected attachment."})
    with pytest.raises(ValueError, match="Not-applicable"):
        import_reviews(second, [review])
    correct = review.model_copy(update={"conclusions": {"accuracy": "unknown", "attachment_citation": "not_applicable"},
        "reasons": {"accuracy": "The form fixture has no actual human signature.", "attachment_citation": "No selected attachment."}})
    import_reviews(second, [correct])
    assert review_report(second)["outputs"][0]["content_status"] == "pending_review"
    with pytest.raises(ValueError, match="frozen standard"):
        import_reviews(first, [correct])
    assert first.read_bytes() == first_bytes
    assert result.result_path.read_bytes() == original
    assert len(json.loads(result.result_path.with_name("review-exports.json").read_bytes())) == 2


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_long_stream_interruption_keeps_partial_output_and_failure(tmp_path, protocol):
    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [Reply(tool="read_authorized_text"), *[Reply(text="PARTIAL-ORIGINAL-7319", interrupt=True)] * 3], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.encoded-long"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.tool_status == "passed", case.facts
    assert case.target_status == case.case_status == "failed"
    assert case.facts["response_complete"] is False
    assert "PARTIAL-ORIGINAL-7319" in case.facts["answer"]
    exchange = json.loads(case.evidence["exchange"].read_bytes())
    assert any("PARTIAL-ORIGINAL-7319" in item["received_utf8"] for item in exchange["exchanges"])
    assert case.evidence["attachment"].read_bytes().startswith(b"\xef\xbb\xbf")



def test_one_import_cannot_contain_two_final_resolutions(tmp_path):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews, review_report

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    package_path = export_review(result.result_path, tmp_path / "review", final_reviewer="Final form fixture")
    package = json.loads(package_path.read_bytes())
    output = package["outputs"][0]
    first = HumanReview(review_id="prior", output_id=output["output_id"], reviewer="First form fixture", authorship="human",
        signed_at="2026-11-16T06:00:00+08:00", scope="Synthetic form contract", package_sha256=package["binding"],
        criteria_version=output["standard"]["version"], output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
        conclusions={name: "passed" for name in output["standard"]["dimensions"]}, reasons={})
    final = first.model_copy(update={"review_id": "first-final", "reviewer": "Final form fixture", "resolves": ["prior"],
        "reasons": {name: "Read originals and the prior synthetic form." for name in first.conclusions}})
    repeated = final.model_copy(update={"review_id": "second-final", "resolves": ["prior", "first-final"]})
    with pytest.raises(ValueError, match="once"):
        import_reviews(package_path, [first, final, repeated])
    assert not review_report(package_path)["outputs"][0]["history"]


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_native_response_view_drives_output_units_after_real_sdk_decoding(tmp_path, protocol, encoding):
    import gzip
    import httpx2
    from tests.support.agent_acceptance.recorded_response import RecordedResponseStream, capture_response_metadata

    delivered = []

    class NativeBody(httpx2.AsyncByteStream):
        def __init__(self, body):
            self.body = body

        async def __aiter__(self):
            body, self.body = self.body, b""
            if body:
                yield body

    class NativeTransport(OfflineSession):
        async def respond(self, request):
            response = super().respond(request)
            await response.aclose()
            if protocol == "google":
                events = [{"candidates": [{"content": {"role": "model", "parts": [{"text": "The result is 42."}]}, "finishReason": "STOP"}]}]
            else:
                events = [{"id": "native-proof", "object": "chat.completion.chunk", "created": 1, "model": self.config.model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "The result is 42."}, "finish_reason": None}]},
                    {"id": "native-proof", "object": "chat.completion.chunk", "created": 1, "model": self.config.model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            body = b"".join(b"data: " + json.dumps(event).encode("utf-8") + b"\r\n\r\n" for event in events)
            if protocol != "google":
                body += b"data: [DONE]\r\n\r\n"
            if protocol != "google":
                body = b": native comment\r\n\r\n" + body
            payload = gzip.compress(body, mtime=0) if encoding == "gzip" else body
            delivered.append(payload)
            record = self.exchanges[-1]
            record["received_utf8"] = ""
            record.pop("received_base64", None)
            record.pop("response_body_state", None)
            headers = {"content-type": "text/event-stream", "content-encoding": encoding}
            actual = httpx2.Response(200, headers=headers,
                stream=RecordedResponseStream(NativeBody(payload), record, lambda: None))
            record["response_metadata"] = capture_response_metadata(actual, source="fixed_fixture")
            return actual

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=NativeTransport))
    case = result.cases[0]
    assert case.product_status == "passed", case.facts
    assert case.facts["answer"] == "The result is 42."
    review = json.loads(case.evidence["semantic_review"].read_bytes())
    assert len(review["outputs"]) == 1
    output = review["outputs"][0]
    from base64 import b64decode
    captured = json.loads(case.evidence["exchange"].read_bytes())["exchanges"][0]
    assert b64decode(captured["received_base64"]) == delivered[0]
    assert output["response_evidence"]["raw_sha256"] == sha256(delivered[0]).hexdigest()
    assert output["text"] == "The result is 42."
    assert output["evidence_complete"] is True
    assert case.case_status == "pending_review"


def test_uncontested_failure_is_not_hidden_by_an_unknown_dimension(tmp_path):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    path = export_review(result.result_path, tmp_path / "review")
    package = json.loads(path.read_bytes()); output = package["outputs"][0]
    form = HumanReview(review_id="mixed-form", output_id=output["output_id"], reviewer="Named form fixture", authorship="human",
        signed_at="2026-11-16T06:00:00+08:00", scope="Synthetic form contract", package_sha256=package["binding"],
        criteria_version=output["standard"]["version"], output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
        conclusions={"factual_accuracy": "unknown", "sufficiency": "failed", "readability": "passed", "critical_status": "unknown"},
        reasons={"factual_accuracy": "No actual human review in this fixture.", "sufficiency": "The synthetic form records a required-goal failure.", "critical_status": "Not measured by the fixture."})
    report = import_reviews(path, [form])
    assert report["outputs"][0]["content_status"] == "failed"
    assert report["outputs"][0]["dimensions"]["critical_status"] == "unknown"
    assert report["case_results"][0]["case_status"] == "failed"


def test_reexport_keeps_the_final_failure_and_its_original_history(tmp_path):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews, review_report

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    first_path = export_review(result.result_path, tmp_path / "first", final_reviewer="Final form fixture")
    package = json.loads(first_path.read_bytes()); output = package["outputs"][0]
    first = HumanReview(review_id="failed-original", output_id=output["output_id"], reviewer="Named form fixture", authorship="human",
        signed_at="2026-11-16T06:00:00+08:00", scope="Synthetic form contract", package_sha256=package["binding"],
        criteria_version=output["standard"]["version"], output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
        conclusions={name: "failed" for name in output["standard"]["dimensions"]}, reasons={name: "The original form records failure." for name in output["standard"]["dimensions"]})
    import_reviews(first_path, [first])
    final = first.model_copy(update={"review_id": "final-failure", "reviewer": "Final form fixture", "resolves": [first.review_id]})
    import_reviews(first_path, [final])
    second_path = export_review(result.result_path, tmp_path / "second", final_reviewer="Final form fixture")
    second = json.loads(second_path.read_bytes())
    passed = first.model_copy(update={"review_id": "later-pass", "package_sha256": second["binding"],
        "conclusions": {name: "passed" for name in first.conclusions}, "reasons": {}})
    report = import_reviews(second_path, [passed])
    assert report["outputs"][0]["content_status"] == "failed"
    assert report["case_results"][0]["case_status"] == "failed"
    assert {row["review_id"] for row in report["outputs"][0]["history"]} == {"failed-original", "final-failure", "later-pass"}
    assert review_report(first_path)["outputs"][0]["content_status"] == "failed"
    newer_standard = {**output["standard"], "version": "explicit-new-standard-v2"}
    newer_path = export_review(result.result_path, tmp_path / "new-standard", standards={"main_answer": newer_standard})
    newer = json.loads(newer_path.read_bytes())
    newer_form = passed.model_copy(update={"review_id": "new-standard-pass", "package_sha256": newer["binding"], "criteria_version": newer_standard["version"]})
    assert import_reviews(newer_path, [newer_form])["outputs"][0]["content_status"] == "passed"
    assert review_report(first_path)["outputs"][0]["content_status"] == "failed"


@pytest.mark.parametrize("duplicate", ["case-protocol", "execution-id"])
def test_export_rejects_unsupported_repeated_execution_shapes_before_writing(tmp_path, duplicate):
    import copy
    from tests.support.agent_acceptance import export_review

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question", "ordinary.attachment"]), root=tmp_path / "run", registry=_ordinary_registry()))
    saved = json.loads(result.result_path.read_bytes())
    if duplicate == "case-protocol":
        row = copy.deepcopy(saved["cases"][0]); row["execution_id"] += "-other"
        saved["cases"].append(row)
    else:
        saved["cases"][1]["execution_id"] = saved["cases"][0]["execution_id"]
    result.result_path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="Repeated"):
        export_review(result.result_path, tmp_path / "review")
    assert not (tmp_path / "review").exists()


def test_result_save_rejects_repeated_case_protocol_before_coverage_can_overwrite(tmp_path):
    import copy
    from tests.support.agent_acceptance import SharedControlError

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=_ordinary_registry()))
    original = result.result_path.read_bytes()
    result.cases.append(copy.deepcopy(result.cases[0]))
    with pytest.raises(SharedControlError, match="Repeated"):
        result.save("offline")
    assert result.result_path.read_bytes() == original



@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_question_explicit_denial_is_not_a_pass(tmp_path, protocol):
    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [Reply(text="Six times seven is not 42.")], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.tool_status == "passed"
    assert case.product_status == case.target_status == case.deterministic_status == case.case_status == "failed"



@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_attachment_explicit_denial_does_not_override_actual_source(tmp_path, protocol):
    from dataclasses import replace

    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [replace(reply, text="The threshold is not 800. [purchase.md:1]") if reply.text else reply for reply in replies], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.attachment"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.tool_status == "passed"
    assert case.facts["source"]["native_input_ids"]
    assert case.product_status == case.target_status == case.deterministic_status == case.case_status == "failed"


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_wrong_equation_is_not_saved_by_the_correct_number(tmp_path, protocol):
    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [Reply(text="6 + 7 = 42.")], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    assert result.cases[0].product_status == result.cases[0].deterministic_status == "failed"


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_a_number_mention_without_a_clear_assertion_remains_pending(tmp_path, protocol):
    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [Reply(text="There are 42 possible answers.")], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.product_status == "inconclusive"
    assert case.target_status == case.case_status == "pending_review"
    assert case.deterministic_status == "not_measured"
    assert case.expectation_status == "review_required"



@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_long_answer_accepts_equivalent_encoding_wording(tmp_path, protocol):
    from dataclasses import replace

    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [replace(reply, text=reply.text.replace("UTF-8 with BOM", "UTF-8 BOM")) if reply.text else reply for reply in replies], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.encoded-long"], protocols=[protocol]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.product_status == case.target_status == case.deterministic_status == "passed", case.facts
    assert case.facts["response_complete"] is True
    assert case.facts["answer"].startswith("UTF-8 BOM;")
    assert case.facts["source"]["native_input_ids"]
    assert case.facts["source"]["unchanged"] is True
    assert case.case_status == "pending_review"



@pytest.mark.parametrize("damage", ["missing-line", "wrong-encoding", "no-bom", "lf-only", "wrong-byte-prefix", "missing-citation", "short-body", "missing-ending"])
def test_long_result_keeps_required_structure_and_byte_facts(tmp_path, damage):
    from dataclasses import replace
    import re

    def transport(selected, replies, *, config=None):
        changed = []
        for reply in replies:
            text = reply.text
            if text:
                if damage == "missing-line":
                    text = "\n".join(line for line in text.splitlines() if not line.startswith("Line 077:"))
                elif damage == "wrong-encoding":
                    text = text.replace("UTF-8 with BOM", "UTF-16 with BOM")
                elif damage == "no-bom":
                    text = text.replace("with BOM", "without BOM")
                elif damage == "lf-only":
                    text = text.replace("CRLF line endings", "LF-only line endings")
                elif damage == "wrong-byte-prefix":
                    text = text.replace("CRLF line endings.", "CRLF line endings; prefix FF FE.")
                elif damage == "missing-citation":
                    text = text.replace("[encoding.txt:1-3]", "")
                elif damage == "short-body":
                    text = re.sub(r"(?m)^(Line \d+:).+$", r"\1 x", text)
                else:
                    text = text.replace("END-ORIGINAL-7319", "")
            changed.append(replace(reply, text=text))
        return OfflineSession(selected, changed, config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.encoded-long"]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.product_status == case.target_status == case.deterministic_status == "failed", case.facts
    assert case.facts["source"]["unchanged"] is True
    assert case.evidence["attachment"].read_bytes().startswith(b"\xef\xbb\xbf")


def test_long_result_allows_equivalent_numbering_body_and_valid_source_line(tmp_path):
    from dataclasses import replace
    import re

    def transport(selected, replies, *, config=None):
        changed = []
        for reply in replies:
            text = reply.text.replace("UTF-8 with BOM; CRLF line endings.", "Encoding: UTF‑8 BOM and CRLF newlines.")
            text = text.replace("[encoding.txt:1-3]", "[encoding.txt:2]")
            text = text.replace("preserve the original source and full response for review.", "Keep the entire source and response available for review.")
            text = re.sub(r"(?m)^Line (\d+):", lambda match: str(int(match[1])) + ")", text)
            changed.append(replace(reply, text=text))
        return OfflineSession(selected, changed, config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.encoded-long"]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.product_status == case.target_status == case.deterministic_status == "passed", case.facts
    assert case.facts["numbered_lines"] == 120
    assert case.facts["response_complete"] is True


@pytest.mark.parametrize(("case_id", "answer"), [
    ("ordinary.question", 'The result is 42. The earlier claim "six times seven is not 42" is wrong.'),
    ("ordinary.question", 'The result is 42. "6 + 7 = 42" is an example of an incorrect equation.'),
    ("ordinary.attachment", 'The threshold is 800. The old claim "threshold is not 800" is incorrect. [purchase.md:1]'),
])
def test_complex_quoted_claims_require_review_without_false_failure(tmp_path, case_id, answer):
    from dataclasses import replace

    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [replace(reply, text=answer) if reply.text else reply for reply in replies], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=[case_id]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.product_status == "inconclusive", case.facts
    assert case.facts["goal_program_contract_passed"] is True
    assert case.target_status == case.case_status == "pending_review"
    assert case.deterministic_status == "not_measured"


@pytest.mark.parametrize("shape", ["reviewable", "fixed-fixture", "program-unmeasured", "program-failed", "critical-unknown", "incomplete-evidence"])
def test_named_review_closes_only_semantic_uncertainty_with_complete_program_facts(tmp_path, shape):
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews, review_report

    def transport(selected, replies, *, config=None):
        return OfflineSession(selected, [Reply(text="Multiplying the two values gives the answer: forty-two.")], config=config)

    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]),
        root=tmp_path / "run", registry=_ordinary_registry(), session_factory=transport))
    case = result.cases[0]
    assert case.product_status == "inconclusive"
    original_case = json.loads(result.result_path.read_bytes())
    row = original_case["cases"][0]
    row["facts"]["goal_program_contract_passed"] = shape != "program-unmeasured"
    row["critical_contracts"] = {name: "passed" for name in ("K1", "K2", "K3", "K4", "K5")}
    if shape == "program-failed":
        row["tool_status"] = "failed"
    elif shape == "critical-unknown":
        row["critical_contracts"]["K1"] = "unknown"
    elif shape == "incomplete-evidence":
        row["evidence_status"] = "incomplete"
    source_path = case.evidence["semantic_review"]
    source = json.loads(source_path.read_bytes())
    for output in source["outputs"]:
        output["provenance"] = "fixed_fixture" if shape == "fixed-fixture" else "current_real"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    index_path = case.evidence["evidence_index"]
    index = json.loads(index_path.read_bytes())
    index["links"]["semantic_review"]["sha256"] = sha256(source_path.read_bytes()).hexdigest()
    index_path.write_text(json.dumps(index), encoding="utf-8")
    result.result_path.write_text(json.dumps(original_case), encoding="utf-8")
    original_bytes = result.result_path.read_bytes()
    package_path = export_review(result.result_path, tmp_path / "review")
    package = json.loads(package_path.read_bytes())
    output = package["outputs"][0]
    form = HumanReview(review_id="semantic-form-fixture", output_id=output["output_id"], reviewer="Named synthetic human-form author",
        authorship="human", signed_at="2026-11-16T06:00:00+08:00", scope="Complete originals; synthetic public import-contract test only",
        package_sha256=package["binding"], criteria_version=output["standard"]["version"], output_sha256=output["sha256"],
        reviewed_originals=output["required_originals"], conclusions={name: "passed" for name in output["standard"]["dimensions"]}, reasons={})
    report = import_reviews(package_path, [form])
    reviewed = report["case_results"][0]
    expected = "passed" if shape == "reviewable" else "failed" if shape == "program-failed" else "pending_review"
    assert reviewed["case_status"] == expected
    assert reviewed["deterministic_status"] == "not_measured"
    assert report["original_product_results"][0]["product_status"] == "inconclusive"
    assert result.result_path.read_bytes() == original_bytes
    assert review_report(package_path)["case_results"] == report["case_results"]
    if shape == "fixed-fixture":
        assert report["outputs"][0]["semantic_status"] == "not_measured"
