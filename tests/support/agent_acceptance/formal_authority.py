"""Verify the frozen authority passed to a formal desktop process."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Literal

from myrk.project_paths import project_root
from myrk.provider import ProviderConfig
from pydantic import BaseModel, ConfigDict, Field
from tests.support.agent_acceptance.core import Protocol, SharedControlError
from tests.support.agent_acceptance.preparation import (
    LiveAuthorization, LiveGate, SOURCE, bundle_material_root, read_prepared_snapshot,
)


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
    canonical_quality = bundle.plan.quality_packet is not None and value.case_id.startswith("quality.")
    split_quality = bundle.candidate_directory is not None and canonical_quality
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
                or value.phase not in scenario["phases"]):
            raise SharedControlError("Formal scenario material or root changed; no connection acquired.")
        if canonical_quality:
            from tests.support.agent_acceptance.quality_runtime import verify_frozen_isolation

            evidence_root = os.environ.get("MYRK_ACCEPTANCE_EVIDENCE_ROOT")
            if not evidence_root:
                raise SharedControlError("Formal quality execution lacks the launcher's evidence isolation directory.")
            verify_frozen_isolation(value.case_id, scenario_root, bundle, evidence_root=Path(evidence_root))
        if scenario != manifest(value.case_id, scenario_root, value.protocol):
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
