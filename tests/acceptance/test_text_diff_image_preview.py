"""Real public Desktop Main preview coverage, selected through the common catalogue."""

from tests.support.agent_acceptance import builtin_registry


def test_preview_is_selectable_as_one_local_primary_output_group() -> None:
    case = builtin_registry().cases["preview.text-diff-image"]
    assert case.owner_ticket == "18"
    assert case.group == "text-diff-image-preview"
    assert case.protocols == ("deepseek",)
    assert case.observation_layer == "electron-main-service-sdk"
    assert case.formal_live is False


def test_public_preview_keeps_actual_content_identity_snapshots_and_reopen(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    result = asyncio.run(run_plan(AcceptancePlan(cases=["preview.text-diff-image"]), root=acceptance_public_root, registry=builtin_registry()))
    outcome = result.cases[0]
    assert outcome.tool_status == "passed", outcome.facts
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["covered"] == ["L07", "L08", "L09", "L10", "L11", "L12", "L13"]
    assert outcome.facts["setup_http_attempts"] == 10
    assert outcome.facts["preview_http_attempts"] == 0
    assert outcome.facts["processes_complete"] is True
    work = next(row for row in outcome.facts["states"] if row["phase"] == "work")
    assert work["original_acl_restored"] is True
    assert any(code in work["permission_cause"]["message"] for code in ("EACCES", "EPERM"))
    assert work["acl_restore"]["owner_group_access_entries_and_protection_equal"] is True
    assert "EBUSY" in work["unreadable_cause"]["message"]
    assert work["chat_reading_isolated"] is True
    assert work["image_reading"]["focus"] == "放大图片 picture.png"
    assert outcome.critical_contracts["K2"] == "not_applicable"
    assert outcome.critical_contracts["K3"] == "passed"
    assert outcome.critical_contracts["K5"] == "passed"
    assert outcome.semantic_status == "not_measured"
