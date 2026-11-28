"""Run the real Main/owner/relay/service chain in a managed directory."""

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from uuid import uuid4

from myrk.project_paths import project_root
from tests.support.agent_acceptance.core import AcceptancePlan, CaseContext, CaseOutcome, SharedControlError
from tests.support.agent_acceptance.formal_authority import (
    FormalAuthority as FormalAuthority, SourceBinding as SourceBinding, provider_config as provider_config,
    verify_authority as verify_authority,
)
from tests.support.agent_acceptance.formal_outcome import (
    formal_evidence as formal_evidence, preserve_runtime as preserve_runtime, read_phase_reports as read_phase_reports,
    scenario_outcome as scenario_outcome, score_scenario,
)
from tests.support.agent_acceptance.formal_workspace import (
    bindings as bindings, build as build, environment as environment, lifecycle as lifecycle, prepare as prepare,
)
from tests.support.agent_acceptance.preparation import LiveAuthorization, LiveGate, SOURCE, bundle_material_root


async def handoff(context: CaseContext) -> CaseOutcome:
    from tests.support.agent_acceptance import builtin_registry

    gate = context.live_gate
    saved = gate.prepared() if gate is not None else None
    registry = builtin_registry(quality_packet=saved.plan.quality_packet) if saved is not None and getattr(saved.plan, "quality_packet", None) is not None else builtin_registry()
    owned_root = None
    if context.execution is not None and gate is None:
        raise SharedControlError("Frozen formal execution requires its original parent gate; no replacement budget or connection was created.")
    if gate is None:
        from tests.support.agent_acceptance.preparation import prepare_offline_qualification

        bundle = prepare_offline_qualification(
            AcceptancePlan(cases=[context.case_id], protocols=[context.protocol], mode="prepare-live"), registry=registry,
        )
        owned_root = bundle.root
        limit = 6
        if context.case_id != "desktop.handoff":
            from tests.support.agent_acceptance.formal_scenarios import SCENARIOS
            limit = SCENARIOS[context.case_id][2]
        gate = LiveGate(owned_root, registry, LiveAuthorization(authorization_id="offline-qualification-" + context.run_id,
            binding=bundle.binding, total_limit=limit, case_limits={context.case_id: limit}, transport="mock",
            total_microusd=limit * 122880, case_microusd={context.case_id: limit * 122880}))
    context = replace(context, live_gate=gate)
    bundle, grant, _ = gate.validate(case_id=context.case_id, protocol=context.protocol)
    build_root = Path(os.environ.get("MYRK_CHECK_RUN_DIRECTORY", str(context.root))) / ("formal-" + uuid4().hex[:8])
    built = await asyncio.to_thread(build, build_root)
    for name in ("build.stdout", "build.stderr"):
        (context.root / name).write_bytes((build_root / name).read_bytes())
    if built != bundle.execution[context.case_id]["compiled_sources"]:
        raise SharedControlError("Rebuilt formal desktop differs from frozen compiled sources; no connection acquired.")
    execution_directory = "execution-" + uuid4().hex
    controller_runtime = bundle_material_root(bundle) / ".scratch" / execution_directory
    split_quality = bundle.candidate_directory is not None and bundle.plan.quality_packet is not None and context.case_id.startswith("quality.")
    runtime = gate.root / ".scratch" / execution_directory if split_quality else controller_runtime
    for owned in dict.fromkeys((controller_runtime, runtime)):
        if any(part.is_symlink() or part.is_junction() for part in (owned, *owned.parents)):
            raise SharedControlError("Formal runtime cannot follow a redirected data or control root.")
        owned.mkdir(parents=True)
    if split_quality:
        for owned, kind, peer in ((controller_runtime, "controller", runtime), (runtime, "quality-data", controller_runtime)):
            (owned / "runtime-owner.json").write_text(json.dumps({"owner": "agent-acceptance-formal", "kind": kind,
                "runtime_directory": str(owned), "accounting_root": str(gate.root), "binding": bundle.binding,
                "execution_id": context.execution_id, "paired_runtime": str(peer),
                "delete_when": "The original parent is retired after exact evidence preservation and confirmed owned process exit."},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    main = build_root / "project/desktop/out/main/index.js"
    source_hashes = [{"path": str(bundle_material_root(bundle) / path), "sha256": value} for path, value in bundle.files.items()
                     if Path(path).suffix in {".py", ".mjs", ".js", ".cjs", ".ps1", ".ts"}]
    protocol = context.protocol
    config = provider_config(protocol)
    scenario = None
    from tests.support.agent_acceptance import deliverables
    if context.case_id != "desktop.handoff":
        from tests.support.agent_acceptance.formal_scenarios import manifest, scenario_config, setup
        config = scenario_config(context.case_id, config)
        setup(context.case_id, runtime, protocol)
        if context.case_id.startswith("quality."):
            from tests.support.agent_acceptance.formal_scenarios import bind_execution as bind_quality_execution
            bind_quality_execution(context, runtime)
        if context.case_id in deliverables.SCENARIOS:
            deliverables.bind_execution(context, runtime)
        from tests.support.agent_acceptance import delegation_parent_check
        if context.case_id in delegation_parent_check.FORMAL_CASES:
            delegation_parent_check.bind_execution(context, runtime)
        from tests.support.agent_acceptance import independent_review
        if context.case_id in independent_review.FORMAL_CASES:
            independent_review.bind_execution(context, runtime)
        from tests.support.agent_acceptance import compaction_continuity
        if context.case_id in compaction_continuity.FORMAL_CASES:
            compaction_continuity.bind_execution(context,runtime)
        from tests.support.agent_acceptance import memory_isolation
        if context.case_id in memory_isolation.IMPLEMENTED:
            memory_isolation.bind_execution(context, runtime)
        from tests.support.agent_acceptance import queue_parallel_stop
        if context.case_id in queue_parallel_stop.SCENARIOS:
            queue_parallel_stop.bind_execution(context, runtime)
        from tests.support.agent_acceptance import save_import_recovery
        if context.case_id in save_import_recovery.SCENARIOS:
            save_import_recovery.bind_execution(context, runtime)
        scenario = manifest(context.case_id, runtime, protocol)
    if context.case_id in {"desktop.handoff", "formal.provider-settings"}:
        from tests.support.agent_acceptance.connection_desktop_lifecycle import bind_execution
        bind_execution(context, runtime)
        if scenario is not None:
            scenario = manifest(context.case_id, runtime, protocol)
    phases = scenario["phases"] if scenario else ["work"]
    phase_results: list[dict[str, Any]] = []
    runner = bundle_material_root(bundle) / "tests/support/agent_acceptance/desktop/run.mjs"
    frozen_modules = bundle_material_root(bundle) / "desktop/node_modules"
    if not frozen_modules.exists():
        subprocess.run([r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoProfile", "-Command", "New-Item", "-ItemType", "Junction",
            "-Path", str(frozen_modules), "-Target", str(project_root(SOURCE) / "desktop/node_modules")], check=True, capture_output=True)
    for phase in phases:
        phase_root = controller_runtime if scenario is None else controller_runtime / phase
        phase_root.mkdir(parents=True, exist_ok=True)
        auth = phase_root / "authorization.json"
        if scenario is not None:
            (phase_root / "scenario.json").write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        authority = {"run_root": str(gate.root), "grant": grant.model_dump(mode="json"), "case_id": context.case_id,
            "protocol": context.protocol, "case_nonce": uuid4().hex, "source_hashes": source_hashes,
            "runtime": str(phase_root), "main": str(main), "compiled_sources": built, "run_id": context.run_id,
            "provider_config": config.model_dump(mode="json"), "scenario_root": str(runtime) if scenario else None, "phase": phase,
            "execution_id": context.execution.execution_id if context.execution is not None else None,
            "candidate_binding": bundle.binding if bundle.candidate_directory is not None else None}
        auth.write_text(json.dumps(authority, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        env = {**environment(context.root), "PYTHONPATH": str(bundle_material_root(bundle)), "MYRK_ACCEPTANCE_AUTHORITY": str(auth),
            "MYRK_ACCEPTANCE_EVIDENCE_ROOT": str(context.root),
            "MYRK_SHARED_DESKTOP_MODULES": str(project_root(SOURCE) / "desktop/node_modules"),
            "MYRK_CONNECTION_PROJECT_ROOT": str(bundle_material_root(bundle)), "MYRK_SERVICE_MODULE": "tests.support.agent_acceptance.formal_service"}
        validated = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "validate", str(auth)],
            cwd=bundle_material_root(bundle), env=env, capture_output=True)
        (context.root / f"{phase}-validate.stdout").write_bytes(validated.stdout)
        (context.root / f"{phase}-validate.stderr").write_bytes(validated.stderr)
        (context.root / f"{phase}-validate-result.json").write_text(json.dumps({"case_id":context.case_id,"protocol":protocol,"phase":phase,
            "returncode":validated.returncode,"argv":validated.args,"stdout_sha256":sha256(validated.stdout).hexdigest(),
            "stderr_sha256":sha256(validated.stderr).hexdigest()},indent=2)+"\n",encoding="utf-8")
        if validated.returncode:
            preserved = preserve_runtime(context, runtime, gate.root)
            (context.root / "phase-validation-failure.json").write_text(json.dumps({"phase":phase,"returncode":validated.returncode,
                "prior_phases":phase_results,"evidence_relocation":preserved,"prior_runtime_and_ledger_preserved":True},indent=2)+"\n",encoding="utf-8")
            raise SharedControlError(f"Frozen formal entry failed pre-connection validation (exit {validated.returncode}); originals: {context.root}")
        process = await asyncio.create_subprocess_exec("node", str(runner), str(auth), cwd=SOURCE,
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        (context.root / f"{phase}-runner.stdout").write_bytes(stdout)
        (context.root / f"{phase}-runner.stderr").write_bytes(stderr)
        phase_results.append({"phase": phase, "exit_code": process.returncode})
        if split_quality:
            destination = runtime / phase
            destination.mkdir(parents=True, exist_ok=True)
            for original in phase_root.rglob("*"):
                relative = original.relative_to(phase_root)
                if original.is_file() and not any(part in {"profile", "session", "cache"} for part in relative.parts):
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    body = original.read_bytes()
                    if target.exists() and target.read_bytes() != body:
                        raise SharedControlError("Formal preserved phase path already contains different original bytes.")
                    target.write_bytes(body)
                    if sha256(target.read_bytes()).digest() != sha256(body).digest():
                        raise SharedControlError("Formal phase control original could not be preserved exactly.")
            (runtime / "control-runtime-owner.json").write_bytes((controller_runtime / "runtime-owner.json").read_bytes())
        if process.returncode:
            break
    exact_relocations = preserve_runtime(context, runtime, gate.root)
    auth = context.root / "authorization.json"
    if scenario is not None:
        outcome = score_scenario(context, gate, bundle, grant, runtime=runtime, phases=phases,
            phase_results=phase_results, exact_relocations=exact_relocations, config=config)
        shutil.rmtree(runtime)
        shutil.rmtree(build_root)
        for path in (context.root / leaf for leaf in ("temp", "local", "roaming", "cache")):
            if path.exists():
                shutil.rmtree(path)
        if owned_root is not None:
            if frozen_modules.is_junction():
                frozen_modules.rmdir()
            shutil.rmtree(owned_root)
        return outcome
    evidence = {"authority": auth, "processes": context.root / "processes.json", "receiver": context.root / "receiver/state.json",
        "owner": context.root / "owner/state.json", "relay": context.root / "relay/state.json", "exchange": context.root / "receiver/exchange.json",
        "events": context.root / "receiver/events.json", "handoff": context.root / "connection-owner-state.json",
        "transport": context.root / "receiver/transport.json", "prepared": context.root / "prepared.json", "ledger": context.root / "ledger.json"}
    if process.returncode != 0 or any(not path.is_file() for path in evidence.values()):
        raise SharedControlError(f"Formal desktop qualification failed ({process.returncode}); original logs: {context.root}")
    exchange = json.loads(evidence["exchange"].read_text(encoding="utf-8"))
    receiver = json.loads(evidence["receiver"].read_text(encoding="utf-8"))
    owner = json.loads(evidence["owner"].read_text(encoding="utf-8"))
    transport = json.loads(evidence["transport"].read_text(encoding="utf-8"))
    if transport.get("control_error"):
        raise SharedControlError(f"Formal send control stopped the run: {transport['control_error']}")
    processes = json.loads(evidence["processes"].read_text(encoding="utf-8"))
    if not processes["complete"]:
        gate.stop()
        raise SharedControlError(f"Owned handoff processes have not all exited; runtime retained: {runtime}")
    passed = (receiver["passed"] and processes["complete"] and len(exchange["exchanges"]) == 1
              and (grant.transport != "mock" or owner["synthetic_profile"] and receiver["synthetic_profile"]))
    outcome = CaseOutcome("passed" if passed else "failed", {"product_attempts": len(exchange["product_attempts"]),
        "mock_http_attempts": transport["mock_http_attempts"], "real_http_attempts": transport["real_http_attempts"],
        "normal_profile_accesses": owner["normal_profile_accesses"], "owner_profile": owner["profile"],
        "receiver_profile": receiver["profile"], "owner_connection_store": owner["connection_store"],
        "synthetic_profiles": owner["synthetic_profile"] and receiver["synthetic_profile"], "process_exit": process.returncode,
        "transport": grant.transport, "mode": context.mode, "compiled_sources_match": True}, evidence)
    outcome.facts["budget"] = gate.accounting_report()
    outcome.execution_id = gate.execution_id(context.case_id, protocol)
    shutil.rmtree(runtime)
    for path in (build_root, *(context.root / leaf for leaf in ("temp", "local", "roaming", "cache"))):
        if path.exists():
            shutil.rmtree(path)
    if owned_root is not None:
        if frozen_modules.is_junction():
            frozen_modules.rmdir()
        shutil.rmtree(owned_root)
    return outcome
