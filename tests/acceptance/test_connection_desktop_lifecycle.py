"""Connection/settings contracts through the existing public acceptance entry."""

from tests.support.agent_acceptance import builtin_registry


def test_settings_and_handoff_have_frozen_ordinary_ids_and_caps() -> None:
    registry = builtin_registry()
    assert registry.cases["desktop.connection-basics"].observation_layer == "electron-main-service-sdk"
    settings = registry.cases["formal.provider-settings"]
    assert settings.owner_ticket == "16" and settings.formal_live and settings.frozen_material_input
    assert settings.screening["deepseek"]["scenario_id"] == "D16"
    assert settings.screening["google"]["scenario_id"] == "G04"
    assert {row["request_cap"] for row in settings.screening.values()} == {12}
    handoff = registry.cases["desktop.handoff"]
    assert handoff.screening["deepseek"]["scenario_id"] == "D17"
    assert handoff.screening["deepseek"]["request_cap"] == 6
    assert handoff.frozen_material_input

def test_public_settings_roundtrip_reports_effective_off_separately(acceptance_public_root) -> None:
    import asyncio
    import json
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["formal.provider-settings"]), root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == "passed", outcome.facts
    contract = outcome.facts["connection_contract"]
    assert contract["off_before_new"] is True
    assert contract["native_tool_roundtrip"] is True
    assert outcome.product_status == ("passed" if contract["effective_native_setting"] else "failed")
    assert outcome.semantic_status == "not_measured" and outcome.user_acceptance == "pending"
    review = json.loads(outcome.evidence["connection_review"].read_bytes())
    assert review["execution_id"] == outcome.execution_id
    assert review["human_review"] == "not_performed"

def test_public_connection_basics_keep_real_ready_errors_credentials_and_close(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["desktop.connection-basics"]), root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == "passed", outcome.facts
    startup = outcome.facts["startup_status_observation"]
    assert outcome.facts["original_startup_error"] is True, startup
    assert outcome.facts["no_ready_success"] is True, startup
    assert outcome.product_status == "passed", outcome.facts
    assert outcome.facts["service_ready"] is True
    assert outcome.facts["normal_startup_ready_visible"] is True
    assert outcome.facts["startup_failure_visible"] is True
    assert "startup-rendered-text.txt" in " ".join(outcome.evidence)
    assert outcome.facts["password_not_refilled"] is True
    assert outcome.facts["new_input_cleared"] is True
    assert outcome.facts["credential_forgotten"] is True
    assert outcome.facts["wrong_connection_not_success"] is True
    assert outcome.facts["recipient_preview_and_decisions_zero_send"] is True
    assert outcome.facts["recipient_consent"]["source"]["protocol"] == "deepseek"
    assert outcome.facts["recipient_consent"]["target"]["protocol"] == "openai-chat"
    assert outcome.facts["close_continued"] and outcome.facts["reopened_without_duplicate"]
    assert outcome.facts["normal_exit"] and outcome.facts["processes_complete"]
    assert outcome.facts["normal_profile_accesses"] == outcome.facts["real_http_attempts"] == 0
    assert outcome.facts["crash_recovery"] == "requires_bound_reused_original"
    assert outcome.critical_contracts["K3"] == "passed"
    assert outcome.facts["critical_applicability"]["K5"]["B01"] == "passed"
    assert outcome.facts["critical_applicability"]["K5"]["B02"] == "passed"
    assert outcome.facts["critical_applicability"]["K5"]["C09_startup_failure_quit"] == "passed"

