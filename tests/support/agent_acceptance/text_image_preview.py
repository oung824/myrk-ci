"""Actual public text/diff/image preview facts; setup alone uses terminal model replay."""

import asyncio
from base64 import b64decode
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
from typing import Any
from uuid import uuid4

from tests.support.agent_acceptance.core import AcceptanceCase, AcceptancePlan, CaseContext, CaseOutcome, CaseRegistry
from tests.support.agent_acceptance.formal import bindings, build, environment
from tests.support.agent_acceptance.preparation import SOURCE, bound_files

MATERIALS = SOURCE / "tests/acceptance/materials/text-image-preview"
CASE = "preview.text-diff-image"
CLASSIFICATION = "text-image-contracts-v2"
LEGACY_ASSERTION_DRIVER_SHA256 = "a6c03b40c4528eaf8306eba719f0d2f1d61fe2a5f64943b3a7e6b131958f2546"
SOURCE_LABELS = ("delivery setup completed", "text read", "comparison read", "API snapshot close",
    "all 180 Unicode segments read", "fixed snapshot does not change original", "explicit refresh read only",
    "after actual unreadable lock restored", "after actual unreadable acl restored", "missing file restored after factual state",
    "inline image and exact bytes", "image zoom", "image close preserves focus and chat reading position",
    "same-name B identity is separate", "full Main restart opens each original without new work")


