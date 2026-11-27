"""Complete historical groups through public import, independent review and report."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.agent_acceptance import AcceptanceCase, AcceptancePlan, CaseOutcome, CaseRegistry, Reply, run_plan


@pytest.fixture
def tmp_path():
    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/t21-history" / uuid4().hex
    root.mkdir(parents=True)
    return root


@pytest.fixture
def historical_group(tmp_path, request):
    from myrk.conversation import Conversation, Task
    registry = CaseRegistry()

    async def original(context):
        session = context.session([Reply(text="SYNTHETIC original result 42")])
        conversation = Conversation(context.root / "conversation.json")
        task = Task(goal="SYNTHETIC retained whole-group task", scope=None, provider_config=session.config)
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, task=task, model=model)
        exchange = session.save(context.root / "exchange.json", attempts=[a.model_dump(mode="json") for a in turn.request_attempts], run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        failed = context.case_id.endswith("failure")
        return CaseOutcome("failed" if failed else "passed", {"answer": "SYNTHETIC original result 42", "known_product_failure": failed and getattr(request, "param", None) != "grader_error"},
            {"conversation": conversation.path, "exchange": exchange}, target_status="failed" if failed else "passed", deterministic_status="failed" if failed else "passed")

    for name in ("history.synthetic-success", "history.synthetic-failure", "history.synthetic-unexecuted"):
        registry.register(AcceptanceCase(name, "failure-reporting", ("deepseek",), original,
            dependencies=("history.synthetic-failure",) if name.endswith("unexecuted") else ()))
    result = asyncio.run(run_plan(AcceptancePlan(cases=list(registry.cases)), root=tmp_path / "original", registry=registry))
    assert [row.product_status for row in result.cases] == ["passed", "failed", "not_executed"]
    return result, registry


def test_public_import_keeps_complete_original_group_without_new_execution(historical_group, tmp_path):
    from tests.support.agent_acceptance import HistoricalChange, import_historical_group, historical_reuse_report
    result, _ = historical_group
    before = sha256(result.result_path.read_bytes()).hexdigest()
    group = import_historical_group(result.result_path, destination=tmp_path / "history",
        change=HistoricalChange(author="synthetic-current-implementer", current={"source": "SYNTHETIC source v2"},
            reason="SYNTHETIC unrelated documentation change"))
    report = historical_reuse_report(group)
    assert report["original_denominator"] == 3
    assert [row["original"]["product_status"] for row in report["executions"]] == ["passed", "failed", "not_executed"]
    assert report["new_product_executions"] == 0
    assert report["layers"]["current_real"] == "not_measured"
    assert report["all_passed"] is False
    assert report["human_review"] == "not_performed"
    assert report["user_acceptance"] == "pending"
    assert sha256(result.result_path.read_bytes()).hexdigest() == before


def test_independent_applicability_reuses_facts_without_hiding_other_originals(historical_group, tmp_path):
    from tests.support.agent_acceptance import (HistoricalChange, HistoricalApplicabilityReview,
        import_historical_group, import_historical_applicability, historical_reuse_report)
    from tests.support.agent_acceptance.historical_reuse import FACETS
    result, _ = historical_group
    group = import_historical_group(result.result_path, destination=tmp_path / "reviewed-history",
        change=HistoricalChange(author="synthetic-implementer", current={name: "SYNTHETIC current identity" for name in FACETS},
            reason="SYNTHETIC documentation-only change"))
    frozen = json.loads(group.read_bytes())
    fact = {"execution_id": result.cases[0].execution_id, "fact_path": "/target_status",
        "matrix": {name: {"status": "unaffected", "reason": "SYNTHETIC independent review of this fact and actual original",
            "evidence_keys": ["source-result"]} for name in FACETS}}
    review = HistoricalApplicabilityReview(review_id="synthetic-independent-1", reviewer="synthetic-independent-ai",
        authorship="ai_applicability", group_binding=frozen["binding"], scope="SYNTHETIC target program fact only",
        reason="SYNTHETIC inspected complete group including failure and unexecuted case", facts=[fact])
    with pytest.raises(ValueError, match="independent"):
        import_historical_applicability(group, review.model_copy(update={"reviewer": "synthetic-implementer"}))
    report = import_historical_applicability(group, review)
    assert report["facts"][0]["current_status"] == "passed"
    assert report["facts"][0]["applicability"] == "unaffected"
    assert len(report["executions"]) == 3
    assert report["known_product_failures"][0]["execution_id"] == result.cases[1].execution_id
    assert report["all_passed"] is False
    assert report["layers"]["historical_reuse"] == "scoped_facts"
    assert report["layers"]["current_real"] == "not_measured"
    assert report["human_review"] == "not_performed"
    changed = json.loads(json.dumps(fact))
    changed["matrix"]["model"] = {"status": "affected", "reason": "SYNTHETIC changed model", "evidence_keys": ["source-result"]}
    second = HistoricalApplicabilityReview.model_validate({**review.model_dump(mode="json"), "review_id": "synthetic-model-drift", "facts": [changed]})
    report = import_historical_applicability(group, second)
    assert report["facts"][0]["applicability"] == "unknown"
    assert report["facts"][0]["current_status"] == "unknown"


@pytest.mark.parametrize("historical_group", ["grader_error"], indirect=True)
def test_readonly_regrade_keeps_old_judge_and_needs_independent_applicability(historical_group, tmp_path):
    from tests.support.agent_acceptance import (HistoricalChange, ReadOnlyHistoricalRegrade,
        HistoricalApplicabilityReview, import_historical_group, import_historical_applicability,
        import_historical_regrade, historical_reuse_report)
    from tests.support.agent_acceptance.historical_reuse import FACETS
    result, _ = historical_group
    group = import_historical_group(result.result_path, destination=tmp_path / "regrade",
        change=HistoricalChange(author="synthetic-implementer", current={name: "SYNTHETIC v2" for name in FACETS}, reason="SYNTHETIC grader correction"))
    original = group.read_bytes()
    grade = ReadOnlyHistoricalRegrade(regrade_id="synthetic-score-v2", author="synthetic-grader-fixer",
        group_binding=json.loads(original)["binding"], execution_id=result.cases[1].execution_id,
        fact_path="/deterministic_status", extraction_version="synthetic-reader-v2", criteria_version="synthetic-score-v2",
        scope="SYNTHETIC answer extraction only", reason="SYNTHETIC old reader mismatched a correct retained answer",
        conclusion="passed", correction_kind="grader_error",
        checks=[{"original_key": "source-result", "pointer": "/cases/1/facts/answer", "expected": "SYNTHETIC original result 42"}])
    report = import_historical_regrade(group, grade)
    assert report["regrades"][0]["old_judgment"] == "failed"
    assert report["regrades"][0]["new_judgment"] == "passed"
    assert report["facts"] == []
    fact = {"execution_id": result.cases[1].execution_id, "fact_path": "/deterministic_status", "regrade_id": grade.regrade_id,
        "matrix": {name: {"status": "unaffected", "reason": "SYNTHETIC independent review of corrected extraction", "evidence_keys": ["source-result"]} for name in FACETS}}
    review = HistoricalApplicabilityReview(review_id="synthetic-review-regrade", reviewer="synthetic-independent-ai",
        authorship="ai_applicability", group_binding=grade.group_binding, scope="SYNTHETIC one extraction fact",
        reason="SYNTHETIC inspected actual retained answer and old judge", facts=[fact])
    report = import_historical_applicability(group, review)
    assert report["facts"][0]["current_status"] == "passed"
    assert report["executions"][1]["original"]["deterministic_status"] == "failed"
    assert report["executions"][1]["original"]["product_status"] == "failed"
    assert report["new_product_executions"] == 0
    assert group.read_bytes() == original


def test_import_rejects_selected_successes_against_original_frozen_plan(historical_group, tmp_path):
    from tests.support.agent_acceptance import HistoricalChange, import_historical_group
    result, _ = historical_group
    selected = json.loads(result.result_path.read_bytes())
    selected["cases"] = selected["cases"][:1]
    selected["planned_denominator"] = 1
    attempt = result.result_path.with_name("attempted-cherry-picked.json")
    attempt.write_text(json.dumps(selected), encoding="utf-8")
    with pytest.raises(ValueError, match="complete group"):
        import_historical_group(attempt, destination=tmp_path / "invalid-selection",
            change=HistoricalChange(author="synthetic-author", current={}, reason="SYNTHETIC selected success attempt"))


def test_builtin_run_and_cli_report_keep_historical_groups_separate(historical_group, tmp_path):
    import subprocess
    import sys
    from tests.support.agent_acceptance import HistoricalChange, import_historical_group, builtin_registry
    result, _ = historical_group
    group = import_historical_group(result.result_path, destination=tmp_path / "entry-history",
        change=HistoricalChange(author="synthetic-author", current={}, reason="SYNTHETIC reporting only"))
    current = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"], historical_groups=[group]),
        root=tmp_path / "current", registry=builtin_registry()))
    report = json.loads(current.result_path.read_bytes())
    assert len(report["cases"]) == 1
    assert report["planned_denominator"] == 1
    assert report["historical_reuse"][0]["original_denominator"] == 3
    assert report["measurements"]["current_real"] == "not_measured"
    completed = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance", "history-report", str(group)],
        capture_output=True, text=True, encoding="utf-8", check=True)
    cli = json.loads(completed.stdout)
    assert cli["group_binding"] == report["historical_reuse"][0]["group_binding"]
    assert cli["new_product_executions"] == 0


def test_legacy_group_without_original_manifest_stays_unknown(historical_group, tmp_path):
    from tests.support.agent_acceptance import HistoricalChange, HistoricalApplicabilityReview, import_historical_group, import_historical_applicability
    from tests.support.agent_acceptance.historical_reuse import FACETS
    result, _ = historical_group
    legacy = json.loads(result.result_path.read_bytes())
    legacy["version"].pop("plan")
    source = result.result_path.with_name("legacy-without-group-manifest.json")
    source.write_text(json.dumps(legacy), encoding="utf-8")
    group = import_historical_group(source, destination=tmp_path / "legacy",
        change=HistoricalChange(author="synthetic-implementer", current={}, reason="SYNTHETIC legacy import"))
    review = HistoricalApplicabilityReview(review_id="legacy-independent", reviewer="synthetic-independent-ai",
        authorship="ai_applicability", group_binding=json.loads(group.read_bytes())["binding"], scope="SYNTHETIC original program fact", reason="SYNTHETIC no complete-group proof",
        facts=[{"execution_id":result.cases[0].execution_id,"fact_path":"/target_status",
            "matrix":{name:{"status":"unaffected","reason":"SYNTHETIC irrelevant changed component","evidence_keys":["source-result"]} for name in FACETS}}])
    report = import_historical_applicability(group, review)
    assert report["group_completeness"] == "unknown"
    assert report["facts"][0]["current_status"] == "unknown"


def test_current_material_use_proof_is_separate_from_legacy_as_of(historical_group, tmp_path):
    import shutil
    from tests.support.agent_acceptance import (HistoricalChange, HistoricalMaterialUseReview, import_historical_group,
        import_historical_material_use, historical_reuse_report, freeze_quality, quality_plan, FrozenParentBudget, record_quality_adjustment)
    from tests.support.agent_acceptance.frozen import SOURCE
    from tests.support.agent_acceptance.quality import quality_usage_authority_path, read_quality
    from tests.support.agent_acceptance.output_review import ReviewOriginal, digest
    assert quality_usage_authority_path().is_file(), "Existing canonical authority is required; this test never initializes one."
    work = SOURCE / ".scratch/retro-agent-live-acceptance-20261116/implementation/t21/tmp01" / ("quality-" + uuid4().hex)
    work.mkdir(parents=True)
    try:
        tasks = json.loads((SOURCE / "tests/acceptance/materials/quality/synthetic.json").read_bytes())
        for task in tasks:
            task["objective"] += " [SYNTHETIC history proof " + work.name + "]"
            for kind in ("input", "gold"):
                target = work / (task["task_id"] + "." + kind + ".txt")
                target.write_text(task.pop(kind), encoding="utf-8")
                task[kind + "_path"] = target
        materials = freeze_quality(tasks, destination=work / "freeze.json", provenance="synthetic", source_version="SYNTHETIC historical source",
            environment={"platform":"SYNTHETIC offline"}, models={"deepseek":{"model":"synthetic"},"google":{"model":"synthetic"}},
            criteria_version="synthetic-history-v1", qualification_families=[])
        frozen = read_quality(materials)
        registry = CaseRegistry()
        async def not_executed(_context):
            return CaseOutcome("not_executed", {"reason":"SYNTHETIC no product execution for material authority proof"})
        registry.register(AcceptanceCase("history.synthetic-quality", "request-input", ("deepseek","google"), not_executed,
            bindings=(materials, *(SOURCE / name for name in frozen.files)), frozen_material_input=True, output_standards={}))
        plan = quality_plan(materials, registry=registry, adapters={task.task_id:"history.synthetic-quality" for task in frozen.tasks},
            parent_budget=FrozenParentBudget(parent_id="synthetic-history-parent",accounting_root=tmp_path / "accounting",request_cap=2520,
                phase_microusd={"screening":0,"quality":39076920,"judge":0},reserve_microusd=0,profile="synthetic"),mode="offline")
        result = asyncio.run(run_plan(plan, root=tmp_path / "unexecuted-quality", registry=registry))
        legacy = frozen.model_dump(mode="json")
        legacy["schema_version"] = 3
        legacy["usage_authority"] = None
        legacy["binding"] = digest({key:value for key,value in legacy.items() if key!="binding"})
        legacy_path = work / "controlled-legacy-manifest.json"
        legacy_path.write_text(json.dumps(legacy),encoding="utf-8")
        sources = {"quality-materials":ReviewOriginal(path=legacy_path,sha256=sha256(legacy_path.read_bytes()).hexdigest())}
        sources.update({"material-" + str(index):ReviewOriginal(path=SOURCE / name,sha256=fingerprint) for index,(name,fingerprint) in enumerate(frozen.files.items())})
        group = import_historical_group(result.result_path,destination=tmp_path / "material-history",state_originals=sources,
            change=HistoricalChange(author="synthetic-implementer",current={},reason="SYNTHETIC read-only old material proof"))
        original = group.read_bytes()
        proof = HistoricalMaterialUseReview(review_id="synthetic-current-use",reviewer="synthetic-independent-ai",authorship="ai_applicability",
            group_binding=json.loads(original)["binding"],materials_original_key="quality-materials",
            authority_sha256=sha256(quality_usage_authority_path().read_bytes()).hexdigest(),reason="SYNTHETIC independently bound original task inputs/objectives to persistent authority")
        report = import_historical_material_use(group, proof)
        assert report["material_use"][0]["current"]["status"] == "verified"
        assert report["material_use"][0]["current"]["records"] == []
        assert report["material_use"][0]["historical_as_of"] == "unknown"
        decision = work / "synthetic-adjustment.txt"
        decision.write_text("SYNTHETIC test observed use of one formal task for grader adjustment",encoding="utf-8")
        record_quality_adjustment(materials,task_ids=["QG1.formal.1"],purpose="judge",evidence=decision)
        later = historical_reuse_report(group)
        assert later["material_use"][0]["current"]["records"][0]["task_ids"] == ["QG1.formal.1"]
        assert later["material_use"][0]["historical_as_of"] == "unknown"
        assert later["actual_baseline"] == "not_established"
        assert later["new_product_executions"] == 0
        assert group.read_bytes() == original
    finally:
        assert work.resolve().is_relative_to(SOURCE / ".scratch/retro-agent-live-acceptance-20261116/implementation/t21/tmp01")
        shutil.rmtree(work)


@pytest.mark.parametrize("facet", ["source", "model", "prompt", "protocol", "permissions", "scope", "save", "summary", "environment", "material", "date", "criteria"])
def test_each_affected_facet_and_missing_original_prevents_inherited_pass(historical_group, tmp_path, facet):
    from tests.support.agent_acceptance import HistoricalChange, HistoricalApplicabilityReview, import_historical_group, import_historical_applicability, historical_reuse_report
    from tests.support.agent_acceptance.historical_reuse import FACETS
    result, _ = historical_group
    group = import_historical_group(result.result_path,destination=tmp_path / "drift",
        change=HistoricalChange(author="synthetic-implementer",current={},reason="SYNTHETIC scoped drift"))
    matrix={name:{"status":"unaffected","reason":"SYNTHETIC independent unaffected fact evidence","evidence_keys":["source-result"]} for name in FACETS}
    matrix[facet]={"status":"affected","reason":"SYNTHETIC this fact depends on the changed facet","evidence_keys":["source-result"]}
    review=HistoricalApplicabilityReview(review_id="synthetic-drift",reviewer="synthetic-independent-ai",authorship="ai_applicability",
        group_binding=json.loads(group.read_bytes())["binding"],scope="SYNTHETIC one original fact",reason="SYNTHETIC changed dependency",
        facts=[{"execution_id":result.cases[0].execution_id,"fact_path":"/target_status","matrix":matrix}])
    report=import_historical_applicability(group,review)
    assert report["facts"][0]["current_status"] == "unknown"
    original=json.loads(group.read_bytes())["originals"]["source-result"]
    (group.parent / original["path"]).unlink()
    missing=historical_reuse_report(group)
    assert "source-result" in missing["missing_originals"]
    assert missing["facts"][0]["current_status"] == "unknown"


def test_legitimate_legacy_group_can_use_independent_whole_manifest(historical_group,tmp_path):
    from tests.support.agent_acceptance import HistoricalChange, HistoricalApplicabilityReview, import_historical_group, import_historical_applicability
    from tests.support.agent_acceptance.historical_reuse import FACETS
    from tests.support.agent_acceptance.output_review import ReviewOriginal
    result,_=historical_group
    original=json.loads(result.result_path.read_bytes())
    manifest=tmp_path / "original-whole-manifest.json"
    manifest.write_text(json.dumps({"run_id":result.run_id,"execution_ids":[row.execution_id for row in result.cases]}),encoding="utf-8")
    original["version"].pop("plan")
    legacy=result.result_path.with_name("legacy-valid-manifest.json")
    legacy.write_text(json.dumps(original),encoding="utf-8")
    group=import_historical_group(legacy,destination=tmp_path / "legacy-proven",
        state_originals={"original-whole-manifest":ReviewOriginal(path=manifest,sha256=sha256(manifest.read_bytes()).hexdigest())},
        change=HistoricalChange(author="synthetic-implementer",current={},reason="SYNTHETIC unrelated changed component"))
    review=HistoricalApplicabilityReview(review_id="legacy-complete",reviewer="synthetic-independent-ai",authorship="ai_applicability",
        group_binding=json.loads(group.read_bytes())["binding"],scope="SYNTHETIC retained program fact",reason="SYNTHETIC verified the actual separate full-group manifest",
        complete_group_original_key="original-whole-manifest",
        facts=[{"execution_id":result.cases[0].execution_id,"fact_path":"/target_status",
            "matrix":{name:{"status":"unaffected","reason":"SYNTHETIC this original fact is unrelated to the changed component","evidence_keys":["source-result"]} for name in FACETS}}])
    report=import_historical_applicability(group,review)
    assert report["group_completeness"] == "verified"
    assert report["facts"][0]["current_status"] == "passed"
    assert report["executions"][0]["applicability"] == "scoped_facts"
    assert report["all_passed"] is False


@pytest.mark.parametrize("correction",["grader_error","criteria_revision"])
def test_known_product_failure_cannot_be_washed_by_score_version(historical_group,tmp_path,correction):
    from tests.support.agent_acceptance import HistoricalChange, ReadOnlyHistoricalRegrade, import_historical_group, import_historical_regrade
    result,_=historical_group
    group=import_historical_group(result.result_path,destination=tmp_path / "failure",
        change=HistoricalChange(author="synthetic-implementer",current={},reason="SYNTHETIC new score version"))
    grade=ReadOnlyHistoricalRegrade(regrade_id="synthetic-failure-regrade",author="synthetic-grader",group_binding=json.loads(group.read_bytes())["binding"],
        execution_id=result.cases[1].execution_id,fact_path="/deterministic_status",extraction_version="synthetic-v2",criteria_version="synthetic-v2",
        scope="SYNTHETIC score-only revision",reason="SYNTHETIC new version does not resolve the known product failure",conclusion="passed",correction_kind=correction,
        checks=[{"original_key":"source-result","pointer":"/cases/1/facts/answer","expected":"SYNTHETIC original result 42"}])
    with pytest.raises(ValueError,match="known product failure"):
        import_historical_regrade(group,grade)


def test_copied_execution_and_cross_batch_successes_are_rejected(historical_group,tmp_path):
    from tests.support.agent_acceptance import HistoricalChange,import_historical_group
    result,_=historical_group
    raw=json.loads(result.result_path.read_bytes())
    for name,edit in (("duplicate",lambda body:body["cases"][1].update(execution_id=body["cases"][0]["execution_id"])),
                      ("cross-batch",lambda body:body["cases"][0]["facts"].update(source_run_id="another-original-batch"))):
        attempted=json.loads(json.dumps(raw))
        edit(attempted)
        source=result.result_path.with_name(name+".json")
        source.write_text(json.dumps(attempted),encoding="utf-8")
        with pytest.raises(ValueError,match="complete group"):
            import_historical_group(source,destination=tmp_path / name,change=HistoricalChange(author="synthetic-author",current={},reason="SYNTHETIC invalid import"))


def test_fixture_self_report_never_becomes_actual_historical_evidence(historical_group,tmp_path):
    from tests.support.agent_acceptance import HistoricalChange,import_historical_group,historical_reuse_report
    result,_=historical_group
    raw=json.loads(result.result_path.read_bytes())
    raw["mode"]="live"
    raw["cases"][0]["facts"]["real_http_attempts"]=1
    source=result.result_path.with_name("synthetic-false-real-label.json")
    source.write_text(json.dumps(raw),encoding="utf-8")
    group=import_historical_group(source,destination=tmp_path / "fixture-guard",
        change=HistoricalChange(author="synthetic-author",current={},reason="SYNTHETIC provenance negative control"))
    report=historical_reuse_report(group)
    assert report["historical_source_provenance"] == "fixed_fixture"
    assert report["layers"]["historical_real"] == "not_measured"
    assert report["human_review"] == "not_performed"
    assert report["actual_baseline"] == "not_established"


def test_independent_source_claim_cannot_override_native_fixture_bytes(historical_group,tmp_path):
    from tests.support.agent_acceptance import HistoricalChange,HistoricalApplicabilityReview,import_historical_group,import_historical_applicability
    from tests.support.agent_acceptance.historical_reuse import FACETS
    result,_=historical_group
    group=import_historical_group(result.result_path,destination=tmp_path / "source-proof",
        change=HistoricalChange(author="synthetic-author",current={},reason="SYNTHETIC source origin negative control"))
    review=HistoricalApplicabilityReview(review_id="false-real-origin",reviewer="synthetic-independent-ai",authorship="ai_applicability",
        group_binding=json.loads(group.read_bytes())["binding"],scope="SYNTHETIC actual source claim",reason="SYNTHETIC false source claim must remain rejected",
        historical_source="historical_real",source_evidence_keys=["source-result"],
        facts=[{"execution_id":result.cases[0].execution_id,"fact_path":"/target_status",
            "matrix":{name:{"status":"unaffected","reason":"SYNTHETIC original fact","evidence_keys":["source-result"]} for name in FACETS}}])
    with pytest.raises(ValueError,match="fixture"):
        import_historical_applicability(group,review)


def test_unsigned_original_review_preserves_frozen_standard_without_claiming_human_review(historical_group,tmp_path):
    from tests.support.agent_acceptance import HistoricalChange,import_historical_group,historical_reuse_report,export_review
    result,_=historical_group
    standard={"version":"synthetic-older-frozen-standard","dimensions":{"sufficiency":{"description":"SYNTHETIC original complete answer","applicable":True}}}
    package=export_review(result.result_path,tmp_path / "old-review",standards={"main_answer":standard})
    original=json.loads(package.read_bytes())
    group=import_historical_group(result.result_path,destination=tmp_path / "review-preserved",review_package=package,
        change=HistoricalChange(author="synthetic-author",current={},reason="SYNTHETIC old output review preservation"))
    frozen=json.loads(group.read_bytes())
    assert frozen["outputs"] == original["outputs"]
    assert frozen["original_review_package"] == original
    assert frozen["original_human_report"]["outputs"][0]["history"] == []
    report=historical_reuse_report(group)
    assert report["human_review"] == "not_performed"
    assert report["user_acceptance"] == "pending"
    assert report["all_passed"] is False


def test_current_report_rejects_copied_historical_group_before_product_execution(historical_group,tmp_path):
    from tests.support.agent_acceptance import HistoricalChange,import_historical_group,SharedControlError
    result,registry=historical_group
    groups=[import_historical_group(result.result_path,destination=tmp_path / name,
        change=HistoricalChange(author="synthetic-author",current={},reason="SYNTHETIC copied-group negative control")) for name in ("copy-a","copy-b")]
    current=tmp_path / "must-not-execute"
    with pytest.raises(SharedControlError,match="duplicate original execution"):
        asyncio.run(run_plan(AcceptancePlan(cases=["history.synthetic-success"],historical_groups=groups),root=current,registry=registry))
    assert not current.exists()


def test_structured_original_fact_keeps_its_actual_value_without_semantic_promotion(historical_group,tmp_path):
    from tests.support.agent_acceptance import HistoricalChange,HistoricalApplicabilityReview,import_historical_group,import_historical_applicability
    from tests.support.agent_acceptance.historical_reuse import FACETS
    result,_=historical_group
    group=import_historical_group(result.result_path,destination=tmp_path / "structured-fact",
        change=HistoricalChange(author="synthetic-author",current={},reason="SYNTHETIC unrelated component change"))
    review=HistoricalApplicabilityReview(review_id="structured-fact-review",reviewer="synthetic-independent-ai",authorship="ai_applicability",
        group_binding=json.loads(group.read_bytes())["binding"],scope="SYNTHETIC original structured program facts",reason="SYNTHETIC compared exact retained JSON facts",
        facts=[{"execution_id":result.cases[0].execution_id,"fact_path":"/facts",
            "matrix":{name:{"status":"unaffected","reason":"SYNTHETIC this original structured fact is unchanged","evidence_keys":["source-result"]} for name in FACETS}}])
    report=import_historical_applicability(group,review)
    fact=report["facts"][0]
    assert fact["current_status"] == "verified"
    assert fact["current_value"]["answer"] == "SYNTHETIC original result 42"
    assert fact["current_value"]["known_product_failure"] is False
    assert report["all_passed"] is False
    assert report["layers"]["current_real"] == "not_measured"