def test_frozen_settings_and_handoff_keep_original_parent_and_each_ordinary_once(acceptance_public_root, prepared_root) -> None:
    import asyncio
    from hashlib import sha256
    import json
    from pathlib import Path
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import AcceptancePlan, FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate, prepare_live, run_plan
    materials = Path(__file__).parent / "materials/connection-desktop"
    entries = [("D16", "formal.provider-settings", "deepseek", 12, "settings.txt"),
        ("G04", "formal.provider-settings", "google", 12, "settings.txt"),
        ("D17", "desktop.handoff", "deepseek", 6, "handoff.txt")]
    rows = [FrozenExecution(execution_id=f"{scenario.lower()}:{protocol}:1",case_id=case,protocol=protocol,
        scenario_id=scenario,material_family_id="connection-"+scenario.lower(),material_variant="baseline",
        material_path=materials/material,material_sha256=sha256((materials/material).read_bytes()).hexdigest(),
        startpoint=f"connection-{scenario.lower()}-{protocol}-isolated-1",order=number,request_cap=cap,cost_cap_microusd=cap*122880)
        for number,(scenario,case,protocol,cap,material) in enumerate(entries,1)]
    parent = FrozenParentBudget(parent_id="synthetic-connection-"+prepared_root.name,accounting_root=prepared_root,
        request_cap=30,phase_microusd={"screening":30*122880,"quality":0,"judge":0},reserve_microusd=0,profile="synthetic")
    plan = AcceptancePlan(executions=rows,parent_budget=parent)
    registry = builtin_registry()
    activation = project_root(Path(__file__)) / ".scratch/acceptance-parent-budgets" / (sha256(parent.parent_id.encode()).hexdigest()+".json")
    assert not activation.exists() and not prepared_root.exists()
    try:
        bundle = prepare_live(plan.model_copy(update={"mode":"prepare-live"}),root=prepared_root,registry=registry)
        grant = LiveAuthorization(authorization_id="synthetic-frozen-connection",binding=bundle.binding,transport="mock",
            parent_budget_id=parent.parent_id,total_limit=30,case_limits={row.execution_id:row.request_cap for row in rows},
            total_microusd=30*122880,case_microusd={row.execution_id:row.cost_cap_microusd for row in rows})
        result = asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
        assert [case.execution_id for case in result.cases] == [row.execution_id for row in rows]
        for row,case in zip(rows,result.cases):
            assert case.tool_status == "passed", case.facts
            assert case.facts["real_http_attempts"] == case.facts["normal_profile_accesses"] == 0
            assert case.screening["scenario_id"] == row.scenario_id and case.screening["request_cap"] == row.request_cap
            assert case.semantic_status == "not_measured" and case.user_acceptance == "pending"
            assert_frozen_execution_budget(case, row)
            if row.scenario_id != "D17":
                contract = case.facts["connection_contract"]
                assert contract["native_tool_roundtrip"] and contract["complete_sdk_inputs"] and contract["preview_send_count_match"]
                assert contract["tools_stable"] is True
                assert case.product_status == ("passed" if contract["effective_native_setting"] else "failed")
                if row.protocol == "deepseek":
                    assert contract["off_before_new"] is True
                else:
                    assert contract["effective_native_setting"] is True
                    assert contract["models"] == ["gemini-3.5-flash", "gemini-3.5-flash"]
            else:
                assert case.product_status == "passed" and case.facts["synthetic_profiles"] is True
                processes = json.loads(case.evidence["processes"].read_bytes())
                assert processes["complete"] is True
        assert json.loads(result.result_path.read_bytes())["planned_denominator"] == 3
    finally:
        if activation.exists() and not any((prepared_root / ".scratch").glob("execution-*")):
            assert not activation.is_symlink() and not activation.is_junction()
            data = json.loads(activation.read_bytes())
            assert data["parent_id"] == parent.parent_id and Path(data["accounting_root"]).resolve() == prepared_root.resolve()
            activation.unlink()