def _document(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return default


def _reference(root: Path, hashes: dict[str, str], filename: str) -> bytes | None:
    try:
        data = (root / "expected" / filename).read_bytes()
    except OSError:
        return None
    return data if sha256(data).hexdigest() == hashes.get(filename) else None


def _status(checks: dict[str, bool | None]) -> str:
    if any(value is False for value in checks.values()):
        return "failed"
    return "passed" if checks and all(value is True for value in checks.values()) else "unknown"


def _equals(actual: Any, expected: Any) -> bool | None:
    return None if actual is None or expected is None else bool(actual == expected)



def register(registry: CaseRegistry) -> None:
    registry.register(AcceptanceCase(CASE, "text-diff-image-preview", ("deepseek",), preview,
        owner_ticket="18", bindings=(*bindings(), Path(__file__).with_name("text_image_preview_service.py"),
            SOURCE / "tests/support/agent_acceptance/desktop/text-image-preview.electron.mjs", *MATERIALS.iterdir()),
        applicability="One local public Main chain for L07-L13; setup SDK/terminal Mock is distinct from zero-request preview operations."))


async def preview(context: CaseContext) -> CaseOutcome:
    from tests.support.agent_acceptance import builtin_registry

    expected = context.root / "expected"
    shutil.copytree(MATERIALS, expected)
    for name in ("A", "B"):
        folder = context.root / name
        (folder / ".git").mkdir(parents=True)
        (folder / "report.txt").write_bytes((expected / "before.txt").read_bytes())
    build_root = Path(os.environ.get("MYRK_CHECK_RUN_DIRECTORY", str(context.root))) / ("pv-" + uuid4().hex[:8])
    compiled = await asyncio.to_thread(build, build_root)
    sources = bound_files(AcceptancePlan(cases=[CASE]), builtin_registry())
    binding = {"run_id":context.run_id, "case_id":CASE, "source_root":str(SOURCE), "compiled_root":str(build_root / "project/desktop/out"),
        "sources":sources, "compiled_sources":compiled, "fixture_bytes":{path.name:sha256(path.read_bytes()).hexdigest() for path in expected.iterdir()}}
    (context.root / "execution-bindings.json").write_text(json.dumps(binding,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    proof = context.root / "source-proof/driver.mjs"
    proof.parent.mkdir()
    driver_name = "tests/support/agent_acceptance/desktop/text-image-preview.electron.mjs"
    driver_bytes = (SOURCE / driver_name).read_bytes()
    if sha256(driver_bytes).hexdigest() != sources[driver_name]:
        raise ValueError("Preview driver changed after its pre-run source binding.")
    proof.write_bytes(driver_bytes)
    request = {"root":str(context.root), "driver":str(SOURCE / "tests/support/agent_acceptance/desktop/text-image-preview.electron.mjs"),
        "phases":[{"name":"work"},{"name":"reopen"}], "environment":{"MYRK_TEST_RUN_DIRECTORY":str(build_root),
        "MYRK_USER_DATA_DIRECTORY":str(context.root / "profile"),"MYRK_DATA_DIRECTORY":str(context.root / "records"),
        "MYRK_SERVICE_MODULE":"tests.support.agent_acceptance.text_image_preview_service", "MYRK_PREVIEW_ROOT":str(context.root),"MYRK_PREVIEW_RUN_ID":context.run_id}}
    (context.root / "desktop-phases.json").write_text(json.dumps(request,indent=2),encoding="utf-8")
    process = await asyncio.create_subprocess_exec("node",str(SOURCE / "tests/support/agent_acceptance/desktop/restart.mjs"),str(context.root / "desktop-phases.json"),
        cwd=SOURCE,env=environment(context.root / "environment"),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await process.communicate()
    (context.root / "runner.stdout").write_bytes(stdout)
    (context.root / "runner.stderr").write_bytes(stderr)
    processes = _document(context.root / "processes.json", {})
    for name in ("build.stdout","build.stderr"):
        (context.root / name).write_bytes((build_root / name).read_bytes())
    if processes.get("complete") is True:
        shutil.rmtree(build_root)
        for leaf in ("environment","profile"):
            shutil.rmtree(context.root / leaf,ignore_errors=False)
    return _outcome(context.root, process.returncode)


def _outcome(root: Path, runner_exit: int | None) -> CaseOutcome:
    check: dict[str, Any]
    event: dict[str, Any]
    observation: dict[str, Any]
    finished: dict[str, Any]
    action: dict[str, Any]
    artifact: dict[str, Any]
    change: dict[str, Any]
    history: dict[str, Any]
    groups: dict[str, dict[str, Any]]
    expected = root / "expected"
    processes = _document(root / "processes.json", {})
    states = [_document(root / phase / "observation.json", {}) for phase in ("work","reopen") if (root / phase / "observation.json").is_file()]
    captured = runner_exit == 0 and processes.get("complete") is True and len(states)==2 and all(row.get("passed") is True for row in states)
    work: dict[str, Any] = next((row for row in states if row.get("phase")=="work"),{})
    reopened: dict[str, Any] = next((row for row in states if row.get("phase")=="reopen"),{})
    facts: dict[str, Any] = {"covered":list(_document(expected / "criteria.json", {}).get("local_ids", [])) if captured else [],
        "states":states,"processes_complete":processes.get("complete"),"normal_exit_phases":processes.get("normal_exit_phases"),
        "setup_http_attempts":work.get("setup_http_attempts"),"preview_http_attempts":work["preview_http_attempts"]+reopened["preview_http_attempts"] if work.get("preview_http_attempts") is not None and reopened.get("preview_http_attempts") is not None else None,
        "real_http_attempts":0,"normal_profile_accesses":0,"current_real_service":"not_measured","human_review":"not_performed",
        "reading_contract":"same Main image close/Escape focus and chat scroll; no cross-process zoom persistence claim",
        "critical_applicability":{}}
    evidence={path.relative_to(root).as_posix():path for path in root.rglob("*") if path.is_file()}
    outcome=CaseOutcome("inconclusive",facts,evidence,tool_status="passed" if captured else "failed")
    outcome.critical_contracts={name:"unknown" for name in ("K1","K2","K3","K4","K5")}
    chats = json.loads((root / "saved-chats.json").read_bytes()) if (root / "saved-chats.json").is_file() else []
    events = json.loads((root / "work/public-events.json").read_bytes()) if (root / "work/public-events.json").is_file() else None
    settings = [chat.get("turn", {}).get("task", {}).get("approval", {}) for chat in chats]
    settings.extend(chat.get("turn", {}).get("approval_settings", {}) for chat in chats)
    actions = [action for chat in chats for action in chat.get("turn", {}).get("actions", [])]
    settings.extend(action.get("approval_settings", {}) for action in actions)
    no_review = bool(chats and actions and events is not None) and all(
        item.get("mode") == "full_access" and item.get("condition") == "never" and item.get("reviewer") == "none"
        and item.get("review_mandatory") is False for item in settings) and all(
        "approval_id" in action and action["approval_id"] is None and "review" in action and action["review"] is None
        and action.get("approval_basis") == "task_scope" for action in actions) and not any(
        event.get("event") in {"approval", "review"} for event in (events or []))
    checks: dict[str, dict[str, bool | None]] = {"K1": {}}
    frozen = json.loads((root / "execution-bindings.json").read_bytes()) if (root / "execution-bindings.json").is_file() else {}
    hashes = frozen.get("fixture_bytes", {})
    for filename in ("after.txt", "changed.txt", "other.txt", "long.txt", "picture.png"):
        path = root / "expected" / filename
        checks["K1"]["independent_original:" + filename] = True if _reference(root, hashes, filename) is not None else None
    snapshots = [check for state in states for check in state.get("source_checks", [])]
    for label in SOURCE_LABELS:
        check = next((check for check in snapshots if check.get("label") == label), {})
        rows = check.get("rows", [])
        checks["K1"]["source_rows:" + label] = True if len(rows) == 6 else None
        for row in rows:
            filename = ("changed.txt" if label in {"fixed snapshot does not change original", "explicit refresh read only"} else "after.txt") if row.get("chat") == "A" and row.get("name") == "report.txt" else "other.txt" if row.get("name") == "report.txt" else row.get("name")
            checks["K1"][label + ":" + str(row.get("chat")) + "/" + str(row.get("name"))] = _equals(row.get("sha256"), hashes.get(filename))
    refusal = work.get("cross_chat_refused", {})
    checks["K1"]["cross_chat_refused"] = False if "value" in refusal else True if "所属会话" in refusal.get("error", "") else None
    checks["K1"]["two_chat_originals"] = True if len(chats) == 2 else None
    checks["K1"]["action_originals"] = True if chats and all(chat.get("turn", {}).get("actions") for chat in chats) else None
    for chat in chats:
        turn = chat.get("turn", {})
        task = turn.get("task", {})
        for action in turn.get("actions", []):
            key = str(action.get("id"))
            checks["K1"]["action_task:" + key] = _equals(action.get("task_id"), task.get("id"))
            checks["K1"]["authorization_revision:" + key] = _equals(action.get("authorization_revision"), action.get("approval_settings", {}).get("revision"))
            for change in action.get("file_changes", []):
                output_path, folder = change.get("path"), task.get("scope", {}).get("root")
                checks["K1"]["authorized_output:" + str(change.get("id"))] = PureWindowsPath(output_path).is_relative_to(PureWindowsPath(folder)) if output_path and folder else None
    outcome.critical_contracts["K1"] = _status(checks["K1"])
    facts["contract_measurements"] = checks
    facts["classification_version"] = CLASSIFICATION
    facts["critical_applicability"]["K1"] = "Effective action task/revision and local output scope, actual per-operation source hashes, and same-name cross-chat refusal are checked independently; missing originals stay unknown."
    checks["K2"] = {}
    for action in actions:
        executed = action.get("status") == "completed" and action.get("effect") == "completed"
        review = action.get("review")
        mandatory = action.get("approval_settings", {}).get("review_mandatory")
        if executed and mandatory is True:
            checks["K2"]["mandatory_review:" + str(action.get("id"))] = False if "review" in action and review is None else None
        if executed and review and review.get("status") in {"denied", "cancelled", "failed", "timeout", "context_changed"}:
            checks["K2"]["review_refusal:" + str(action.get("id"))] = False
        if executed and action.get("approval_basis") == "user":
            checks["K2"]["actual_user_approval:" + str(action.get("id"))] = False if "approval_id" in action and action["approval_id"] is None else None
    checks["K2"]["conditional_branch_evidence"] = True if no_review else None
    outcome.critical_contracts["K2"] = "failed" if _status(checks["K2"]) == "failed" else "not_applicable" if no_review else "unknown"
    reopen_events = json.loads((root / "reopen/public-events.json").read_bytes()) if (root / "reopen/public-events.json").is_file() else None
    reopen_exchange = json.loads((root / "reopen/exchange.json").read_bytes()) if (root / "reopen/exchange.json").is_file() else {}
    checks["K3"] = {"two_chat_originals": True if len(chats) == 2 else None, "reopen_terminal_http_zero": _equals(len(reopen_exchange["exchanges"]) if "exchanges" in reopen_exchange else None, 0),
        "reopen_no_started_action": not any(event.get("event") in {"started", "action"} for event in reopen_events) if reopen_events is not None else None,
        "fresh_Main_identity": work["pid"] != reopened["pid"] if work.get("pid") and reopened.get("pid") else None}
    for chat in chats:
        event = next((event for event in (reopen_events or []) if event.get("event") == "conversation" and event.get("path") == chat.get("path")), {})
        turns = event.get("turns", [])
        old = chat.get("turn", {})
        current = turns[-1] if turns else {}
        for field in ("id", "scope", "approval", "requests", "total_requests"):
            checks["K3"][str(chat.get("name")) + ":task:" + field] = _equals(current.get("task", {}).get(field), old.get("task", {}).get(field))
        for field in ("actions", "artifacts"):
            checks["K3"][str(chat.get("name")) + ":" + field] = _equals([item.get("id") for item in current[field]] if field in current else None,
                [item.get("id") for item in old[field]] if field in old else None)
    for phase in ("work", "reopen"):
        observation = next((item for item in processes.get("observations", []) if item.get("phase") == phase), {})
        checks["K3"][phase + ":normal_exit"] = _equals(observation.get("rootExit"), 0)
    outcome.critical_contracts["K3"] = _status(checks["K3"])
    facts["critical_applicability"]["K3"] = "Actual complete Main exits and saved-chat reopen must keep task identity, scope, approval, usage and completed action/artifact identities, with zero new terminal HTTP/start/action events. No adopted stop, in-flight cancellation or unknown-effect task resume was exercised."
    checks["K4"] = {"saved_chat_originals": True if chats else None, "actual_public_preview_responses": True if work.get("previews") else None}
    for chat in chats:
        turn = chat.get("turn", {})
        finished = next((event for event in (events or []) if event.get("event") == "finished" and event.get("path") == chat.get("path")), {})
        record_path = root / "records" / PureWindowsPath(chat.get("path", "")).name
        record = json.loads(record_path.read_bytes()) if record_path.is_file() else {}
        actual_turn = record.get("turns", [{}])[-1] if record.get("turns") else {}
        checks["K4"][str(chat.get("name")) + ":saved_claim"] = _equals(actual_turn.get("artifacts"), finished.get("turn", {}).get("artifacts")) if finished.get("saved") is True else True if finished.get("saved") is False else None
        for artifact in chat.get("artifacts", []):
            key = str(artifact.get("id"))
            action = next((action for action in turn.get("actions", []) if action.get("id") == artifact.get("source_action_id")), {})
            change = next((change for change in action.get("file_changes", []) if change.get("id") == artifact.get("file_change_id")), {})
            checks["K4"][key + ":source_record"] = _equals(artifact.get("source_record_path"), chat.get("path"))
            checks["K4"][key + ":source_version"] = _equals(artifact.get("delivered_sha256"), change.get("after_sha256"))
            local = root / str(chat.get("name")) / str(artifact.get("name"))
            checks["K4"][key + ":actual_delivered_bytes"] = _equals(sha256(local.read_bytes()).hexdigest() if local.is_file() else None, artifact.get("delivered_sha256"))
    for index, response in enumerate(work.get("previews", [])):
        key = "preview:" + str(index)
        artifact = next((artifact for chat in chats if chat.get("path") == response.get("conversationId") for artifact in chat.get("artifacts", []) if artifact.get("id") == response.get("artifactId")), {})
        checks["K4"][key + ":delivered_identity"] = _equals(response.get("name"), artifact.get("name"))
        content = response.get("content", {})
        if response.get("status") == "ready":
            checksum = sha256(content["text"].encode("utf-8")).hexdigest() if content.get("kind") == "text" and "text" in content else sha256(b64decode(content["base64"])).hexdigest() if content.get("kind") == "image" and "base64" in content else None
            checks["K4"][key + ":ready_bytes"] = _equals(checksum, response.get("sha256"))
            checks["K4"][key + ":version_state"] = _equals(response.get("deliveryState"), None if response.get("sha256") is None or artifact.get("delivered_sha256") is None else "unchanged" if response["sha256"] == artifact["delivered_sha256"] else "changed")
        elif response.get("status") == "unreadable":
            messages = [work.get(name, {}).get("message") for name in ("unreadable_cause", "permission_cause")]
            checks["K4"][key + ":read_failure"] = response.get("message") in messages if response.get("message") and any(messages) else None
        elif response.get("status") == "missing":
            checks["K4"][key + ":missing_notice"] = "丢失" in response["message"] if response.get("message") else None
    display = work.get("display_measurements", {})
    for key, filename in (("report_fulltext", "after.txt"), ("long_fulltext", "long.txt"), ("fixed_snapshot", "after.txt"), ("refresh_fulltext", "changed.txt")):
        reference = _reference(root, hashes, filename)
        checks["K4"]["actual_DOM:" + key] = _equals(display.get(key), reference.decode("utf-8") if reference is not None else None)
    proof_path = root / "source-proof/driver.mjs"
    driver_bytes = proof_path.read_bytes() if proof_path.is_file() else b""
    driver_source = driver_bytes.decode("utf-8")
    driver_name = "tests/support/agent_acceptance/desktop/text-image-preview.electron.mjs"
    source_bound = bool(driver_bytes) and sha256(driver_bytes).hexdigest() == frozen.get("sources", {}).get(driver_name)
    reviewed_control_flow = sha256(driver_bytes).hexdigest() == LEGACY_ASSERTION_DRIVER_SHA256
    expected_driver = PureWindowsPath(frozen["source_root"]) / driver_name if frozen.get("source_root") else None
    launched = any(PureWindowsPath(argument) == expected_driver for phase in processes.get("observations", [])
        if phase.get("phase") == "work" for process in phase.get("processes", []) if process.get("pid") == work.get("pid")
        for argument in process.get("direct_spawn", {}).get("argv", [])) if expected_driver else False
    transport = _document(root / "work/exchange.json", {})
    run_bound = frozen.get("run_id") is not None and frozen.get("run_id") == transport.get("run_id") and frozen.get("case_id") == transport.get("case_id") == CASE
    program_evidence = {}
    program_rows = (("report_fulltext", "after.txt", "await click('查看会话成果')", "text read"),
        ("long_fulltext", "long.txt", "await select(long)", "all 180 Unicode segments read"),
        ("fixed_snapshot", "after.txt", "await writeFile(report.path,await readFile(join(expected,'changed.txt')))", "fixed snapshot does not change original"),
        ("refresh_fulltext", "changed.txt", "await click('刷新预览');await until(()=>execute('document.querySelector(\"[aria-label=成果全文]\")?.textContent.includes(\"9163\")')", "explicit refresh read only"))
    for key, filename, start_text, marker in program_rows:
        start = driver_source.find(start_text)
        end = driver_source.find("await checkSources('" + marker + "'", start) if start >= 0 else -1
        assertion = "assert.equal(await execute('document.querySelector(\"[aria-label=成果全文]\").textContent'),await text('" + filename + "'))"
        exact_assertion = start >= 0 and end > start and assertion in driver_source[start:end]
        gold = root / "expected" / filename
        gold_bound = _reference(root, hashes, filename) is not None
        marker_status = _status({name:value for name,value in checks["K1"].items() if name == "source_rows:" + marker or name.startswith(marker + ":")})
        verified = source_bound and reviewed_control_flow and launched and run_bound and exact_assertion and gold_bound and marker_status == "passed"
        if key == "long_fulltext":
            verified = verified and work.get("long_segments") == 180 and "result.long_segments=180" in driver_source[start:end]
        program_evidence[key] = {"source_sha256":sha256(driver_bytes).hexdigest() if driver_bytes else None,
            "source_bound":source_bound,"reviewed_control_flow":reviewed_control_flow,"actual_launch_bound":launched,"run_id_bound":run_bound,"exact_assertion_before_marker":exact_assertion,
            "gold_sha256":hashes.get(filename),"gold_bound":gold_bound,"marker":marker,"marker_measurements":marker_status,
            "verified":verified,"separate_DOM_bytes_retained":key in display}
        if key not in display:
            checks["K4"]["actual_DOM:" + key] = True if verified else None
    facts["program_assertion_evidence"] = program_evidence
    outcome.critical_contracts["K4"] = _status(checks["K4"])
    facts["critical_applicability"]["K4"] = "Published save/delivery/version/ready/error claims must match actual saved records, delivered bytes and public preview responses. DOM facts use separately retained bytes, or their own exact hash-bound driver assertion, actual launch/run/input linkage and post-assertion measurement marker. An overall passed flag supplies neither proof."
    criteria_bytes = _reference(root, hashes, "criteria.json")
    criteria = json.loads(criteria_bytes) if criteria_bytes is not None else {}
    image = criteria.get("image", {})
    reading = work.get("image_reading", {})
    before_top, after_top = reading.get("beforeTop"), reading.get("afterTop")
    work_exchange = json.loads((root / "work/exchange.json").read_bytes()) if (root / "work/exchange.json").is_file() else {}
    readonly_status = _status({key:value for key,value in checks["K1"].items() if key.startswith("independent_original:") or key.startswith("source_rows:") or ":A/" in key or ":B/" in key})
    checks["K5"] = {"read_only_originals": True if readonly_status == "passed" else False if readonly_status == "failed" else None,
        "image_dimensions": _equals([work.get("image_pixels", {}).get("width"), work.get("image_pixels", {}).get("height")] if work.get("image_pixels") else None, [image.get("width"), image.get("height")] if image else None),
        "image_pixels": _equals(work.get("image_pixels", {}).get("samples"), [row[2] for row in image["samples"]] if image.get("samples") else None),
        "image_zoom": _equals(reading.get("zoom"), "125%"), "image_focus": _equals(reading.get("focus"), "放大图片 picture.png"),
        "image_reading_position": abs(after_top-before_top) <= 1 if before_top is not None and after_top is not None else None,
        "preview_zero_model_calls": _equals(len(work_exchange["exchanges"]) if "exchanges" in work_exchange else None, work.get("setup_http_attempts")),
        "measured_preview_zero": _equals(work.get("preview_http_attempts"), 0), "chat_reading_isolated": work.get("chat_reading_isolated"),
        "saved_reopen_no_replay": True if _status(checks["K3"]) == "passed" else False if _status(checks["K3"]) == "failed" else None}
    for key in ("long_fulltext", "fixed_snapshot", "refresh_fulltext"):
        checks["K5"]["required_DOM:" + key] = checks["K4"]["actual_DOM:" + key]
    history = next((response.get("content", {}).get("history", {}) for response in work.get("previews", []) if response.get("content", {}).get("history", {}).get("status") == "available"), {})
    for key, filename in (("beforeText", "before.txt"), ("afterText", "after.txt")):
        reference = _reference(root, hashes, filename)
        checks["K5"]["independent_diff:" + key] = _equals(history.get(key), reference.decode("utf-8") if reference is not None else None)
    for kind in ("lock", "acl"):
        cause = work.get("unreadable_cause" if kind == "lock" else "permission_cause", {})
        checks["K5"][kind + ":real_denial"] = any(code in cause["message"] for code in (("EBUSY",) if kind == "lock" else ("EPERM", "EACCES"))) if cause.get("message") else None
    restore = work.get("acl_restore", {})
    checks["K5"]["acl_access_restored"] = restore.get("owner_group_access_entries_and_protection_equal")
    checks["K5"]["acl_raw_recovery"] = _equals(restore.get("original_sddl", "").replace("D:AI", "D:") or None, restore.get("restored_sddl", "").replace("D:AI", "D:") or None)
    groups = {phase:next((row for row in processes.get("observations", []) if row.get("phase") == phase), {}) for phase in ("work", "reopen")}
    groups.update({kind:work.get(kind + "_process", {}) for kind in ("lock", "acl")})
    for name, group in groups.items():
        rows = group.get("processes", [])
        checks["K5"][name + ":process_observations"] = True if rows else None
        for row in rows:
            key = name + ":" + str(row.get("pid"))
            checks["K5"][key + ":exited"] = True if row.get("state") == "exited" else False if row.get("state") == "running" else None
            checks["K5"][key + ":owned_identity"] = _equals(row.get("identity"), row.get("expected")) if row.get("ownership") == "owned" else None
    for chat in chats:
        finished = next((event for event in (events or []) if event.get("event") == "finished" and event.get("path") == chat.get("path")), {})
        checks["K5"][str(chat.get("name")) + ":fixed_save_chain"] = finished.get("saved")
    outcome.critical_contracts["K5"] = _status(checks["K5"])
    facts["critical_applicability"]["K5"] = "The fixed local chain checks independent text/diff/image facts, zero-model preview, focus/reading position, source hashes, actual lock/ACL denial and exact owned-process exit. Missing DOM bytes require specific hash-bound program-assertion evidence; if that proof is absent they stay unknown. No cross-process zoom persistence is required."
    decisions = list(outcome.critical_contracts.values())
    outcome.product_status = "failed" if "failed" in decisions else "inconclusive" if "unknown" in decisions else "passed"
    outcome.deterministic_status = "failed" if outcome.product_status == "failed" else "passed" if outcome.product_status == "passed" else "not_measured"
    outcome.target_status = "failed" if outcome.product_status == "failed" else "passed" if outcome.product_status == "passed" else "pending_review"
    facts["measurement_completeness"] = "incomplete" if any(value is None for group in checks.values() for value in group.values()) else "complete"
    facts["known_contract_failures"] = [name for name,status in outcome.critical_contracts.items() if status == "failed"]
    facts["missing_measurements"] = {name:[key for key,value in group.items() if value is None] for name,group in checks.items() if any(value is None for value in group.values())}
    facts["critical_applicability"]["K2"] = ("Actual task, turn and action settings are full_access/never/none, with no approval ID, independent review or approval/review event; this chain does not trigger K2 approval/review decisions."
        if no_review else "K2 checks actual mandatory approval/review and refusal before execution. A confirmed bypass fails; missing approval/review evidence remains unknown.")
    required = ("processes.json", "execution-bindings.json", "expected/criteria.json", "saved-chats.json", "work/observation.json", "reopen/observation.json", "work/public-events.json", "reopen/public-events.json", "work/exchange.json", "reopen/exchange.json")
    facts["missing_originals"] = [name for name in required if not (root / name).is_file()]
    facts["capture_complete"] = captured
    if facts["missing_originals"]:
        outcome.tool_status = "failed"
    facts["declared_local_cases"] = criteria.get("local_ids", [])
    facts["covered"] = facts["declared_local_cases"] if outcome.critical_contracts["K5"] == "passed" else []
    return outcome
