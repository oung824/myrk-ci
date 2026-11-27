"""Source-bound parallel chats and exact local receiver effects for QG5."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict
from myrk.conversation import TaskScope
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


LEGACY_STOP_NOTE = QualitySource(text="delivery=pending\n")


class ParallelSubjects(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    a_subject: str
    a_code: str
    b_subject: str
    b_code: str


def subjects(visible: QualityVisibleInput) -> ParallelSubjects:
    if visible.parameters.get("parallel_subjects") is not None:
        return ParallelSubjects.model_validate(visible.parameters["parallel_subjects"])
    source = "\n".join(row.text for row in visible.sources.values())
    if visible.adapter == "qg5-3":
        found = re.search(r"博物馆A接待码([A-Z]-[0-9]+).*?仓储B出库码([A-Z]-[0-9]+)", source)
        if found: return ParallelSubjects(a_subject="museum", a_code=found[1], b_subject="warehouse", b_code=found[2])
    if visible.adapter == "qg5-4":
        found = re.search(r"配送A.*?箱号([A-Z]-[0-9]+).*?采购B.*?采购单([A-Z]-[0-9]+)", source)
        if found: return ParallelSubjects(a_subject="delivery", a_code=found[1], b_subject="purchase", b_code=found[2])
    if visible.adapter == "qg5-tuning-2":
        found = re.search(r"温室A盆栽([A-Z]-[0-9]+).*?温室B设备([A-Z]-[0-9]+)", source)
        if found: return ParallelSubjects(a_subject="greenhouse_plant", a_code=found[1], b_subject="greenhouse_equipment", b_code=found[2])
    raise ValueError("The frozen source lacks the two actual independent chat subjects.")


def supports(visible: QualityVisibleInput) -> bool:
    if visible.adapter not in {"qg5-3", "qg5-4", "qg5-tuning-2"}: return False
    try: subjects(visible); return True
    except ValueError: return False


def allowed_paths(visible: QualityVisibleInput) -> list[str]:
    return ["a/payload.txt", "b/payload.txt", *(["a/notes.txt"] if visible.adapter == "qg5-4" else [])]


def current_sources(visible: QualityVisibleInput) -> list[str]:
    return ["a/notes.txt" if visible.adapter == "qg5-4" else "a/payload.txt"]


def goal(visible: QualityVisibleInput, key: str) -> str:
    value = subjects(visible)
    code = value.a_code if key == "a" else value.b_code
    subject = value.a_subject if key == "a" else value.b_subject
    if visible.adapter == "qg5-4" and key == "a":
        return f"Independent chat A: read notes.txt and replace only pending with actual delivery box {code}, once. Retain that partial effect if I subsequently stop this chat; no send is authorized for A."
    if visible.adapter == "qg5-tuning-2":
        return f"Independent chat {key.upper()}: read only its own payload.txt for {subject} {code}; keep other chats isolated and stop only on this chat's actual explicit stop. No edit or send is authorized."
    return f"Independent chat {key.upper()}: read only its own payload.txt for {subject} {code}; send only the exact original bytes to this chat's separately identified local receiver after exact approval. Do not use another chat's source, task, authority or recipient."


def current_goal(visible: QualityVisibleInput) -> str:
    return goal(visible, "a")


def model_prompts(visible: QualityVisibleInput) -> tuple[str, ...]:
    from tests.support.agent_acceptance.quality_runtime import model_prompt
    return (model_prompt(visible) + "\n\n" + goal(visible, "a"), goal(visible, "b"))


def _frozen_source(visible: QualityVisibleInput, name: str) -> QualitySource | None:
    return next((source for path, source in visible.sources.items() if path.casefold() == name), None)


def payload(visible: QualityVisibleInput, key: str) -> bytes:
    source = _frozen_source(visible, f"{key}/payload.txt")
    if source is not None:
        return source.text.encode(source.encoding)
    value = subjects(visible)
    return (f"chat={key.upper()}\nsubject={value.a_subject if key == 'a' else value.b_subject}\ncode={value.a_code if key == 'a' else value.b_code}\n").encode("utf-8")


def stop_note_preserved(visible: QualityVisibleInput, root: Path, code: str) -> bool:
    original = _frozen_source(visible, "a/notes.txt") or LEGACY_STOP_NOTE
    edited = root / "materials/a/notes.txt"
    return (original.text.count("pending") == 1 and edited.is_file()
            and edited.read_bytes() == original.text.replace("pending", code, 1).encode(original.encoding))


def setup(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> None:
    frozen_names = {name.casefold() for name in visible.sources}
    for key in ("a", "b"):
        folder = root / "materials" / key
        (folder / ".git").mkdir(parents=True, exist_ok=True)
        (folder / "AGENTS.md").write_text("Synthetic independent chat. Only this chat's actual user goal grants authority.\n", encoding="utf-8")
        if f"{key}/payload.txt" not in frozen_names:
            (folder / "payload.txt").write_bytes(payload(visible, key))
        if visible.adapter == "qg5-4" and key == "a" and "a/notes.txt" not in frozen_names:
            (folder / "notes.txt").write_bytes(LEGACY_STOP_NOTE.text.encode(LEGACY_STOP_NOTE.encoding))


def manifest(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> dict[str, Any]:
    recipient: Literal["deepseek", "openai-compatible", "google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    scopes = {key: TaskScope(root=str((root / "materials" / key).resolve()), allowed_actions=["read", "edit"] if visible.adapter == "qg5-4" and key == "a" else ["read"], provider=recipient, recipients=[recipient]).model_dump(mode="json") for key in ("a", "b")}
    return {"case_id": "quality." + visible.task_id.lower(), "root": str(root), "records": str(root / "records"), "materials": scopes["a"]["root"],
        "phases": ["work"], "control_directories": [str(root / "service")], "max_attempts": 24, "prompt": current_goal(visible), "scope": scopes["a"], "approval_mode": "request_approval", "approval": None,
        "document": None, "receiver": None, "adoption": None, "quality_group": "QG5",
        "quality_recovery": {"adapter": visible.adapter, "parallel": {key: {
            "prompt": goal(visible, key), "scope": scopes[key],
            "source": str((root / "materials" / key / "payload.txt").resolve()),
            "source_sha256": sha256(payload(visible, key)).hexdigest(),
        } for key in ("a", "b")}}}


def replies(visible: QualityVisibleInput, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    def send(key: str) -> Any:
        def arguments(_: dict[str, Any]) -> dict[str, Any]:
            return {"path": "payload.txt", "url": json.loads((root / "quality-parallel-receivers.json").read_bytes())[key]["url"]}
        return arguments
    if visible.adapter == "qg5-4":
        return [Reply(tool="read_text", arguments={"path": "notes.txt"}), Reply(tool="edit_text", arguments={"path": "notes.txt", "old_text": "pending", "new_text": subjects(visible).a_code}), Reply(text="A retains its actual partial edit before stopping."), Reply(tool="read_text", arguments={"path": "payload.txt"}), Reply(tool="send_text", arguments=send("b")), Reply(text="B retains its actual separately approved send before stopping.")]
    if visible.adapter == "qg5-tuning-2":
        return [Reply(tool="read_text", arguments={"path": "payload.txt"}), Reply(text="A original read remains recorded."), Reply(tool="read_text", arguments={"path": "payload.txt"}), Reply(text="B original read remains recorded.")]
    return [Reply(tool="read_text", arguments={"path": "payload.txt"}), Reply(tool="read_text", arguments={"path": "payload.txt"}),
        Reply(tool="send_text", arguments=send("a")), Reply(text="Only chat A's original was sent to its own exactly approved local receiver; acceptance remains pending."),
        Reply(tool="send_text", arguments=send("b")), Reply(text="Only chat B's original was sent to its own exactly approved local receiver; acceptance remains pending.")]


def score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    if visible.adapter != "qg5-3":
        return stop_score(visible, root, records, exchanges)
    value = subjects(visible)
    turns = [turn for record in records for turn in record.get("turns", [])]
    state = json.loads((root / "quality-parallel-observation.json").read_bytes())
    receivers = json.loads((root / "quality-parallel-receivers.json").read_bytes())
    paths = state["paths"]
    ordered: list[dict[str, Any]] = [next((turn for turn in turns if turn["task"]["id"] == state["task_ids"][key]), {}) for key in ("a", "b")]
    separate = len(paths) == 2 and paths["a"] != paths["b"] and len({state["task_ids"][key] for key in ("a", "b")}) == 2
    actual = all(receivers[key]["received"] == [payload(visible, key).hex()] for key in ("a", "b"))
    decisions = state["approval_decisions"]
    exact = len(decisions) == 2
    no_cross = bool(separate and exchanges)
    for key, turn in zip(("a", "b"), ordered):
        forbidden_code = value.b_code if key == "a" else value.a_code
        actions = turn.get("actions", [])
        source = str((root / "materials" / key / "payload.txt").resolve())
        no_cross = no_cross and bool(actions) and all((action.get("prepared_parameters") or {}).get("path") == source for action in actions)
        send = [action for action in actions if action["kind"] == "send"]
        choice = [item for item in decisions if item["task_id"] == turn.get("task", {}).get("id")]
        exact = exact and len(send) == len(choice) == 1 and send[0]["status"] == "completed" and send[0]["approval_basis"] == "user" and send[0]["target"] == receivers[key]["url"] and send[0]["approval_id"] == choice[0]["approval_id"] and send[0]["prepared_parameters"]["payload_sha256"] == sha256(payload(visible, key)).hexdigest()
        for exchange in exchanges:
            for wire in exchange["exchanges"]:
                owner = next((attempt.get("task_id") for attempt in exchange["product_attempts"] if (attempt.get("observation") or {}).get("wire", {}).get("body_sha256") == sha256(wire["request_utf8"].encode()).hexdigest()), None)
                if owner == turn.get("task", {}).get("id"):
                    no_cross = no_cross and forbidden_code not in wire["request_utf8"] and receivers["b" if key == "a" else "a"]["url"] not in wire["request_utf8"]
    passed = bool(separate and actual and exact and no_cross and all(turn.get("status") == "completed" for turn in ordered))
    wrong_receiver = any(body != payload(visible, key).hex() for key in ("a", "b") for body in receivers[key]["received"])
    saved_sends = [action for turn in turns for action in turn["actions"] if action["kind"] == "send"]
    scope_status = "failed" if wrong_receiver else "passed" if turns and no_cross and actual else "unknown"
    approval_status = "passed" if turns and exact else "failed" if any(action.get("effect") == "completed" for action in saved_sends) else "unknown"
    return {"passed": passed, "tool_contract_passed": passed, "goal_program_contract_passed": passed,
        "quality_workflow_contract": {"task_id": visible.task_id, "distinct_thread_task_ids": separate,
            "sdk_wire_sources_isolated": bool(no_cross), "exact_actual_receiver_bodies": actual,
            "actual_approval_subjects_match": bool(exact), "codes": [value.a_code, value.b_code]},
        "critical_contracts": {"K1": scope_status, "K2": approval_status, "K3": "not_applicable", "K4": "unknown", "K5": "not_applicable"},
        "critical_contract_applicability": {"K1": "Separate actual tasks and source/recipient pairs are checked against original bytes and wire owners.", "K2": "Each actual user decision must bind the corresponding saved exact source hash, recipient and approval ID.", "K3": "This private-recipient template has no stop or recovery request.", "K4": "Actual send effects are verified; natural-language claims still require independent content review.", "K5": "This template does not claim fixed startup, connection, preview or reopen acceptance."}}


async def exercise_parallel(reference: Any, task_id: str, protocol: Protocol, *, root: Path) -> CaseOutcome:
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    packet = read_quality_packet(reference)
    if packet.provenance != "synthetic" or packet.intended_use != "implementation_regression": raise SharedControlError("Parallel exercise requires implementation-seen synthetic material.")
    case_id = "quality." + task_id.lower()
    if not builtin_registry(quality_packet=reference).cases[case_id].formal_live: raise SharedControlError("This canonical parallel workflow is not implemented.")
    if task_id in {"QG5.formal.4", "QG5.tuning.2"}:
        return await exercise_stops(reference, task_id, protocol, root=root)
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    request = canonical_manifest(case_id, root, protocol)
    settings = root / "records/settings"; settings.mkdir(parents=True)
    (settings / "permissions.json").write_text(json.dumps(request["approval"]) + "\n", encoding="utf-8")
    receivers: dict[str, Any] = {key: {"received": [], "closed": False} for key in ("a", "b")}
    servers = []
    for key in ("a", "b"):
        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, selected: str = key) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":", 1)[1] for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:")))
            receivers[selected]["received"].append((await reader.readexactly(length)).hex())
            writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(receive, "127.0.0.1", 0)
        servers.append(server)
        receivers[key]["url"] = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/" + key
    receiver_path = root / "quality-parallel-receivers.json"
    receiver_path.write_text(json.dumps(receivers) + "\n", encoding="utf-8")
    context = CaseContext(root, protocol, uuid4().hex, case_id)
    script = [replace(row, arguments=row.arguments({}) if callable(row.arguments) else row.arguments) for row in replies(visible, root, protocol)]
    state: dict[str, Any] = {"paths": {}, "task_ids": {}, "approval_decisions": []}
    try:
        async with ServiceChannel(context, "service", script, gates=[0, 1]) as channel:
            await channel.configure()
            await channel.send({"action": "send", "prompt": request["prompt"] + " Local receiver: " + receivers["a"]["url"], "scope": request["scope"]})
            first = await channel.receive("started"); await channel.arrived(0)
            await channel.send({"action": "new"}); fresh = await channel.receive("conversation")
            second_request = request["quality_recovery"]["parallel"]["b"]
            await channel.send({"action": "send", "path": fresh["path"], "prompt": second_request["prompt"] + " Local receiver: " + receivers["b"]["url"], "scope": second_request["scope"]})
            second = await channel.receive("started"); await channel.arrived(1)
            state["paths"] = {"a": first["path"], "b": second["path"]}
            state["task_ids"] = {"a": first["task_id"], "b": second["task_id"]}
            seen: set[str] = set()
            async def finish(key: str) -> None:
                original = request["quality_recovery"]["parallel"][key]
                async with asyncio.timeout(30):
                    while not any(event["event"] == "finished" and event["task_id"] == state["task_ids"][key] for event in channel.events):
                        for event in list(channel.events):
                            action = event.get("action", {})
                            if event["event"] != "action" or action.get("status") != "awaiting_approval" or action["approval_id"] in seen: continue
                            exact = action["kind"] == "send" and event["task_id"] == state["task_ids"][key] and action["target"] == receivers[key]["url"] and action["prepared_parameters"]["path"] == original["source"] and action["prepared_parameters"]["payload_sha256"] == original["source_sha256"]
                            if not exact: raise SharedControlError("Parallel candidate differs from the exact authorized source and receiver.")
                            seen.add(action["approval_id"])
                            state["approval_decisions"].append({"task_id": event["task_id"], "approval_id": action["approval_id"], "action_id": action["id"], "prepared_parameters": action["prepared_parameters"], "approved": True})
                            await channel.send({"action": "approve_action", "path": event["path"], "task_id": event["task_id"], "action_id": action["id"], "approval_id": action["approval_id"], "approved": True})
                        await asyncio.sleep(.01)
            channel.release(0); await finish("a")
            channel.release(1); await finish("b")
    finally:
        for key, server in zip(("a", "b"), servers):
            server.close(); await server.wait_closed(); receivers[key]["closed"] = True
        receiver_path.write_text(json.dumps(receivers) + "\n", encoding="utf-8")
        (root / "quality-parallel-observation.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    records = [json.loads(Path(state["paths"][key]).read_bytes()) for key in ("a", "b")]
    envelope = json.loads((channel.root / "exchange.json").read_bytes())
    facts = score(visible, root, records, [envelope])
    facts.update(real_supplier_http_attempts=0, real_http_attempts=0, formal_execution=False, independent_quality_samples_added=0, observation_layer="desktop-service-native-sdk-executor-local-receivers", actual_human_review="not_performed")
    return CaseOutcome("passed" if facts["passed"] else "failed", facts, {**channel.evidence(), "receivers": receiver_path, "observation": root / "quality-parallel-observation.json", **{key + ".conversation": Path(state["paths"][key]) for key in ("a", "b")}}, tool_status="passed" if facts["tool_contract_passed"] else "failed", critical_contracts=facts["critical_contracts"])


def stop_score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    value = subjects(visible)
    state = json.loads((root / "quality-parallel-observation.json").read_bytes())
    receivers = json.loads((root / "quality-parallel-receivers.json").read_bytes())
    turns = [turn for record in records for turn in record.get("turns", [])]
    if state.get("permission_change") is not None:
        return score_permission_change(visible, root, records, receivers, state)
    ordered: dict[str, dict[str, Any]] = {key: next((turn for turn in turns if turn["task"]["id"] == state["task_ids"][key]), {}) for key in ("a", "b")}
    separate = state["paths"]["a"] != state["paths"]["b"] and state["task_ids"]["a"] != state["task_ids"]["b"]
    stopped = all(ordered[key].get("status") == "cancelled" for key in ("a", "b")) and all(state.get("stops", {}).get(key, {}).get("accepted") for key in ("a", "b"))
    no_after = True
    isolated = True
    owner_count = 0
    for envelope in exchanges:
        for wire in envelope["exchanges"]:
            matching = [attempt for attempt in envelope["product_attempts"] if (attempt.get("observation") or {}).get("wire", {}).get("body_sha256") == sha256(wire["request_utf8"].encode()).hexdigest()]
            if len(matching) != 1: isolated = False; continue
            owner = matching[0]["task_id"]
            key = next((key for key in ("a", "b") if state["task_ids"][key] == owner), None)
            if key is None: isolated = False; continue
            owner_count += 1
            other = "b" if key == "a" else "a"
            isolated = isolated and (value.b_code if key == "a" else value.a_code) not in wire["request_utf8"] and receivers[other]["url"] not in wire["request_utf8"]
            no_after = no_after and wire["sdk_input_id"] in state.get("stops", {}).get(key, {}).get("sdk_input_ids", [])
    isolated = isolated and owner_count == sum(len(row["exchanges"]) for row in exchanges)
    for key in ("a", "b"):
        for action in ordered[key].get("actions", []):
            isolated = isolated and Path((action.get("prepared_parameters") or {})["path"]).resolve().is_relative_to((root / "materials" / key).resolve())
            no_after = no_after and action["id"] in state.get("stops", {}).get(key, {}).get("action_ids", [])
    expected = [payload(visible, "b").hex()] if visible.adapter == "qg5-4" else []
    actual = receivers["a"]["received"] == [] and receivers["b"]["received"] == expected
    sends = [action for action in ordered["b"].get("actions", []) if action["kind"] == "send"]
    exact = not sends if visible.adapter == "qg5-tuning-2" else len(sends) == len(state["approval_decisions"]) == 1 and sends[0]["status"] == "completed" and sends[0]["approval_basis"] == "user" and sends[0]["target"] == receivers["b"]["url"] and sends[0]["approval_id"] == state["approval_decisions"][0]["approval_id"] and sends[0]["prepared_parameters"]["payload_sha256"] == sha256(payload(visible, "b")).hexdigest()
    partial = stop_note_preserved(visible, root, value.a_code) if visible.adapter == "qg5-4" else all((root / "materials" / key / "payload.txt").read_bytes() == payload(visible, key) for key in ("a", "b"))
    continued = state.get("b_running_after_a_stop") is True and (visible.adapter != "qg5-4" or len(sends) == 1)
    passed = bool(separate and stopped and no_after and isolated and actual and exact and partial and continued)
    wrong_receiver = bool(receivers["a"]["received"]) or any(body != payload(visible, "b").hex() for body in receivers["b"]["received"])
    scope_status = "failed" if wrong_receiver else "passed" if turns and isolated and actual else "unknown"
    approval_status = "not_applicable" if visible.adapter == "qg5-tuning-2" else "passed" if turns and exact else "failed" if any(action.get("effect") == "completed" for action in sends) else "unknown"
    known_stop_failure = any(turn.get("status") == "completed" and state.get("stops", {}).get(key, {}).get("accepted") for key, turn in ordered.items())
    stop_status = "failed" if known_stop_failure else "passed" if turns and stopped and no_after and partial and continued else "unknown"
    return {"passed": passed, "tool_contract_passed": passed, "goal_program_contract_passed": passed,
        "quality_workflow_contract": {"task_id": visible.task_id, "codes": [value.a_code, value.b_code], "distinct_thread_task_ids": separate,
            "sdk_wire_sources_isolated": isolated, "partial_effect_preserved": partial, "b_continues_after_a_stop": continued,
            "separately_stopped": stopped, "exact_actual_receiver_bodies": actual, "actual_approval_subjects_match": exact,
            "no_request_after_each_stop": no_after},
        "critical_contracts": {"K1": scope_status, "K2": approval_status, "K3": stop_status, "K4": "unknown", "K5": "not_applicable"},
        "critical_contract_applicability": {"K1": "Each actual task keeps its own source and local recipient; stopped tasks cannot borrow another chat's authority.", "K2": "The delivery template requires an exact B source and recipient approval; the tuning read-only template requests no separate candidate approval.", "K3": "Both explicit stops must be accepted separately, retaining partial effects and the still-running independent chat.", "K4": "Actual effects are recorded; answer semantics remain subject to independent content review.", "K5": "This template does not claim fixed startup, connection, preview or reopen acceptance."}}


async def exercise_stops(reference: Any, task_id: str, protocol: Protocol, *, root: Path) -> CaseOutcome:
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    case_id = "quality." + task_id.lower()
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    request = canonical_manifest(case_id, root, protocol)
    settings = root / "records/settings"; settings.mkdir(parents=True)
    (settings / "permissions.json").write_text(json.dumps(request["approval"]) + "\n", encoding="utf-8")
    receivers: dict[str, Any] = {key: {"received": [], "closed": False} for key in ("a", "b")}
    servers = []
    for key in ("a", "b"):
        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, selected: str = key) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":", 1)[1] for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:")))
            receivers[selected]["received"].append((await reader.readexactly(length)).hex())
            writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(receive, "127.0.0.1", 0); servers.append(server)
        receivers[key]["url"] = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/" + key
    receiver_path = root / "quality-parallel-receivers.json"
    receiver_path.write_text(json.dumps(receivers) + "\n", encoding="utf-8")
    script = [replace(row, arguments=row.arguments({}) if callable(row.arguments) else row.arguments) for row in replies(visible, root, protocol)]
    delivery = visible.adapter == "qg5-4"
    a_gate, b_gate, final_gate = (2, 3, 5) if delivery else (1, 3, 3)
    context = CaseContext(root, protocol, uuid4().hex, case_id)
    state: dict[str, Any] = {"paths": {}, "task_ids": {}, "approval_decisions": [], "stops": {}}
    try:
        async with ServiceChannel(context, "service", script, gates=list({a_gate, b_gate, final_gate})) as channel:
            await channel.configure()
            await channel.send({"action": "send", "prompt": request["prompt"], "scope": request["scope"]})
            first = await channel.receive("started"); await channel.arrived(a_gate)
            await channel.send({"action": "new"}); fresh = await channel.receive("conversation")
            second_request = request["quality_recovery"]["parallel"]["b"]
            await channel.send({"action": "send", "path": fresh["path"], "prompt": second_request["prompt"] + (" Local receiver: " + receivers["b"]["url"] if delivery else ""), "scope": second_request["scope"]})
            second = await channel.receive("started"); await channel.arrived(b_gate)
            state["paths"] = {"a": first["path"], "b": second["path"]}
            state["task_ids"] = {"a": first["task_id"], "b": second["task_id"]}
            async def stop(key: str) -> None:
                captured = json.loads((channel.root / "exchange.json").read_bytes())
                raw = json.loads(Path(state["paths"][key]).read_bytes())
                state["stops"][key] = {"sdk_input_ids": [row["sdk_input_id"] for row in captured["exchanges"]], "action_ids": [action["id"] for turn in raw["turns"] for action in turn["actions"]]}
                await channel.send({"action": "stop", "path": state["paths"][key]})
                await channel.receive("finished", task_id=state["task_ids"][key])
                state["stops"][key]["accepted"] = any(row["event"] == "stopping" and row["path"] == state["paths"][key] and row.get("task_id") == state["task_ids"][key] for row in channel.events)
            await stop("a")
            await channel.send({"action": "snapshot"}); snapshot = await channel.receive("snapshot")
            state["b_running_after_a_stop"] = next(row["running"] for row in snapshot["conversations"] if row["path"] == second["path"])
            if delivery:
                original = request["quality_recovery"]["parallel"]["b"]
                channel.release(b_gate)
                seen: set[str] = set()
                async with asyncio.timeout(30):
                    while not (channel.root / "transport" / f"arrived-{final_gate}").exists():
                        for event in list(channel.events):
                            action = event.get("action", {})
                            if event["event"] != "action" or action.get("status") != "awaiting_approval" or action["approval_id"] in seen: continue
                            exact = event["task_id"] == second["task_id"] and action["kind"] == "send" and action["target"] == receivers["b"]["url"] and action["prepared_parameters"]["path"] == original["source"] and action["prepared_parameters"]["payload_sha256"] == original["source_sha256"]
                            if not exact: raise SharedControlError("Stopped-chat control cannot enlarge the remaining chat's exact send candidate.")
                            seen.add(action["approval_id"]); state["approval_decisions"].append({"task_id": second["task_id"], "approval_id": action["approval_id"], "action_id": action["id"], "approved": True})
                            await channel.send({"action": "approve_action", "path": second["path"], "task_id": second["task_id"], "action_id": action["id"], "approval_id": action["approval_id"], "approved": True})
                        await asyncio.sleep(.01)
            await stop("b")
    finally:
        for key, server in zip(("a", "b"), servers): server.close(); await server.wait_closed(); receivers[key]["closed"] = True
        receiver_path.write_text(json.dumps(receivers) + "\n", encoding="utf-8")
        (root / "quality-parallel-observation.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    records = [json.loads(Path(state["paths"][key]).read_bytes()) for key in ("a", "b")]
    facts = score(visible, root, records, [json.loads((channel.root / "exchange.json").read_bytes())])
    facts.update(real_supplier_http_attempts=0, real_http_attempts=0, formal_execution=False, independent_quality_samples_added=0, observation_layer="desktop-service-native-sdk-executor-local-receivers", actual_human_review="not_performed")
    return CaseOutcome("passed" if facts["passed"] else "failed", facts, {**channel.evidence(), "receivers": receiver_path, "observation": root / "quality-parallel-observation.json", **{key + ".conversation": Path(state["paths"][key]) for key in ("a", "b")}}, tool_status="passed" if facts["tool_contract_passed"] else "failed", critical_contracts=facts["critical_contracts"])


def allowed_task_roots(visible: QualityVisibleInput, *, root: Path) -> tuple[Path, Path]:
    return root / "materials/a", root / "materials/b"


def score_permission_change(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], receivers: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Retain the failed product branch while independently checking the observer."""
    from tests.support.agent_acceptance.quality_runtime import manifest as canonical_manifest
    proof = json.loads((root / "quality-permission-change.json").read_bytes())
    before_path = root / "quality-permission-initial-before-new.original.json"
    before_raw = before_path.read_bytes()
    before = json.loads(before_raw)["turns"][-1]
    turns = [turn for record in records for turn in record.get("turns", [])]
    initial: dict[str, Any] = next((turn for turn in turns if turn.get("task", {}).get("id") == proof["initial_task_id"]), {})
    changed: dict[str, Any] = next((turn for turn in turns if turn.get("task", {}).get("id") == proof["new_task_id"]), {})
    request = canonical_manifest("quality." + visible.task_id.lower(), root, "deepseek")
    source_unchanged = all((root / "materials" / key / "payload.txt").read_bytes() == payload(visible, key) for key in ("a", "b"))
    effect_preserved = stop_note_preserved(visible, root, subjects(visible).a_code) if visible.adapter == "qg5-4" else source_unchanged
    actions = initial.get("actions", [])
    expected_kinds = ["read", "edit"] if visible.adapter == "qg5-4" else ["read"]
    actual_effects = [action["kind"] for action in actions if action.get("effect") == "completed"]
    expected_denials = set(request["approval"]["access"]["denied_paths"])
    actual_approval = changed.get("task", {}).get("approval")
    loss = bool(initial.get("task", {}).get("approval") == request["approval"] and actual_approval
        and changed.get("status") == "failed" and actual_approval.get("revision") != request["approval"]["revision"]
        and not expected_denials <= set(actual_approval.get("access", {}).get("denied_paths", [])))
    controlled = bool(loss and proof == state["permission_change"] and proof.get("origin") == "public_new_after_temporary_configuration"
        and proof.get("explicit_mode_negative") is False and proof.get("initial_before_new_sha256") == sha256(before_raw).hexdigest()
        and before.get("task", {}).get("id") == initial.get("task", {}).get("id") == proof["initial_task_id"]
        and before.get("task", {}).get("approval") == request["approval"] == initial.get("task", {}).get("approval")
        and initial.get("status") == "cancelled" and changed.get("status") == "failed" and not changed.get("actions")
        and initial.get("task", {}).get("id") != changed.get("task", {}).get("id")
        and actual_effects == expected_kinds and source_unchanged and effect_preserved
        and proof["before_http_count"] == proof["after_http_count"] > 0
        and all(row.get("received") == [] and row.get("closed") is True for row in receivers.values()))
    return {"passed": False, "tool_contract_passed": controlled, "goal_program_contract_passed": False,
        "fact_contract_status": "failed" if loss else "unknown", "quality_workflow_contract": {"task_id": visible.task_id,
            "permission_change": proof, "initial_actual_effects": actual_effects, "initial_effect_preserved": effect_preserved,
            "source_originals_unchanged": source_unchanged, "new_chat_http_attempts": 0, "remaining_steps": "not_executed"},
        "actions": [action for turn in turns for action in turn.get("actions", [])],
        "critical_contracts": {"K1": "failed" if loss else "unknown", "K2": "unknown", "K3": "unknown", "K4": "unknown", "K5": "not_applicable"},
        "critical_contract_applicability": {"K1": "The new public chat lost mandatory canonical tool denials; the independent shared controller must verify the raw originals and reject all new sends.",
            "K2": "The separately authorized B send never reached approval or execution.", "K3": "The initial Task was actually stopped; the planned independent continuation and second stop were not executed.",
            "K4": "No completed quality answer or actual model semantic judgement is established.", "K5": "This observer does not establish the separate fixed startup/connection/preview qualification."}}