def test_google_low_tooling_recheck_uses_new_bounded_plan_without_replacing_original(acceptance_public_root) -> None:
    import asyncio
    import json
    from tests.support.agent_acceptance import AcceptancePlan, run_plan
    plan = AcceptancePlan(cases=["formal.provider-settings"],protocols=["google"])
    request = acceptance_public_root.with_name("google-tooling-recheck-"+acceptance_public_root.name+".json")
    request.parent.mkdir(parents=True,exist_ok=True)
    request.write_text(json.dumps({"purpose":"Tooling-only recheck after correcting the observed thinking_level wire field.",
        "original_frozen_batch":"b-drw4cgy9", "original_execution_id":"g04:google:1", "replaces_original":False,
        "independent_quality_sample":False,"plan":plan.model_dump(mode="json")},indent=2)+"\n",encoding="utf-8")
    outcome = asyncio.run(run_plan(plan,root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    contract = outcome.facts["connection_contract"]
    assert contract["native_tool_roundtrip"] and contract["effective_native_setting"]
    assert contract["models"] == ["gemini-3.5-flash","gemini-3.5-flash"]
    assert contract["native_settings_per_http"] == [True,True]
    assert contract["prefix_stable"] and contract["tools_stable"] and contract["output_budget_matches_wire"]
    assert outcome.execution_id != "g04:google:1"
    assert outcome.facts["real_http_attempts"] == outcome.facts["normal_profile_accesses"] == 0
    ledger = json.loads(outcome.evidence["ledger.json"].read_bytes())
    assert 0 < len(ledger["attempts"]) <= 12
    assert outcome.semantic_status == "not_measured" and outcome.user_acceptance == "pending"


def test_public_gemini_25_flash_low_uses_exact_native_budget(acceptance_public_root, tmp_path, request) -> None:
    import asyncio
    from dataclasses import asdict
    import json
    from pathlib import Path
    import shutil
    from uuid import uuid4

    from myrk.provider import ProviderConfig
    from tests.support.agent_acceptance import connection_desktop_lifecycle as connection
    from tests.support.agent_acceptance.core import CaseContext
    from tests.support.agent_acceptance.preparation import SOURCE
    from tests.support.agent_acceptance.resume_desktop import run_desktop_phases

    root = acceptance_public_root
    context = CaseContext(root, "google", uuid4().hex, "desktop.gemini-25-flash-low")
    config = ProviderConfig(name="Gemini", protocol="google", model="gemini-2.5-flash",
                            base_url="https://generativelanguage.googleapis.com", output_token_reserve=4096)
    connection.setup(root)
    (root / "pytest-node.json").write_text(json.dumps({"nodeid": request.node.nodeid, "case_id": context.case_id,
        "run_id": context.run_id}), encoding="utf-8")
    scenario = connection.manifest(root, context.protocol)
    (root / "scenario.json").write_text(json.dumps({**scenario, "case_id": context.case_id,
        "config": config.model_dump(mode="json")}), encoding="utf-8")
    replay = root / "script.json"
    replay.write_text(json.dumps({"protocol": context.protocol, "case_id": context.case_id, "run_id": context.run_id,
        "max_http_attempts": 2, "replies": [asdict(reply) for reply in connection.replies()]}), encoding="utf-8")
    processes = asyncio.run(run_desktop_phases(context,
        driver=SOURCE / "tests/support/agent_acceptance/desktop/connection-gemini-low.electron.mjs",
        phases=[{"name": "exact-flash-low", "environment": {}}],
        extra_environment={"MYRK_SERVICE_MODULE": "tests.support.agent_acceptance.resume_service",
            "MYRK_ACCEPTANCE_RESUME_SCRIPT": str(replay), "MYRK_CONNECTION_PROJECT_ROOT": str(SOURCE),
            "MYRK_CONNECTION_GEMINI_ROOT": str(root), "MYRK_USER_DATA_DIRECTORY": str(tmp_path / "gemini-profile")}))
    state = json.loads((root / "state.json").read_bytes())
    assert processes["complete"] and processes["normal_exit_phases"] == 1, processes
    shutil.rmtree(root / "environment")
    assert state["passed"] and state["service_ready"] and state["isolated_profile"], state
    assert state["requested_reasoning"] == "low"
    assert state["new_chat_selection"]["selection"]["reasoning"] == "dynamic"
    assert state["selected_controls"]["selection"] == {"provider_config": config.model_dump(mode="json"), "reasoning": "low"}
    events = json.loads((root / "public-events.json").read_bytes())
    started, finished = [event for event in events if event["event"] in {"started", "finished"}]
    assert started["task_id"] == finished["turn"]["task"]["id"] == state["task_id"]
    saved = json.loads(Path(finished["path"]).read_bytes())["turns"][-1]
    assert finished["saved"] and saved["status"] == "completed"
    for task in (started["task"], saved["task"]):
        assert task["provider_config"] == config.model_dump(mode="json") and task["reasoning"] == "low"
    assert [(action["kind"], action["status"]) for action in saved["actions"]] == [("read", "completed")]

    exchange = json.loads((root / "exchange.json").read_bytes())
    assert len(exchange["exchanges"]) == len(exchange["sdk_inputs"]) == len(exchange["product_attempts"]) == 2
    for wire, sdk, attempt in zip(exchange["exchanges"], exchange["sdk_inputs"], exchange["product_attempts"], strict=True):
        assert wire["url"].split("?", 1)[0] == config.base_url + "/v1beta/models/gemini-2.5-flash:streamGenerateContent"
        body = json.loads(wire["request_utf8"])
        assert body["generationConfig"]["thinkingConfig"] == {"thinking_budget": 2048}
        assert body["generationConfig"]["maxOutputTokens"] == sdk["settings"]["max_tokens"] == 4096
        assert body["tools"] and sdk["messages"]
        assert (attempt["task_id"], attempt["actor"], attempt["purpose"]) == (state["task_id"], "main", "work")
    assert "native-tool-proof-42" in json.dumps(exchange["sdk_inputs"][1]["messages"])


def assert_frozen_execution_budget(case, execution):
    """Check the same original parent ledger for every mixed-adapter execution."""
    import json
    ledger = json.loads(case.evidence["budget.ledger"].read_bytes())
    actual = [item for item in ledger["attempts"] if item["execution_id"] == execution.execution_id]
    assert 0 < len(actual) <= execution.request_cap
    assert all(item["role"] == "product" and item["actor"] == "main" and item["reservation_microusd"] > 0 for item in actual)
    return ledger
