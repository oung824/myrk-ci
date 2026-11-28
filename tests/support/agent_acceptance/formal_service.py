"""Formal desktop service with an explicit terminal-only qualification transport."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
import json
import os
import sys
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import httpx2
from pydantic_ai import models
from pydantic_ai.models import Model

from myrk import desktop_service
from myrk.observed_http import ObservedHTTPTransport
from myrk.provider import deepseek_model
from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance.core import SharedControlError
from tests.support.agent_acceptance.replay import OfflineSession, Reply, SDKCapture, active_sdk_input
from tests.support.agent_acceptance.formal import verify_authority
from tests.support.agent_acceptance.formal_transport import _AuthorizedTransport as _AuthorizedTransport
from tests.support.agent_acceptance.save_diagnostics import observe_saves
from tests.support.process_receipt import write_process_receipt


async def main() -> None:
    authority_path = Path(os.environ["MYRK_ACCEPTANCE_AUTHORITY"])
    value, gate = verify_authority(authority_path)
    grant = value.grant
    gate.validate(case_id=value.case_id, protocol=value.protocol)
    write_process_receipt(Path(os.environ["MYRK_PROCESS_RECEIPT_DIRECTORY"]), __file__)
    role = os.environ["MYRK_ACCEPTANCE_ROLE"]
    directory = Path(os.environ["MYRK_DATA_DIRECTORY"])
    if value.case_id == "desktop.handoff":
        replies = [Reply(text="Formal handoff completed.")]
    else:
        from tests.support.agent_acceptance.formal_scenarios import replies as scenario_replies
        replies = scenario_replies(value.case_id, value.scenario_root or value.runtime, value.protocol, value.phase)
    session = OfflineSession(value.protocol, [], config=value.provider_config)
    terminal_mock = OfflineSession(value.protocol, replies, config=value.provider_config)
    from tests.support.agent_acceptance.compaction_continuity import FORMAL_CASES as CONTINUITY_CASES, ContinuityMock
    continuity = ContinuityMock(value.case_id,value.protocol,value.provider_config) if value.case_id in CONTINUITY_CASES and grant.transport == "mock" else None
    adoption = None
    if grant.transport == "mock" and continuity is None:
        from tests.support.agent_acceptance.formal_adoption import adoption_mock

        adoption = adoption_mock(value.case_id, value.protocol, config=value.provider_config)
    directory.mkdir(parents=True, exist_ok=True)
    if value.case_id != "desktop.handoff":
        from tests.support.agent_acceptance.formal_scenarios import manifest

        approval = manifest(value.case_id, value.scenario_root or value.runtime, value.protocol)["approval"]
        if approval is not None:
            permissions = directory / "settings/permissions.json"
            permissions.parent.mkdir(parents=True, exist_ok=True)
            permissions.write_text(json.dumps(approval), encoding="utf-8")

    quality_terminal = None
    if value.case_id.startswith("quality.") and grant.transport == "mock":
        from tests.support.agent_acceptance.quality_runtime import terminal_mock as quality_mock
        quality_terminal = quality_mock(value.case_id, value.scenario_root or value.runtime, value.protocol, value.provider_config, phase=value.phase)

    pending_observations: list[dict[str, object]] = []
    stop_boundaries: list[dict[str, object]] = []
    original_emit = desktop_service.emit

    def observe_stop(event: str, **data: object) -> None:
        if event == "stopping":
            terminal = [{"attempt": row["attempt"], "sdk_input_id": row["sdk_input_id"]}
                        for row in session.exchanges if row.get("terminal_http_started") is True]
            stop_boundaries.append({"event": {"event": event, **data}, "terminal_attempts": terminal})
        original_emit(event, **data)

    def persist(*, finalizing: bool = False) -> None:
        attempts = {}
        task_id = ""
        for path in directory.rglob("*.json"):
            if path.parent != directory and not path.parent.name.endswith("-delegations"):
                continue
            record = json.loads(path.read_bytes())
            if "turns" in record:
                gate.observe_product_stop(path, case_id=value.case_id, protocol=value.protocol, finalizing=finalizing)
            for turn in record.get("turns", []):
                task_id = (turn.get("task") or {}).get("id", task_id)
                for attempt in [*turn.get("request_attempts", []), *(request for action in turn.get("actions", [])
                    if action.get("review") for request in action["review"].get("request_attempts", []))]:
                    attempts[(task_id, attempt["number"], attempt.get("actor"), attempt.get("purpose"))] = {"task_id": task_id, **attempt}
        exchange_path = session.save(value.runtime / role / "exchange.json", attempts=list(attempts.values()),
            run_id=value.run_id, case_id=value.case_id, task_id=task_id)
        if pending_observations or stop_boundaries:
            observed = json.loads(exchange_path.read_bytes())
            if pending_observations:
                observed["pending_observations"] = pending_observations
            if stop_boundaries:
                observed["stop_boundaries"] = stop_boundaries
            exchange_path.write_text(json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (value.runtime / role / "transport.json").write_text(json.dumps(transport.observation, indent=2) + "\n", encoding="utf-8")

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if role != "receiver":
            raise SharedControlError("The connection owner is not permitted to send model requests.")
        attempt = current_request_attempt()
        if quality_terminal is not None:
            quality_input = next(item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get())
            if attempt is None:
                raise SharedControlError("Quality terminal requires its actual native request purpose.")
            response = quality_terminal.respond(request, quality_input, attempt.purpose)
            if not isinstance(response, httpx2.Response):
                raise SharedControlError("Quality terminal returned an invalid terminal HTTP response.")
            from tests.support.agent_acceptance.quality_runtime import terminal_response_barrier as quality_response_barrier
            observed = await quality_response_barrier(value.case_id, value.scenario_root or value.runtime, len(session.exchanges) - 1, quality_input, response)
            if not isinstance(observed, httpx2.Response):
                raise SharedControlError("Quality terminal barrier returned an invalid HTTP response.")
            return observed
        if continuity is not None:
            continuity_input = next(item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get())
            if attempt is None: raise SharedControlError("Continuity fixture requires its actual request purpose.")
            return continuity.respond(request,continuity_input,attempt.purpose)
        if adoption is not None:
            captured = [item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get()]
            if attempt is None or len(captured) != 1:
                raise SharedControlError("The adoption-only Mock requires its actual SDK input and request purpose.")
            return adoption.respond(request, captured[0], purpose=attempt.purpose)
        if attempt is not None and attempt.purpose == "compaction" and value.case_id != "desktop.handoff":
            from tests.support.agent_acceptance.formal_scenarios import summary_reply
            terminal_mock.replies.insert(len(terminal_mock.exchanges), summary_reply(value.case_id))
        if value.case_id == "formal.recovery-family" and value.phase == "work" and len(terminal_mock.exchanges) == 1:
            assert value.scenario_root is not None
            (value.scenario_root / "family-child-arrived.flag").write_text(str(os.getpid()), encoding="utf-8")
            async with asyncio.timeout(30):
                while not (value.scenario_root / "family-child-release.flag").exists():
                    await asyncio.sleep(.025)
        response = terminal_mock.respond(request)
        from tests.support.agent_acceptance.independent_review import FORMAL_CASES, terminal_response_barrier
        if value.case_id in FORMAL_CASES:
            review_input = next(item for item in session.sdk_inputs if item['sdk_input_id'] == active_sdk_input.get())
            await terminal_response_barrier(value.case_id, value.scenario_root or value.runtime, review_input)
        from tests.support.agent_acceptance import queue_parallel_stop
        if value.case_id in queue_parallel_stop.SCENARIOS:
            queue_input = next(item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get())
            response = await queue_parallel_stop.terminal_response_barrier(value.case_id, value.scenario_root or value.runtime, len(terminal_mock.exchanges) - 1, queue_input, response)
        if value.case_id.startswith("quality."):
            from tests.support.agent_acceptance.quality_runtime import terminal_response_barrier as quality_response_barrier
            quality_input = next(item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get())
            observed = await quality_response_barrier(value.case_id, value.scenario_root or value.runtime, len(session.exchanges) - 1, quality_input, response)
            if not isinstance(observed, httpx2.Response):
                raise SharedControlError("Quality terminal barrier returned an invalid HTTP response.")
            return observed
        return response

    def local_failure() -> bool:
        if value.phase != "work" or role != "receiver":
            return False
        if value.case_id in {"formal.recovery-family", "quality.qg5.formal.8"}:
            return any(work["status"] == "completed" and turn.get("task", {}).get("supplements")
                for path in directory.glob("*.json") for turn in json.loads(path.read_bytes()).get("turns", [])
                for work in turn.get("delegations", []))
        if value.case_id != "formal.restart":
            return False
        return any(action["kind"] == "check" and action["status"] == "completed"
            for path in directory.glob("*.json") for turn in json.loads(path.read_bytes()).get("turns", [])
            for action in turn.get("actions", []))

    transport = _AuthorizedTransport(gate, value.case_id, session, role,
        httpx2.MockTransport(respond) if grant.transport == "mock" else httpx2.AsyncHTTPTransport(), persist, local_failure, product_records_root=directory, material_root=value.scenario_root,
        hold_stop_response=value.case_id == "quality.qg5.formal.9" and value.phase == "work",
        fault_origin="planned local pre-send failure after completed child" if value.case_id in {"formal.recovery-family", "quality.qg5.formal.8"} else "planned local pre-send failure after the real completed check")

    def local_save_boundary(event: str, args: tuple[object, ...]) -> None:
        if value.case_id not in {"formal.recovery-unknown", "quality.qg5.formal.6"} or value.phase != "work" or role != "receiver" or event != "os.rename":
            return
        pending = Path(str(args[0]))
        if not str(pending).endswith(".json.tmp") or not pending.resolve().is_relative_to(directory.resolve()):
            return
        raw = pending.read_bytes()
        record = json.loads(raw)
        if not record.get("turns") or not any(action["kind"] == "edit" and action["status"] == "completed" for action in record["turns"][-1]["actions"]):
            return
        root = value.scenario_root
        assert root is not None
        reliable = Path(str(args[1])).read_bytes()
        (root / "reliable-original.json").write_bytes(reliable)
        (root / "pending-original.json").write_bytes(raw)
        target = root / "materials/notes.txt"
        if value.case_id == "quality.qg5.formal.6":
            from tests.support.agent_acceptance.quality_runtime import bound_task
            from tests.support.agent_acceptance.quality_recovery_resume import facts as recovery_facts
            _, visible, _ = bound_task(value.case_id, root)
            target = root / "materials" / recovery_facts(visible).target
        (root / "effect-at-boundary.txt").write_bytes(target.read_bytes())
        pending_observations.extend({"source_path": str(pending), "source_sha256": sha256(raw).hexdigest(),
            "task_id": turn["task"]["id"], "attempts": [{"task_id": turn["task"]["id"], **attempt} for attempt in turn["request_attempts"]], "committed": False,
            "source_kind": "uncommitted_pending_record"} for turn in record["turns"] if turn.get("task"))
        persist()
        (root / "save-boundary.json").write_text(json.dumps({"kind": "local_save_boundary_exit", "pid": os.getpid(),
            "exit_code": 71, "reliable": str(args[1]), "pending": str(pending),
            "reliable_sha256": sha256(reliable).hexdigest(), "pending_sha256": sha256(raw).hexdigest()}, indent=2), encoding="utf-8")
        os._exit(71)

    sys.addaudithook(local_save_boundary)

    class OwnedClient(httpx2.AsyncClient):
        async def aclose(self) -> None:
            pass

    client = OwnedClient(transport=ObservedHTTPTransport(transport), trust_env=False)
    @asynccontextmanager
    async def default_model(key: str) -> AsyncIterator[Model]:
        async with deepseek_model(key, http_client=client) as model:
            yield model

    observe_boundary = value.case_id == "quality.qg3.formal.9" and value.phase == "work" and role == "receiver"
    observer = patch.object(desktop_service, "emit", observe_stop) if observe_boundary else nullcontext()
    with observe_saves(gate.root, value.runtime / role / "save-diagnostics.json"), observer:
        try:
            if grant.transport == "mock":
                models.ALLOW_MODEL_REQUESTS = True
            persist()
            await desktop_service.serve(directory, default_model, http_client=client, model_observer=lambda model: SDKCapture(model, session.sdk_inputs))
        finally:
            try:
                persist(finalizing=True)
            finally:
                await httpx2.AsyncClient.aclose(client)


if __name__ == "__main__":
    asyncio.run(main())
