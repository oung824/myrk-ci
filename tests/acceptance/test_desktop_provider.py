"""Provider and formal desktop behavior through the unified acceptance entry."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, run_plan


def report_root():
    return Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/desktop-provider" / uuid4().hex


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_provider_settings_catalog_and_native_stream_are_observable(tmp_path, protocol):
    from tests.support.agent_acceptance.desktop_provider import register

    registry = CaseRegistry()
    register(registry)
    result = asyncio.run(run_plan(AcceptancePlan(cases=["provider.settings"], protocols=[protocol]),
                                 root=report_root(), registry=registry))
    outcome = result.cases[0]
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.semantic_status == "not_measured"
    assert outcome.facts["real_http_attempts"] == outcome.facts["normal_profile_accesses"] == 0
    assert outcome.facts["catalog_mock_attempts"] == 1
    exchange = json.loads(outcome.evidence["exchange"].read_text(encoding="utf-8"))
    assert exchange["product_attempts"][0]["actor"] == "main"
    assert exchange["exchanges"][0]["received_utf8"]


def test_validated_live_gate_does_not_charge_for_connection_opening(prepared_root):
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, prepare_live
    from tests.support.agent_acceptance.desktop_provider import register

    registry = CaseRegistry()
    register(registry)
    prepared = prepare_live(AcceptancePlan(cases=["provider.settings"], mode="prepare-live"), root=prepared_root, registry=registry)
    gate = LiveGate(prepared_root, registry, LiveAuthorization(authorization_id="qualification", binding=prepared.binding,
        total_limit=1, case_limits={"provider.settings": 1}, transport="mock",
        total_microusd=122880, case_microusd={'provider.settings': 122880}))
    gate.validate(case_id="provider.settings", protocol="deepseek")
    assert json.loads((prepared_root / "ledger.json").read_text())["attempts"] == []


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
@pytest.mark.acceptance_desktop
def test_formal_live_handoff_uses_terminal_mock_and_keeps_originals(tmp_path, prepared_root, protocol):
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, prepare_live
    from tests.support.agent_acceptance.desktop_provider import register

    registry = CaseRegistry()
    register(registry)
    prepared = prepare_live(AcceptancePlan(cases=["desktop.handoff"], protocols=[protocol], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-formal-qualification", binding=prepared.binding,
        total_limit=1, case_limits={"desktop.handoff": 1}, transport="mock",
        total_microusd=122880, case_microusd={'desktop.handoff': 122880})
    result = asyncio.run(run_plan(AcceptancePlan(cases=["desktop.handoff"], protocols=[protocol], mode="live"), root=report_root(),
        registry=registry, live_gate=LiveGate(prepared_root, registry, grant)))
    outcome = result.cases[0]
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["product_attempts"] == outcome.facts["mock_http_attempts"] == 1
    assert outcome.facts["real_http_attempts"] == outcome.facts["normal_profile_accesses"] == 0
    assert outcome.facts["synthetic_profiles"] is True
    transport = json.loads(outcome.evidence["transport"].read_text(encoding="utf-8"))
    assert transport["transport_kind"] == "MockTransport"
    native = json.loads(outcome.evidence["exchange"].read_bytes())["exchanges"][0]["billing_eligibility"]
    gap = "unsupported_paid_route" if protocol == "openai-chat" else "input_billing_bound"
    assert native["sending_enabled"] is False and gap in native["unresolved"]
    assert all(path.is_file() for path in outcome.evidence.values())
    assert json.loads(outcome.evidence["processes"].read_text())["complete"] is True


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
@pytest.mark.parametrize("case_id", ["provider.retry", "provider.interrupted", "provider.missing-usage"])
def test_provider_failures_keep_native_errors_fragments_and_unknown_usage(protocol, case_id):
    from tests.support.agent_acceptance.desktop_provider import register

    registry = CaseRegistry()
    register(registry)
    result = asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=[protocol]), root=report_root(), registry=registry))
    assert result.cases[0].product_status == "passed", result.cases[0].facts


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_receiver_switch_preserves_original_task_and_does_not_send_on_preview_or_decline(protocol):
    from tests.support.agent_acceptance.desktop_provider import register

    registry = CaseRegistry()
    register(registry)
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["provider.recipient-switch"], protocols=[protocol]),
        root=report_root(), registry=registry)).cases[0]
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["original_task_id"] == outcome.facts["resumed_task_id"]
    assert outcome.facts["preview_sends"] == outcome.facts["decline_sends"] == 0
    assert outcome.facts["product_attempts"] == 2


@pytest.mark.acceptance_desktop
def test_desktop_lifecycle_and_credentials_preserve_actual_process_evidence():
    from tests.support.agent_acceptance.desktop_provider import register

    registry = CaseRegistry()
    register(registry)
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["desktop.lifecycle"]), root=report_root(), registry=registry)).cases[0]
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["normal_profile_accesses"] == outcome.facts["real_http_attempts"] == 0
    assert outcome.facts["close_continued"] and outcome.facts["reopened_without_duplicate"]
    assert outcome.facts["normal_exit"] and outcome.facts["crash_recovered"] and outcome.facts["credential_forgotten"]
    assert all(path.is_file() for path in outcome.evidence.values())


@pytest.mark.parametrize("failure", ["missing", "binding", "exhausted"])
def test_formal_live_rejects_before_profile_and_process_creation(prepared_root, tmp_path, failure):
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, SharedControlError, prepare_live
    from tests.support.agent_acceptance.desktop_provider import register

    registry = CaseRegistry()
    register(registry)
    prepared = prepare_live(AcceptancePlan(cases=["provider.settings"], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-refusal", binding=prepared.binding,
        total_limit=1, case_limits={"provider.settings": 1}, transport="mock",
        total_microusd=122880, case_microusd={'provider.settings': 122880})
    gate = LiveGate(prepared_root, registry, None if failure == "missing" else grant)
    if failure == "binding":
        (prepared_root / "myrk/provider.py").write_text("changed", encoding="utf-8")
    elif failure == "exhausted":
        gate.reserve(case_id="provider.settings", protocol="deepseek", actor="main", purpose="work")
    root = tmp_path / "refused"
    with pytest.raises(SharedControlError):
        asyncio.run(run_plan(AcceptancePlan(cases=["provider.settings"], mode="live"), root=root, registry=registry, live_gate=gate))
    assert not root.exists()


def test_live_cannot_bypass_the_formal_service_with_an_offline_factory(prepared_root, tmp_path):
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, SharedControlError, prepare_live
    from tests.support.agent_acceptance.desktop_provider import register
    from tests.support.agent_acceptance.replay import OfflineSession

    registry = CaseRegistry()
    register(registry)
    prepared = prepare_live(AcceptancePlan(cases=["provider.settings"], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="factory-bypass-refusal", binding=prepared.binding,
        total_limit=1, case_limits={"provider.settings": 1}, transport="mock",
        total_microusd=122880, case_microusd={'provider.settings': 122880})
    with pytest.raises(SharedControlError, match="formal service"):
        asyncio.run(run_plan(AcceptancePlan(cases=["provider.settings"], mode="live"), root=tmp_path / "refused",
            registry=registry, live_gate=LiveGate(prepared_root, registry, grant), session_factory=OfflineSession))
    assert not (tmp_path / "refused").exists()


@pytest.mark.acceptance_desktop
def test_frozen_preparation_executes_in_new_process_after_original_temporary_directory_is_removed(prepared_root, tmp_path):
    from tests.support.agent_acceptance import LiveAuthorization, prepare_live
    from tests.support.agent_acceptance.desktop_provider import register

    registry = CaseRegistry()
    register(registry)
    with TemporaryDirectory(dir=tmp_path) as original:
        original_path = Path(original)
        (original_path / "selected-material.txt").write_text("synthetic initial preparation workspace", encoding="utf-8")
        prepared = prepare_live(AcceptancePlan(cases=["desktop.handoff"], mode="prepare-live"), root=prepared_root, registry=registry)
    assert not original_path.exists()
    grant = LiveAuthorization(authorization_id="fresh-process-qualification", binding=prepared.binding, total_limit=1,
        case_limits={"desktop.handoff": 1}, transport="mock",
        total_microusd=122880, case_microusd={'desktop.handoff': 122880})
    request = prepared_root / "request.json"
    request.write_text(json.dumps({"run_root": str(prepared_root), "grant": grant.model_dump(mode="json"),
        "case_id": "desktop.handoff", "protocol": "deepseek"}), encoding="utf-8")
    output = report_root()
    process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "execute",
        str(request), "--output", str(output)], cwd=prepared_root,
        env={**os.environ, "PYTHONPATH": str(prepared_root), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
    output.mkdir(parents=True, exist_ok=True)
    (output / "fresh-process.stdout").write_bytes(process.stdout)
    (output / "fresh-process.stderr").write_bytes(process.stderr)
    assert process.returncode == 0, process.stderr.decode("utf-8")
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["cases"][0]["product_status"] == "passed"
    service_receipts = list(output.glob("desktop.handoff/deepseek/receipts/process-*.json"))
    assert service_receipts
    assert all(json.loads(path.read_text(encoding="utf-8"))["source"].startswith(str(prepared_root)) for path in service_receipts)
    assert len(json.loads((prepared_root / "ledger.json").read_text(encoding="utf-8"))["attempts"]) == 1
