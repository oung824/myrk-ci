"""Queue and parallel contracts through the maintained public acceptance entry."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.agent_acceptance import builtin_registry
from tests.support.agent_acceptance.core import CaseContext
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


def test_screening_registers_original_single_execution_and_shared_request_caps() -> None:
    registry = builtin_registry()
    for name, scenario, cap in (("formal.queue-supplement", "D14", 16), ("formal.parallel-stop", "D15", 24)):
        case = registry.cases[name]
        assert case.owner_ticket == "12"
        assert case.formal_live is True and case.frozen_material_input is True
        assert case.screening["deepseek"]["scenario_id"] == scenario
        assert case.screening["deepseek"]["request_cap"] == cap
    gemini = registry.cases["formal.queue-supplement"].screening["google"]
    assert gemini["scenario_id"] == "G03" and gemini["request_cap"] == 16


def test_actual_main_applies_one_supplement_and_starts_independent_queue_in_order(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["formal.queue-supplement"]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    contract = outcome.facts["queue_contract"]
    assert contract["received_applied_source_identity"] is True
    assert contract["supplement_applied_once"] is True
    assert contract["supplement_first_applied_request"] == 2
    assert contract["actual_supplement_effect_once"] is True
    assert contract["queue_start_order_matches_saved"] is True
    assert contract["partial_failure_preserves_next_goal_and_authorization"] is True
    assert contract["public_progress_matches_saved_records"] is True
    assert contract["duplicate_delivery_id_contract"] == "unknown"
    assert contract["individual_supplement_cancel_contract"] == "unknown"
    assert outcome.semantic_status == "not_measured" and outcome.user_acceptance == "pending"


def test_one_actual_main_keeps_parallel_scopes_separate_and_b_continues_after_a_stop(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["formal.parallel-stop"]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    contract = outcome.facts["queue_contract"]
    for key in ("same_real_main", "distinct_thread_task_ids", "sdk_wire_actions_isolated", "recipient_effects_match",
        "a_stop_blocks_new_sends_and_actions", "b_actual_work_continues_after_a_stop", "separately_stopped",
        "inflight_partial_effects_preserved", "unknown_fee_reservations_kept", "same_execution_all_tasks"):
        assert contract[key] is True, (key, contract)
    assert contract["bounded_parent_requests"] is True
    assert outcome.critical_contracts["K3"] == "passed"
    assert outcome.semantic_status == "not_measured" and outcome.user_acceptance == "pending"


def test_only_explicit_parallel_contract_has_frozen_product_task_stop_scope() -> None:
    registry = builtin_registry()
    rows = {row["case_id"]: row for row in registry.catalogue()}
    assert registry.cases["formal.parallel-stop"].product_stop_scope == "task"
    assert rows["formal.parallel-stop"]["product_stop_scope"] == "task"
    assert all(case.product_stop_scope == "execution" for name, case in registry.cases.items() if name != "formal.parallel-stop")


def test_old_v2_original_integrity_and_stop_survive_read_only_compatibility(tmp_path) -> None:
    from pathlib import Path
    import pytest
    from tests.support.agent_acceptance import LiveGate, SharedControlError

    original = (Path(__file__).parent / "materials/queue-parallel/legacy-ledger-v2.json").read_bytes()
    ledger = tmp_path / "ledger.json"
    ledger.write_bytes(original)
    gate = LiveGate(tmp_path, builtin_registry())
    report = gate.accounting_report()
    assert report["case_stops"]["legacy.stopped:deepseek"]["reason"] == "old stop is permanent"
    assert report["reserved_requests"] == 0 and report["product_task_stops"] == {}
    assert ledger.read_bytes() == original
    ledger.write_bytes(original.replace(b"old stop is permanent", b"old stop was removed"))
    with pytest.raises(SharedControlError, match="integrity changed"):
        gate.accounting_report()


def test_stopped_pending_supplement_stays_unapplied_and_saved_queue_needs_explicit_start(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["queue.supplement-stopped-saved"]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == outcome.product_status == "passed", outcome.facts
    contract = outcome.facts["queue_control"]
    assert contract["pending_supplement_not_applied"] is True
    assert contract["cancelled_supplement_no_effect"] is True
    assert contract["saved_stop_and_queue_identity_preserved"] is True
    assert contract["open_zero_sends"] is True
    assert contract["queue_requires_explicit_start"] is True
    assert outcome.critical_contracts["K3"] == "passed"


@pytest.fixture
def supplement_public_root(acceptance_public_root, request):
    acceptance_public_root.mkdir(parents=True)
    (acceptance_public_root / "pytest-node.json").write_text(json.dumps({"nodeid": request.node.nodeid}), encoding="utf-8")
    return acceptance_public_root


@pytest.mark.parametrize("protocol", ["deepseek", "google"])
@pytest.mark.parametrize("keep", [False, True], ids=["revoked-only", "mixed"])
def test_public_supplement_retries_and_revocation_before_a_final_reply(supplement_public_root, protocol, keep) -> None:
    context = CaseContext(supplement_public_root, protocol, uuid4().hex, "queue.supplement-delivery")
    withdrawn = "SYNTHETIC-WITHDRAW-7812"
    retained = "SYNTHETIC-RETAIN-6519"

    async def run() -> None:
        replies = [Reply(text="First final response.")]
        if keep:
            replies.append(Reply(text="Follow-up final response."))
        async with ServiceChannel(context, "delivery-control", replies, gates=[0, 1] if keep else [0]) as channel:
            await channel.configure()
            await channel.send({"action": "send", "prompt": "Synthetic task with a held first final response."})
            started = await channel.receive("started")
            target = {"path": started["path"], "task_id": started["task_id"]}
            await channel.arrived(0)

            def submit(identifier: str, text: str) -> dict[str, str]:
                return {"action": "submit_supplement", **target, "supplement_id": identifier, "prompt": text}

            async def acknowledge(state: str, identifier: str) -> dict:
                event = await channel.receive("supplement", state=state, **target)
                assert event["saved"] is True and event["supplement"]["id"] == identifier
                return event["supplement"]

            original = submit("withdrawn", withdrawn)
            await channel.send(original)
            first = await acknowledge("received", "withdrawn")
            await channel.send(original)
            assert await acknowledge("received", "withdrawn") == first
            await channel.send({**original, "prompt": "Conflicting redelivery"})
            await channel.receive("error", supplement_id="withdrawn", **target)
            await channel.send({**submit("stale", "Wrong task"), "task_id": "not-this-task"})
            await channel.receive("error", supplement_id="stale")
            revoke = {"action": "revoke_supplement", **target, "supplement_id": "withdrawn"}
            await channel.send(revoke)
            revoked = await acknowledge("revoked", "withdrawn")
            assert revoked["revoked_at"] and revoked["applied_request"] is None
            await channel.send(revoke)
            assert await acknowledge("revoked", "withdrawn") == revoked
            await channel.send(original)
            assert await acknowledge("revoked", "withdrawn") == revoked

            if keep:
                for identifier in ("retained-first", "retained-again"):
                    await channel.send(submit(identifier, retained))
                    await acknowledge("received", identifier)
            channel.release(0)
            if keep:
                await channel.arrived(1)
                await channel.send({**revoke, "supplement_id": "retained-first"})
                await channel.receive("error", supplement_id="retained-first", **target)
                channel.release(1)

            finished = await channel.receive("finished", **target)
            assert finished["saved"] is True and finished["turn"]["status"] == "completed", finished
            await channel.send(submit("late", "Late delivery must not start a task"))
            await channel.receive("error", supplement_id="late", **target)
            assert sum(event["event"] == "started" for event in channel.events) == 1

        exchange = json.loads((channel.root / "exchange.json").read_bytes())
        sdk = [json.dumps(row["messages"], ensure_ascii=False) for row in exchange["sdk_inputs"]]
        wire = [row["request_utf8"] for row in exchange["exchanges"]]
        assert len(sdk) == len(wire) == (2 if keep else 1)
        assert all(withdrawn not in raw for raw in sdk + wire)
        if keep:
            assert retained not in sdk[0] and retained not in wire[0]
            assert sdk[1].count(retained) == wire[1].count(retained) == 2
        saved = json.loads(Path(started["path"]).read_bytes())
        supplements = saved["turns"][-1]["task"]["supplements"]
        assert [row["id"] for row in supplements] == (["withdrawn", "retained-first", "retained-again"] if keep else ["withdrawn"])
        assert supplements[0] == revoked
        assert all(row["applied_request"] == 2 for row in supplements[1:])
        assert not any((row.get("metadata") or {}).get("myrk_supplement_id") == "withdrawn" for row in saved["messages"])

    asyncio.run(run())


@pytest.mark.parametrize("legacy_raw", [False, True], ids=["current-journal", "legacy-raw"])
def test_public_unselected_supplement_can_be_revoked_after_stop_and_reopen(supplement_public_root, legacy_raw) -> None:
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart

    context = CaseContext(supplement_public_root, "deepseek", uuid4().hex, "queue.supplement-reopened")
    text = "SYNTHETIC-WITHDRAW-AFTER-REOPEN-1209"

    async def run() -> None:
        async with ServiceChannel(context, "stop-with-pending", [Reply(text="Held response.")], gates=[0]) as original:
            await original.configure()
            await original.send({"action": "send", "prompt": "Original synthetic task."})
            started = await original.receive("started")
            target = {"path": started["path"], "task_id": started["task_id"]}
            await original.arrived(0)
            submit = {"action": "submit_supplement", **target, "supplement_id": "pending", "prompt": text}
            await original.send(submit)
            await original.receive("supplement", state="received", **target)
            await original.send({"action": "stop", "path": started["path"]})
            stopped = await original.receive("finished", **target)
            assert stopped["saved"] and stopped["turn"]["status"] == "cancelled"

        if legacy_raw:
            path = Path(started["path"])
            record = json.loads(path.read_bytes())
            assert record["version"] == 10
            request = ModelRequest(parts=[UserPromptPart(content=text)], metadata={"myrk_supplement_id": "pending"})
            record["messages"].extend(ModelMessagesTypeAdapter.dump_python([request], mode="json"))
            path.write_text(json.dumps(record), encoding="utf-8")

        replies = [Reply(text="Original task resumed."), Reply(text="Next independent task finished.")]
        async with ServiceChannel(context, "reopen-and-revoke", replies) as reopened:
            await reopened.send({"action": "open", "path": started["path"]})
            opened = await reopened.receive("conversation", path=started["path"])
            assert opened["turns"][-1]["task"]["id"] == started["task_id"]
            await reopened.send({"action": "revoke_supplement", **target, "supplement_id": "pending"})
            revoked = await reopened.receive("supplement", state="revoked", **target)
            assert revoked["saved"] and revoked["supplement"]["revoked_at"]
            await reopened.send(submit)
            echoed = await reopened.receive("supplement", state="revoked", **target)
            assert echoed["supplement"] == revoked["supplement"]
            await reopened.configure()
            await reopened.send({"action": "send", "path": target["path"], "prompt": "Continue the stopped task."})
            await reopened.receive("started", **target)
            finished = await reopened.receive("finished", **target)
            assert finished["saved"] and finished["turn"]["status"] == "completed", finished
            await reopened.send({"action": "send", "path": started["path"], "new_task": True, "prompt": "Next synthetic task."})
            next_task = await reopened.receive("started", path=started["path"])
            assert next_task["task_id"] != target["task_id"]
            next_finished = await reopened.receive("finished", task_id=next_task["task_id"])
            assert next_finished["saved"] and next_finished["turn"]["status"] == "completed", next_finished
        exchange = json.loads((reopened.root / "exchange.json").read_bytes())
        assert len(exchange["sdk_inputs"]) == len(exchange["exchanges"]) == 2
        assert text not in json.dumps(exchange["sdk_inputs"], ensure_ascii=False)
        assert all(text not in row["request_utf8"] for row in exchange["exchanges"])
        assert finished["turn"]["task"]["supplements"] == [revoked["supplement"]]
        if legacy_raw:
            saved = json.loads(Path(started["path"]).read_bytes())
            assert any((row.get("metadata") or {}).get("myrk_supplement_id") == "pending" for row in saved["messages"])

    asyncio.run(run())


def test_public_supplement_id_belongs_to_one_task_in_a_conversation(supplement_public_root) -> None:
    context = CaseContext(supplement_public_root, "deepseek", uuid4().hex, "queue.supplement-task-identity")
    texts = ("SYNTHETIC-FIRST-TASK-1545", "SYNTHETIC-SECOND-TASK-8924")

    async def run() -> None:
        replies = [Reply(text="Fixed synthetic reply.") for _ in range(4)]
        async with ServiceChannel(context, "two-tasks", replies, gates=[0, 2]) as channel:
            await channel.configure()
            task_ids = []
            path = None
            for index, text in enumerate(texts):
                await channel.send({"action": "send", "path": path, "prompt": f"Synthetic task {index + 1}.", "new_task": True})
                started = await channel.receive("started")
                assert path is None or started["path"] == path
                path = started["path"]
                task_ids.append(started["task_id"])
                target = {"path": path, "task_id": started["task_id"]}
                await channel.arrived(2 * index)
                await channel.send({"action": "submit_supplement", **target, "supplement_id": "client-id", "prompt": text})
                received = await channel.receive("supplement", state="received", **target)
                assert received["saved"] and received["supplement"]["text"] == text
                channel.release(2 * index)
                finished = await channel.receive("finished", **target)
                assert finished["saved"] and finished["turn"]["status"] == "completed"
            assert len(set(task_ids)) == 2

        exchange = json.loads((channel.root / "exchange.json").read_bytes())
        sdk = [json.dumps(row["messages"], ensure_ascii=False) for row in exchange["sdk_inputs"]]
        wire = [row["request_utf8"] for row in exchange["exchanges"]]
        assert len(sdk) == len(wire) == 4
        for index, text in enumerate(texts):
            assert text not in sdk[2 * index] and text not in wire[2 * index]
            assert sdk[2 * index + 1].count(text) == wire[2 * index + 1].count(text) == 1

    asyncio.run(run())


def test_partial_service_failure_keeps_raw_error_and_next_queued_goal_unchanged(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["queue.partial-service-error"]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == "passed" and outcome.product_status == "failed", outcome.facts
    contract = outcome.facts["queue_control"]
    assert contract["original_partial_effect_preserved"] is True
    assert contract["raw_service_error_preserved"] is True
    assert contract["failure_not_relabelled_stop"] is True
    assert contract["next_goal_and_authorization_unchanged"] is True
    assert contract["next_queued_goal_completed"] is True
    assert contract["original_effects_verified_before_next"] is True
    assert contract["next_status"] == "completed" and contract["next_error"] is None
    assert contract["planned_following_responses_unexecuted"] == 0
    assert outcome.semantic_status == "not_measured"


def test_stream_failure_retains_received_fragment_and_never_counts_as_correct_stop(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan, run_plan

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["queue.partial-stream-error"]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == "passed" and outcome.product_status == "failed", outcome.facts
    contract = outcome.facts["queue_control"]
    assert contract["original_partial_effect_preserved"] is True
    assert contract["raw_service_error_preserved"] is True
    assert contract["stream_fragment_preserved"] is True
    assert contract["failure_not_relabelled_stop"] is True
    assert contract["next_goal_and_authorization_unchanged"] is True
    assert contract["original_status"] == "failed"
    assert contract["original_effects_verified_before_next"] is True
    assert contract["next_queued_goal_completed"] is True



def test_qg5_contribution_keeps_six_owned_slots_and_independent_tuning_materials():
    import json
    from pathlib import Path
    from hashlib import sha256
    folder = Path(__file__).parent / "materials/queue-parallel"
    value = json.loads((folder / "qg5-templates.json").read_bytes())
    formal = [row for row in value["templates"] if row["role"] == "candidate_formal_holdout"]
    tuning = [row for row in value["templates"] if row["role"] == "tuning_only"]
    assert {row["ordinal"] for row in formal} == {1, 2, 3, 4, 7, 10}
    assert len(tuning) == 2 and len({row["task_form"] for row in formal}) >= 4
    assert len({row["family_id"] for row in formal + tuning}) == 8
    assert len({sha256((folder / row["material"]).read_bytes()).hexdigest() for row in formal + tuning}) == 8
    assert value["quality_denominator"] == 0 and value["sending_authorized"] is False
    assert all(not row["executed"] and json.loads((folder / row["gold"]).read_bytes())["visibility"] == "review_only_never_model_input" for row in formal + tuning)


def test_three_frozen_ordinary_scenarios_share_original_parent_and_stopped_task_cannot_escape(acceptance_public_root, prepared_root):
    import asyncio
    from base64 import b64decode
    from hashlib import sha256
    import json
    from pathlib import Path
    import pytest
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import (AcceptancePlan, FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate,
        SharedControlError, prepare_live, run_plan)
    from tests.support.agent_acceptance.preparation import CaseBudgetExceeded
    folder = Path(__file__).parent / "materials/queue-parallel"
    specs = [("formal.queue-supplement", "deepseek", "D14", 16, "d14.txt"),
        ("formal.parallel-stop", "deepseek", "D15", 24, "d15.txt"),
        ("formal.queue-supplement", "google", "G03", 16, "d14.txt")]
    rows = [FrozenExecution(execution_id=f"{scenario}:{protocol}:single", case_id=case, protocol=protocol, scenario_id=scenario,
        material_family_id="queue-screening-" + scenario.lower(), material_variant="baseline", material_path=folder / file,
        material_sha256=sha256((folder / file).read_bytes()).hexdigest(), startpoint=scenario + "-isolated", order=index,
        request_cap=cap, cost_cap_microusd=cap * 122880) for index, (case, protocol, scenario, cap, file) in enumerate(specs, 1)]
    parent = FrozenParentBudget(parent_id="synthetic-queue-" + prepared_root.name, accounting_root=prepared_root, request_cap=56,
        phase_microusd={"screening": 56 * 122880, "quality": 0, "judge": 0}, reserve_microusd=0, profile="synthetic")
    plan = AcceptancePlan(executions=rows, parent_budget=parent)
    registry = builtin_registry()
    activation = project_root(Path(__file__).resolve()) / ".scratch/acceptance-parent-budgets" / (sha256(parent.parent_id.encode()).hexdigest() + ".json")
    assert not activation.exists()
    try:
        bundle = prepare_live(plan.model_copy(update={"mode": "prepare-live"}), root=prepared_root, registry=registry)
        grant = LiveAuthorization(authorization_id="synthetic-queue-terminal-only", binding=bundle.binding, transport="mock", parent_budget_id=parent.parent_id,
            total_limit=56, total_microusd=56 * 122880, case_limits={row.execution_id: row.request_cap for row in rows},
            case_microusd={row.execution_id: row.cost_cap_microusd for row in rows})
        gate = LiveGate(prepared_root, registry, grant)
        result = asyncio.run(run_plan(plan, root=acceptance_public_root, registry=registry, live_gate=gate))
        assert [case.execution_id for case in result.cases] == [row.execution_id for row in rows]
        for case, row in zip(result.cases, rows):
            assert case.tool_status == case.product_status == "passed", case.facts
            assert case.facts["real_http_attempts"] == case.facts["normal_profile_accesses"] == 0
            assert case.facts["processes_complete"] is True
            assert case.semantic_status == "not_measured" and case.user_acceptance == "pending"
            assert case.facts["queue_contract"]["same_execution_all_tasks"] is True
            assert case.facts["output_standard"]["dimensions"]["stop_effect"]["applicable"] is (row.scenario_id == "D15")
        report = gate.accounting_report()
        assert report["parent_budget_id"] == parent.parent_id and report["request_limit"] == 56
        assert report["reserved_requests"] == 22 and not report["case_stops"] and not report["scope_stops"]
        assert len(report["product_task_stops"]) == 2
        owners = report["product_request_owners"]
        assert len(owners) == 6 and {item["execution_id"] for item in owners.values()} == {rows[1].execution_id}
        first_number = min(map(int, owners))
        owner = owners[str(first_number)]
        source = owner["sources"][0]
        raw = b64decode(source["original_base64"], validate=True)
        assert len(raw) == source["bytes"] and sha256(raw).hexdigest() == source["sha256"]
        record = json.loads(raw)
        assert record["turns"][-1]["task"]["id"] == owner["task_id"]
        replay = prepared_root / ".scratch/stopped-task-direct-control"
        replay.mkdir()
        (replay / "actual-source.json").write_bytes(raw)
        attempt = report["attempts"][first_number - 1]
        parameters = dict(case_id=rows[1].case_id, protocol="deepseek", actor=attempt["actor"], purpose=attempt["purpose"],
            product_number=attempt["product_number"], product_step_id=attempt["product_step_id"], delegation_id=attempt["delegation_id"],
            sdk_input_id=attempt["sdk_input_id"], request_sha256=attempt["request_sha256"], product_records_root=replay)
        scoped = gate.for_execution(rows[1].execution_id)
        with pytest.raises(CaseBudgetExceeded, match="Actual product Task was stopped"):
            scoped.reserve(**parameters)
        for changed in ({"product_step_id": "not-owned-new-step"}, {"actor": "action_review"}, {"delegation_id": "not-owned-child"}):
            with pytest.raises(SharedControlError, match="one actual product Task"):
                scoped.reserve(**{**parameters, **changed})
        with pytest.raises(SharedControlError, match="actual frozen product record root"):
            scoped.reserve(**{**parameters, "product_records_root": None})
        final = gate.accounting_report()
        assert final["reserved_requests"] == report["reserved_requests"] and final["occupied_microusd"] == report["occupied_microusd"]
        assert json.loads(result.result_path.read_bytes())["planned_denominator"] == 3
    finally:
        if activation.exists() and not any((prepared_root / ".scratch").glob("execution-*")):
            body = activation.read_bytes()
            value = json.loads(body)
            assert value["parent_id"] == parent.parent_id and Path(value["accounting_root"]).resolve() == prepared_root.resolve()
            (acceptance_public_root / "parent-activation.original.json").write_bytes(body)
            activation.unlink()



def test_real_saved_task_stop_refuses_changed_step_actor_child_and_missing_owner_without_http(prepared_root):
    import json
    from pathlib import Path
    import pytest
    from tests.support.agent_acceptance import AcceptancePlan, LiveAuthorization, LiveGate, SharedControlError, prepare_live
    from tests.support.agent_acceptance.preparation import CaseBudgetExceeded
    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=["formal.parallel-stop"], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-direct-stop-no-terminal", binding=bundle.binding, transport="mock", total_limit=24,
        case_limits={"formal.parallel-stop":24}, total_microusd=24*122880, case_microusd={"formal.parallel-stop":24*122880})
    gate = LiveGate(prepared_root, registry, grant)
    folder = Path(__file__).parent / "materials/queue-parallel"
    records = prepared_root / ".scratch/direct-product-records"
    records.mkdir(parents=True)
    source = records / "actual-a.json"
    source.write_bytes((folder / "actual-a-request-pending.json").read_bytes())
    final = json.loads((folder / "actual-a-stopped.json").read_bytes())
    turn = final["turns"][-1]
    assert turn["status"] == "cancelled"
    request = turn["request_attempts"][0]
    parameters = dict(case_id="formal.parallel-stop", protocol="deepseek", actor=request["actor"], purpose=request["purpose"],
        product_number=request["number"], product_step_id=request["step_id"], delegation_id=request["delegation_id"],
        sdk_input_id="direct-control-no-sdk-send", request_sha256=request["observation"]["wire"]["body_sha256"], product_records_root=records)
    number = gate.reserve(**parameters)
    assert number == 1
    source.write_bytes((folder / "actual-a-stopped.json").read_bytes())
    gate.observe_product_stop(source, case_id="formal.parallel-stop", protocol="deepseek")
    before = gate.accounting_report()
    assert len(before["product_task_stops"]) == 1 and not before["case_stops"]
    with pytest.raises(CaseBudgetExceeded, match="Actual product Task was stopped"):
        gate.reserve(**parameters)
    for changed in ({"product_step_id":"unknown-new-step"}, {"actor":"action_review"}, {"delegation_id":"unknown-child"}):
        with pytest.raises(SharedControlError, match="one actual product Task"):
            gate.reserve(**{**parameters, **changed})
    with pytest.raises(SharedControlError, match="actual frozen product record root"):
        gate.reserve(**{**parameters, "product_records_root":None})
    after = gate.accounting_report()
    assert after["reserved_requests"] == 1 and after["actual_http"] == 0 and after["occupied_microusd"] == before["occupied_microusd"]
    gate.mark_not_sent(number, fault_origin="direct negative control never acquired a terminal sender")
