"""Build and run the real Main/owner/relay/service chain in a managed directory."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Literal
from uuid import uuid4

from myrk.project_paths import project_root
from myrk.provider import ProviderConfig
from pydantic import BaseModel, ConfigDict, Field
from tests.support.agent_acceptance.core import AcceptancePlan, CaseContext, CaseOutcome, CaseRegistry, IncompleteEvidenceError, Protocol, SharedControlError
from tests.support.agent_acceptance.preparation import LiveAuthorization, LiveGate, SOURCE, prepare_live, read_prepared_snapshot, bundle_material_root
from tests.support.agent_acceptance.stop_request_evidence import StopExchange, stop_reservations


def provider_config(protocol: Protocol) -> ProviderConfig:
    model = "gemini-2.5-flash" if protocol == "google" else "gpt-5.4" if protocol == "openai-chat" else "deepseek-flash"
    return ProviderConfig(name="Acceptance formal", protocol=protocol, available_models=[model],
        base_url="https://generativelanguage.googleapis.com" if protocol == "google" else
        "https://api.openai.com/v1" if protocol == "openai-chat" else "https://api.deepseek.com",
        model=model)


class SourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path
    sha256: str


class FormalAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_root: Path
    grant: LiveAuthorization
    case_id: str
    protocol: Protocol
    case_nonce: str = Field(min_length=1)
    source_hashes: list[SourceBinding]
    runtime: Path
    main: Path
    compiled_sources: dict[str, str]
    run_id: str
    provider_config: ProviderConfig
    scenario_root: Path | None = None
    phase: Literal["work", "capacity-reopen", "reopen"] = "work"
    execution_id: str | None = None
    candidate_binding: str | None = None


def verify_authority(path: Path) -> tuple[FormalAuthority, LiveGate]:
    from tests.support.agent_acceptance import builtin_registry

    try:
        value = FormalAuthority.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise SharedControlError(f"Formal authority cannot be read: {error}") from error
    saved = read_prepared_snapshot(value.run_root, binding=value.grant.binding)
    registry = builtin_registry(quality_packet=saved.plan.quality_packet) if getattr(saved.plan, "quality_packet", None) is not None else builtin_registry()
    gate = LiveGate(value.run_root, registry, value.grant)
    if value.execution_id is not None:
        gate = gate.for_execution(value.execution_id)
    bundle, _, _ = gate.validate(case_id=value.case_id, protocol=value.protocol)
    if value.candidate_binding != (bundle.binding if bundle.candidate_directory is not None else None):
        raise SharedControlError("Formal authority does not bind the active reviewed candidate.")
    expected = bundle.execution[value.case_id]
    split_quality = bundle.candidate_directory is not None and bundle.plan.quality_packet is not None and value.case_id.startswith("quality.")
    data_namespace = bundle.root / ".scratch" if split_quality else bundle_material_root(bundle) / ".scratch"
    sources = {str(bundle_material_root(bundle) / name): checksum for name, checksum in bundle.files.items()
               if Path(name).suffix in {".py", ".mjs", ".js", ".cjs", ".ps1", ".ts"}}
    config = provider_config(value.protocol)
    if value.case_id != "desktop.handoff":
        from tests.support.agent_acceptance.formal_scenarios import manifest, scenario_config
        config = scenario_config(value.case_id, config)
        scenario_root = value.scenario_root
        try:
            scenario = json.loads((value.runtime / "scenario.json").read_bytes())
        except (OSError, ValueError) as error:
            raise SharedControlError(f"Formal scenario binding cannot be read: {error}") from error
        paired_roots = (scenario_root is not None and value.runtime.parent == scenario_root)
        if split_quality:
            paired_roots = (scenario_root == bundle.root / ".scratch" / value.runtime.parent.name
                and value.runtime.parent.parent == bundle_material_root(bundle) / ".scratch"
                and re.fullmatch(r"execution-[a-f0-9]{32}", value.runtime.parent.name) is not None)
        if (scenario_root is None or not paired_roots or value.runtime.name != value.phase
                or not scenario_root.is_relative_to(data_namespace)
                or scenario_root.resolve() != scenario_root.absolute() or value.runtime.resolve() != value.runtime.absolute()
                or value.phase not in scenario["phases"] or scenario != manifest(value.case_id, scenario_root, value.protocol)):
            raise SharedControlError("Formal scenario material or root changed; no connection acquired.")
    if (value.provider_config != config
            or value.compiled_sources != expected["compiled_sources"]
            or {str(item.path): item.sha256 for item in value.source_hashes} != sources
            or value.runtime != path.parent or not value.runtime.is_relative_to(bundle_material_root(bundle) / ".scratch")
            or not value.main.is_relative_to(project_root(SOURCE) / ".scratch")):
        raise SharedControlError("Formal handoff authority differs from the frozen preparation; no connection acquired.")
    output = value.main.parent.parent
    for name, checksum in value.compiled_sources.items():
        candidate = output / name
        if not candidate.resolve().is_relative_to(output.resolve()) or sha256(candidate.read_bytes()).hexdigest() != checksum:
            raise SharedControlError("Formal compiled source binding changed; no connection acquired.")
    return value, gate


def environment(root: Path) -> dict[str, str]:
    for leaf in ("temp", "local", "roaming", "cache"):
        (root / leaf).mkdir(parents=True, exist_ok=True)
    return {**os.environ, "TEMP": str(root / "temp"), "TMP": str(root / "temp"), "TMPDIR": str(root / "temp"),
        "LOCALAPPDATA": str(root / "local"), "APPDATA": str(root / "roaming"), "PYTHONPATH": str(SOURCE),
        "PYTHONDONTWRITEBYTECODE": "1", "PYDANTIC_AI_NO_BANNER": "1", "MYRK_PYTHON": sys.executable,
        "npm_config_cache": str(root / "cache"), "MYRK_BACKGROUND_TEST": "1", "ELECTRON_RENDERER_URL": ""}


def build(root: Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=False)
    output = root / "project/desktop/out"
    output.parent.mkdir(parents=True)
    (output.parent / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    modules = project_root(SOURCE) / "desktop/node_modules"
    if not (SOURCE / "desktop/node_modules").exists():
        subprocess.run([r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoProfile", "-Command", "New-Item", "-ItemType", "Junction",
            "-Path", str(SOURCE / "desktop/node_modules"), "-Target", str(modules)], check=True, capture_output=True)
    result = subprocess.run(["node", str(modules / "electron-vite/bin/electron-vite.js"), "build", "--outDir", str(output)],
        cwd=SOURCE / "desktop", env=environment(root), capture_output=True, check=False)
    (root / "build.stdout").write_bytes(result.stdout)
    (root / "build.stderr").write_bytes(result.stderr)
    if result.returncode:
        raise SharedControlError(f"Formal desktop build failed ({result.returncode}): {root}")
    return {path.relative_to(output).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob("*")) if path.is_file()}


def prepare(root: Path) -> dict[str, Any]:
    from tests.support.agent_acceptance.formal_scenarios import SCENARIOS
    build_root = root / "preparation-build"
    hashes = build(build_root)
    shutil.rmtree(build_root / "project")
    return {"compiled_sources": hashes, "case_limits": {name: spec[2] for name, spec in SCENARIOS.items()},
        "provider_configs": {name: provider_config(name).model_dump(mode="json")
        for name in ("deepseek", "openai-chat", "google")}}


def bindings() -> tuple[Path, ...]:
    return tuple(path for directory in (SOURCE / "desktop/src", SOURCE / "desktop/tests", SOURCE / "desktop/scripts")
        for path in directory.rglob("*") if path.is_file()) + (SOURCE / "desktop/electron.vite.config.ts",
        SOURCE / "desktop/package.json", SOURCE / "desktop/package-lock.json", SOURCE / "tests/support/process_observation.py",
        SOURCE / "tests/support/process_receipt.py")


def preserve_runtime(context: CaseContext, runtime: Path, accounting_root: Path) -> dict[str, str]:
    exact_relocations: dict[str, str] = {}
    long_path_originals = []
    for path in runtime.rglob("*"):
        if path.is_file() and not any(part in {"profile", "session", "cache"} for part in path.relative_to(runtime).parts):
            target = context.root / path.relative_to(runtime)
            if context.case_id == "formal.recovery-unknown" and path.name == "save-diagnostics.json" and path.stat().st_size == 0:
                target = target.with_suffix(".json.incomplete-original")
                exact_relocations[str(path)] = target.relative_to(context.root).as_posix()
            if len(str(target)) >= 248:
                target = context.root / "long-path-originals" / (sha256(path.relative_to(runtime).as_posix().encode()).hexdigest()[:24] + path.suffix)
                exact_relocations[str(path)] = target.relative_to(context.root).as_posix()
                long_path_originals.append({"source_path":str(path),"logical_path":path.relative_to(runtime).as_posix(),
                    "stored_path":target.relative_to(context.root).as_posix(),"sha256":sha256(path.read_bytes()).hexdigest(),"bytes":path.stat().st_size})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    if long_path_originals:
        (context.root / "long-path-originals.json").write_text(json.dumps(long_path_originals,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    for name in ("prepared.json", "ledger.json"):
        try:
            (context.root / name).write_bytes((accounting_root / name).read_bytes())
        except OSError as error:
            raise SharedControlError(f"Formal accounting original cannot be preserved: {name}: {error}") from error
    if context.live_gate is not None:
        ledger = context.live_gate._read_ledger()
        if ledger.candidates:
            import zipfile
            snapshots = [accounting_root, *(accounting_root / ".candidates" / item["directory"] for item in ledger.candidates)]
            from tests.support.agent_acceptance.preparation import PreparedBundle
            with zipfile.ZipFile(context.root / "accounting.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                for source in snapshots:
                    prepared = PreparedBundle.model_validate_json((source / "prepared.json").read_bytes())
                    for name in (*prepared.files, "prepared.json", *(("candidate-review.json",) if prepared.candidate_directory else ())):
                        archive.writestr((source.relative_to(accounting_root) / name).as_posix(), (source / name).read_bytes())
                archive.writestr("ledger.json", (accounting_root / "ledger.json").read_bytes())
    return exact_relocations


def formal_evidence(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*")
        if path.is_file() and not any(part in {"temp", "local", "roaming", "cache"} for part in path.relative_to(root).parts)}


def read_phase_reports(runtime: Path, name: str) -> dict[str, dict[str, Any]]:
    reports = {}
    for path in runtime.glob(f"*/{name}"):
        try:
            report = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise SharedControlError(f"Formal control original cannot be read: {path}: {error}") from error
        if not isinstance(report, dict):
            raise SharedControlError(f"Formal control original must be an object: {path}")
        reports[path.relative_to(runtime).parts[0]] = report
    return reports


async def handoff(context: CaseContext) -> CaseOutcome:
    from tests.support.agent_acceptance import builtin_registry

    gate = context.live_gate
    saved = gate.prepared() if gate is not None else None
    registry = builtin_registry(quality_packet=saved.plan.quality_packet) if saved is not None and getattr(saved.plan, "quality_packet", None) is not None else builtin_registry()
    owned_root = None
    if context.execution is not None and gate is None:
        raise SharedControlError("Frozen formal execution requires its original parent gate; no replacement budget or connection was created.")
    if gate is None:
        owned_root = project_root(SOURCE) / ".scratch/acceptance-runs" / uuid4().hex
        bundle = prepare_live(AcceptancePlan(cases=[context.case_id], protocols=[context.protocol], mode="prepare-live"), root=owned_root, registry=registry)
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
        from tests.support.agent_acceptance.formal_scenarios import score
        records = [json.loads(path.read_bytes()) for path in (runtime / "records").rglob("*.json")
            if path.parent == runtime / "records" or path.parent.name.endswith("-delegations")]
        exchange_paths = runtime.glob("*/receiver/exchange.json")
        invalid_exchanges: list[str] = []
        exchanges: list[dict[str, Any]]
        stop_case = context.case_id == "quality.qg3.formal.9"
        stop_exchange = None
        if stop_case:
            from tests.support.agent_acceptance.preparation import _LiveLedger
            from tests.support.agent_acceptance.quality_governance import read_stop_original
            from tests.support.agent_acceptance.reporting import validate_exchange_header

            exchanges = []
            parsed = []
            for path in exchange_paths:
                label = path.relative_to(runtime).as_posix()
                exchange = read_stop_original(path)
                if not isinstance(exchange, dict) or not validate_exchange_header(context, label, exchange):
                    invalid_exchanges.append(label)
                    continue
                observation = StopExchange.from_original(exchange)
                if observation.missing_sections:
                    invalid_exchanges.append(label)
                exchanges.append(exchange)
                parsed.append(observation)
            if len(parsed) == 1:
                try:
                    ledger = _LiveLedger.model_validate_json((context.root / "ledger.json").read_bytes())
                except (OSError, ValueError) as error:
                    raise SharedControlError(f"Formal stop ledger cannot be read: {error}") from error
                stop_exchange = replace(parsed[0], reservations=stop_reservations(context, ledger))
        else:
            exchanges = [json.loads(path.read_bytes()) for path in exchange_paths]
        request_budget = None
        if context.case_id.startswith("quality.qg4."):
            accounting = gate.accounting_report()
            selected = [item for item in accounting["attempts"] if item["execution_id"] == context.execution_id]
            owners = accounting["product_request_owners"]
            projected_requests = [{**item, "task_id": owners[str(item["number"])]["task_id"] if str(item["number"]) in owners else None}
                for item in selected]
            request_budget = {"origin": "validated_controller_ledger", "scope": gate.registry.cases[context.case_id].product_stop_scope, "observed_at": datetime.now(timezone.utc).isoformat(),
                "binding": accounting["binding"], "execution_id": context.execution_id, "case_id": context.case_id, "protocol": protocol,
                "frozen_request_cap": context.execution.request_cap if context.execution is not None else bundle.budget_proposals[context.execution_id].request_limit,
                "approved_request_cap": grant.case_limits[context.execution_id], "reserved_requests": len(selected),
                "requests": projected_requests}
        facts = score(context.case_id, runtime, records, exchanges, request_budget=request_budget, stop_exchange=stop_exchange)
        required = [runtime / phase / name for phase in phases for name in
            ("processes.json", "receiver/state.json", "receiver/events.json", "receiver/exchange.json", "receiver/transport.json", "owner/state.json", "relay/state.json")]
        incomplete = [*invalid_exchanges, *(path.relative_to(runtime).as_posix() for path in required if not path.is_file())]
        transport_reports = read_phase_reports(runtime, "receiver/transport.json")
        control_errors = [report["control_error"] for report in transport_reports.values() if report.get("control_error")]
        if control_errors:
            recognized = None
            if context.case_id.startswith("quality."):
                from tests.support.agent_acceptance.quality_runtime import recognize_default_permission_loss
                recognized = recognize_default_permission_loss(context.case_id, runtime, protocol,
                    ledger=json.loads((gate.root / "ledger.json").read_bytes()), exchanges=exchanges)
            if (recognized is None or recognized.get("kind") != "public_new_default_permissions_lost"
                    or recognized.get("product_status") != "failed" or recognized.get("tool_contract_passed") is not True
                    or recognized.get("fact_contract_status") != "failed" or recognized.get("ledger_stopped") is not True
                    or facts.get("tool_contract_passed") is not True or facts.get("passed") is not False
                    or facts.get("critical_contracts", {}).get("K1") != "failed" or facts.get("driver_errors")):
                raise SharedControlError(f"Formal send control stopped the run: {control_errors}; originals: {context.root}")
            (context.root / "formal-original-control-verdict.json").write_text(json.dumps({
                "kind": "original_unclassified_shared_control_stop", "product_status": "failed", "tool_status": "failed",
                "control_errors": control_errors, "facts": facts,
                "ledger_sha256": sha256((gate.root / "ledger.json").read_bytes()).hexdigest()},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            facts["known_default_permission_loss"] = recognized
            (context.root / "known-default-permission-loss.json").write_text(json.dumps(recognized,
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        process_reports = read_phase_reports(runtime, "processes.json")
        if process_reports.keys() - set(phases) or any(item.get("complete") is not True for item in process_reports.values()):
            raise SharedControlError(f"Owned formal process exit is unconfirmed; original runtime retained: {runtime}")
        if incomplete:
            if stop_case:
                from tests.support.agent_acceptance.reporting import validate_evidence

                control = gate.accounting_report()
                if control.get("stopped"):
                    raise SharedControlError(control.get("stop_reason") or "Shared control stopped.")
                facts["evidence_relocation"] = {**exact_relocations, str(runtime): "."}
                evidence = formal_evidence(context.root)
                evidence["budget.ledger"] = context.root / "ledger.json"
                outcome = CaseOutcome("failed", facts, evidence, critical_contracts=facts["critical_contracts"])
                try:
                    validate_evidence(context, outcome, missing_originals=incomplete)
                except IncompleteEvidenceError as error:
                    raise IncompleteEvidenceError(f"Formal phase evidence is incomplete: {error}; originals retained at {context.root}",
                        stop_violation=error.stop_violation) from error
                except (OSError, ValueError, TypeError, KeyError, StopIteration) as error:
                    raise SharedControlError(f"Formal phase evidence cannot be validated: {error}") from error
            raise IncompleteEvidenceError(f"Formal phase evidence is incomplete ({', '.join(incomplete)}); originals retained at {context.root}")
        facts.update(compiled_sources_match=True, transport=grant.transport, mode=context.mode,
            runtime_versions=[json.loads((runtime / phase / "receiver/state.json").read_bytes())["runtime_versions"] for phase in phases],
            phases=phase_results, processes_complete=True,
            desktop_exits={
                phase: {"root_exit": item.get("root_exit"), "failure": item.get("failure")}
                for phase, item in process_reports.items()
            },
            real_http_attempts=sum(report["real_http_attempts"] for report in transport_reports.values()),
            mock_http_attempts=sum(report["mock_http_attempts"] for report in transport_reports.values()),
            planned_local_transport_failures=sum(report["planned_local_transport_failures"] for report in transport_reports.values()),
            evidence_relocation={**exact_relocations, str(runtime): "."})
        from tests.support.agent_acceptance.formal_save import recognize_runtime
        current_failure = recognize_runtime(context, runtime=runtime, bundle=bundle, grant=grant,
            ledger_path=gate.root / "ledger.json", records=records, exchanges=exchanges, facts=facts, source=SOURCE)
        if current_failure is not None:
            (context.root / "formal-original-failed-verdict.json").write_text(json.dumps({
                "kind": "original_unclassified_formal_verdict", "case_id": context.case_id, "protocol": context.protocol,
                "product_status": "failed", "tool_status": "failed", "facts": facts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (context.root / "current-formal-save-observation.json").write_text(json.dumps(current_failure,
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if context.case_id.startswith(("formal.compaction-", "quality.qg6.")) and records:
            from myrk.conversation import ConversationRecord
            from tests.support.agent_acceptance.compaction_review import write_review
            from tests.support.agent_acceptance.replay import OfflineSession

            record = ConversationRecord.model_validate(records[0])
            captured = OfflineSession(protocol, [], config=config)
            captured.sdk_inputs = [item for envelope in exchanges for item in envelope["sdk_inputs"]]
            captured.exchanges = [item for envelope in exchanges for item in envelope["exchanges"]]
            captured.save(context.root / "exchange.json", attempts=[item for envelope in exchanges for item in envelope["product_attempts"]],
                run_id=context.run_id, case_id=context.case_id, task_id=record.turns[-1].task.id if record.turns and record.turns[-1].task else "")
            write_review(context, record, captured, provenance="current_real" if grant.transport == "real" else "fixed_fixture")
        evidence = formal_evidence(context.root)
        facts["budget"] = gate.accounting_report()
        outcome = scenario_outcome(facts, evidence, current_failure=current_failure, expected_phases=len(phases))
        outcome.execution_id = gate.execution_id(context.case_id, protocol)
        if context.case_id.startswith("quality."):
            from tests.support.agent_acceptance.quality_runtime import finish as finish_quality
            finish_quality(context, outcome)
        if context.case_id in {"formal.command", "formal.calculation-check", "formal.files"}:
            from tests.support.agent_acceptance.files_calculation import finish
            finish(context, outcome, facts.get("domain", {"actual_file_bytes_verified": facts["passed"], "final_sha256": facts.get("final_bytes_sha256")}))
        from tests.support.agent_acceptance import compaction_continuity
        if context.case_id in compaction_continuity.FORMAL_CASES:
            compaction_continuity.finish(context,outcome)
        from tests.support.agent_acceptance import delegation_parent_check
        if context.case_id in delegation_parent_check.FORMAL_CASES:
            delegation_parent_check.finish(context, outcome)
        from tests.support.agent_acceptance import memory_isolation
        if context.case_id in memory_isolation.IMPLEMENTED:
            memory_isolation.finish(context, outcome)
        from tests.support.agent_acceptance import queue_parallel_stop
        if context.case_id in queue_parallel_stop.SCENARIOS:
            queue_parallel_stop.finish(context, outcome)
        from tests.support.agent_acceptance import independent_review
        if context.case_id in independent_review.FORMAL_CASES:
            independent_review.finish(context, outcome)
        from tests.support.agent_acceptance import formal_approval
        if context.case_id in formal_approval.FORMAL_CASES:
            formal_approval.finish(context, outcome)
        from tests.support.agent_acceptance.guidance_skills_extensions import SCENARIOS as GUIDANCE_SCENARIOS, finish as finish_guidance
        if context.case_id in GUIDANCE_SCENARIOS:
            finish_guidance(context, outcome)
        from tests.support.agent_acceptance.authorized_send import SCENARIOS as SEND_SCENARIOS, finish as finish_send
        if context.case_id in SEND_SCENARIOS:
            finish_send(context, outcome)
        from tests.support.agent_acceptance import save_import_recovery
        if context.case_id in save_import_recovery.SCENARIOS:
            save_import_recovery.finish(context, outcome)
        if context.case_id in deliverables.SCENARIOS:
            deliverables.finish(context, outcome)
        if context.case_id == "formal.provider-settings":
            from tests.support.agent_acceptance.connection_desktop_lifecycle import finish as finish_connection
            finish_connection(context, outcome)
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


def scenario_outcome(facts: dict[str, Any], evidence: dict[str, Path], *, current_failure: dict[str, Any] | None = None,
                     expected_phases: int | None = None) -> CaseOutcome:
    """Keep raw product and driver facts when constructing a formal scenario outcome."""
    phases: list[dict[str, Any]] = facts["phases"]
    desktop_exit_passed = (
        facts["processes_complete"] is True and (expected_phases is None or len(phases) == expected_phases)
        and all(item["exit_code"] == 0 for item in phases)
    )
    exits: dict[str, dict[str, Any]] | None = facts.get("desktop_exits")
    if exits is not None:
        desktop_exit_passed = (
            desktop_exit_passed and set(exits) == {item["phase"] for item in phases}
            and all(item["root_exit"] == {"code": 0, "signal": None} and item["failure"] is None for item in exits.values())
        )
    facts = {**facts, "desktop_exit_passed": desktop_exit_passed}
    passed = facts["passed"] and desktop_exit_passed
    status: Literal["passed", "failed", "inconclusive", "not_executed"] = "not_executed" if facts.get("product_execution") == "not_executed" else "passed" if passed else "failed"
    tool_status: Literal["passed", "failed"] = (
        "failed" if not desktop_exit_passed or facts.get("tool_contract_passed") is False or facts.get("driver_errors") else "passed"
    )
    if (status == "failed" and facts.get("fact_contract_status") == "unknown" and facts.get("goal_program_contract_passed")
            and tool_status == "passed"):
        status = "inconclusive"
    from tests.support.agent_acceptance.formal_save import known_failure
    if status == "failed" and known_failure(current_failure):
        facts.update(current_save_failure=current_failure, original_formal_verdict={"product_status": status, "tool_status": tool_status})
        if desktop_exit_passed:
            facts["tool_contract_passed"] = True
            tool_status = "passed"
    return CaseOutcome(status, facts, evidence, tool_status=tool_status)


async def lifecycle(context: CaseContext) -> CaseOutcome:
    await asyncio.to_thread(build, context.root / "build")
    env = {**environment(context.root), "MYRK_TEST_RUN_DIRECTORY": str(context.root / "build")}
    process = await asyncio.create_subprocess_exec("node", str(SOURCE / "tests/support/agent_acceptance/desktop/lifecycle.mjs"),
        str(context.root), cwd=SOURCE, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    (context.root / "runner.stdout").write_bytes(stdout)
    (context.root / "runner.stderr").write_bytes(stderr)
    path = context.root / "lifecycle-result.json"
    if not path.is_file():
        raise SharedControlError(f"Desktop lifecycle evidence missing ({process.returncode}): {context.root}")
    facts = json.loads(path.read_text(encoding="utf-8"))
    facts.update(real_http_attempts=0, normal_profile_accesses=sum(not item["isolated_profile"] for item in facts["observations"]))
    evidence = {"lifecycle": path}
    for item in context.root.rglob("*"):
        if item.is_file() and (item.name in {"processes.json", "electron.jsonl", "service.jsonl", "crash.jsonl", "recover.jsonl",
            "normal-reopen.jsonl", "save.json", "restore.json", "forgotten.json", "before-crash.json"}):
            evidence[item.relative_to(context.root).as_posix()] = item
    for path in (context.root / "build/project", *(context.root / leaf for leaf in ("temp", "local", "roaming", "cache")),
                 *(context.root / leaf / "user-data" for leaf in ("credentials", "lifecycle", "recovery"))):
        if path.exists():
            shutil.rmtree(path)
    return CaseOutcome("passed" if facts["passed"] and process.returncode == 0 else "failed", facts, evidence)
