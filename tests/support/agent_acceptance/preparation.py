"""Freeze a closed execution bundle, then account for every authorized send."""

from dataclasses import dataclass, replace
from base64 import b64decode, b64encode
from decimal import Decimal, ROUND_CEILING
from datetime import datetime, timezone
from contextlib import contextmanager
from collections.abc import Iterator, Callable
from hashlib import sha256
import gzip
import inspect
import json
import os
import zipfile
import zlib
from pathlib import Path
from typing import Annotated, Any, Literal, Concatenate, ParamSpec, TypeVar
from functools import wraps

import httpx2
from pydantic_ai import CancellationToken

from myrk.provider import ProviderConfig
from pydantic import TypeAdapter, BaseModel, ConfigDict, Field, model_validator, field_validator

from myrk.project_paths import project_root
from tests.support.agent_acceptance.core import AcceptancePlan, CaseRegistry, Protocol, SharedControlError

from tests.support.agent_acceptance.eligibility import (
    NativeEligibilityObservation, RouteEligibility, RouteStandardVersion, deepseek_thinking_disabled,
    preparation_eligibility,
)

SOURCE = Path(__file__).resolve().parents[3]


class ProductOwnerOriginal(BaseModel):
    """One complete immutable owner source, in its legacy or lossless encoding."""
    model_config = ConfigDict(extra='forbid')
    task_id: str
    path: str
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    bytes: int = Field(strict=True, ge=0)
    original_base64: str | None = None
    original_codec: Literal['gzip-base64-v1'] | None = None
    original_compressed_base64: str | None = None

    @model_validator(mode='after')
    def one_encoding(self) -> 'ProductOwnerOriginal':
        formats = self.model_fields_set & {'original_base64', 'original_codec', 'original_compressed_base64'}
        if formats == {'original_base64'} and self.original_base64 is not None:
            return self
        if formats == {'original_codec', 'original_compressed_base64'} and self.original_codec is not None and self.original_compressed_base64 is not None:
            return self
        raise ValueError('Owner original requires one explicit complete encoding.')


def restore_product_original(value: dict[str, Any]) -> bytes:
    """Recover original bytes without changing their layout or owner identity."""
    from myrk.conversation import ConversationRecord

    try:
        source = ProductOwnerOriginal.model_validate(value)
        if source.original_codec is not None:
            assert source.original_compressed_base64 is not None
            raw = gzip.decompress(b64decode(source.original_compressed_base64, validate=True))
        else:
            assert source.original_base64 is not None
            raw = b64decode(source.original_base64, validate=True)
        if len(raw) != source.bytes or sha256(raw).hexdigest() != source.sha256:
            raise ValueError('Owner original length or SHA differs from the captured bytes.')
        record = ConversationRecord.model_validate_json(raw)
        if not any(turn.task is not None and turn.task.id == source.task_id for turn in record.turns):
            raise ValueError('Owner original does not contain its saved Task.')
        return raw
    except (OSError, EOFError, ValueError, zlib.error) as error:
        raise SharedControlError(f'Product owner original cannot be restored: {error}') from error


def validate_product_originals(owners: dict[str, dict[str, Any]]) -> None:
    """Reject unreadable source evidence before a reservation or terminal send."""
    for owner in owners.values():
        for source in owner['sources']:
            restore_product_original(source)
            if source['task_id'] != owner['task_id']:
                raise SharedControlError('Product owner original Task binding changed.')

def digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def bound_files(plan: AcceptancePlan, registry: CaseRegistry) -> dict[str, str]:
    if getattr(plan, "quality_packet", None) is not None:
        from tests.support.agent_acceptance import builtin_registry
        canonical = builtin_registry(quality_packet=plan.quality_packet)
        if registry.select(plan) != canonical.select(plan):
            raise SharedControlError("Quality packet registry differs from the canonical frozen adapter.")
    paths = {*SOURCE.joinpath("myrk").rglob("*.py"), *SOURCE.joinpath("myrk").glob("*.json"),
             *Path(__file__).parent.rglob("*.py"), *Path(__file__).parent.rglob("*.mjs"),
             *SOURCE.joinpath("tests/acceptance").glob("*.py"),
             SOURCE / "tests/__init__.py", SOURCE / "pyproject.toml"}
    paths.update(SOURCE / name for name in ("conftest.py", "tests/conftest.py", "tests/acceptance/conftest.py",
        "tests/acceptance/test_selected.py", "scripts/run_checks.py", "requirements.txt", "requirements-dev.txt", "requirements.lock")
        if (SOURCE / name).is_file())
    paths.update(path for directory in (SOURCE / "tests/acceptance/materials", SOURCE / "tests/acceptance/plans")
        for path in directory.rglob("*") if path.is_file())
    paths.update(Path(__file__).parent.rglob(".gitattributes"))
    for case, _ in registry.select(plan):
        paths.add(Path(inspect.getfile(case.run)))
        paths.update(case.bindings)
    files = {}
    for path in sorted(paths):
        path = path.resolve()
        if not path.is_relative_to(SOURCE) or not path.is_file():
            raise SharedControlError(f"Execution binding must be a source file: {path}")
        files[path.relative_to(SOURCE).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return files


class CaseBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_limit: int = Field(gt=0, strict=True)
    cost_limit_microusd: int = Field(gt=0, strict=True)


class FrozenPrice(BaseModel):
    """Offline quotation only; qualification remains a separate, closed gate."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Protocol
    role: Literal["product", "judge"]
    input_per_million: str
    output_per_million: str
    input_limit: int = 32768
    output_limit: int
    version: Literal["offline-2026-11-16"] = "offline-2026-11-16"
    synthetic_only: Literal[True] = True

    def microusd(self, input_tokens: int, output_tokens: int) -> int:
        return int((Decimal(input_tokens) * Decimal(self.input_per_million) +
                    Decimal(output_tokens) * Decimal(self.output_per_million)).to_integral_value(rounding=ROUND_CEILING))

    @property
    def reservation_microusd(self) -> int:
        return self.microusd(self.input_limit, self.output_limit)


class CaseBudgetExceeded(SharedControlError):
    """Only the named planned execution has exhausted its grant."""


class PreparedBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: Path
    source_root: Path
    plan: AcceptancePlan
    files: dict[str, str]
    binding: str
    sending_enabled: bool = False
    candidate_directory: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    execution: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any]
    eligibility: tuple[RouteEligibility, ...] = ()
    budget_proposals: dict[str, CaseBudget] = Field(default_factory=dict)
    prices: tuple[FrozenPrice, ...] = ()


def bundle_fingerprint(bundle: PreparedBundle) -> str:
    value = {"root": str(bundle.root), "source_root": str(bundle.source_root), "plan": bundle.plan.model_dump(mode="json"),
        "files": bundle.files, "execution": bundle.execution, "environment": bundle.environment,
        "eligibility": [item.model_dump(mode="json") for item in bundle.eligibility],
        "budget_proposals": {name: item.model_dump() for name, item in bundle.budget_proposals.items()},
        "prices": [item.model_dump() for item in bundle.prices]}
    if bundle.candidate_directory is not None:
        value["candidate_directory"] = bundle.candidate_directory
    return digest(value)


def prepare_live(plan: AcceptancePlan, *, root: Path, registry: CaseRegistry,
                 budget_proposals: dict[str, dict[str, int]] | None = None) -> PreparedBundle:
    plan = AcceptancePlan.model_validate(plan.model_dump())
    registry.select(plan)
    if plan.executions and (plan.parent_budget is None or plan.parent_budget.accounting_root.resolve() != root.resolve()):
        raise SharedControlError("Frozen executions require their original parent accounting root; no new parent grant is created.")
    if plan.mode != "prepare-live":
        raise ValueError("Preparation requires an explicit prepare-live plan.")
    if any(part.is_symlink() or part.is_junction() for part in (root, *root.parents)):
        raise SharedControlError("Prepared root cannot use links.")
    root = root.resolve()
    project = project_root(SOURCE)
    if not root.is_relative_to(project / ".scratch") or root.is_relative_to(project / ".scratch" / "check-runs"):
        raise SharedControlError("Prepared root must be persistent inside project .scratch, outside disposable check runs.")
    root.mkdir(parents=True, exist_ok=False)
    (root / "owner.json").write_text(json.dumps({"owner": "agent-acceptance-preparation",
        "task_id": os.environ.get("MYRK_TASK_ID"), "project_root": str(project), "source_root": str(SOURCE),
        "runtime_directory": str(root), "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Frozen acceptance execution bundle; preparation grants no sending authority.",
        "delete_when": "The run is complete or superseded and necessary original evidence is archived and verified."},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    execution = {}
    preparations: dict[Any, dict[str, Any]] = {}
    for case, _ in registry.select(plan):
        if case.prepare is not None and case.case_id not in execution:
            if case.prepare not in preparations:
                preparations[case.prepare] = case.prepare(root)
            execution[case.case_id] = preparations[case.prepare]
    files = bound_files(plan, registry)
    from tests.support.agent_acceptance.reporting import environment_facts
    environment = environment_facts()
    routes = preparation_eligibility()
    prices = tuple(FrozenPrice(protocol=item.protocol, role=item.role, input_per_million=item.price_input_per_million,
        output_per_million=item.price_output_per_million, output_limit=item.total_output_limit) for item in routes) + (
        FrozenPrice(protocol="openai-chat", role="product", input_per_million="0.30", output_per_million="1.20", output_limit=4096),)
    proposals = {name: CaseBudget.model_validate(value) for name, value in (budget_proposals or {}).items()}
    keys = [item.execution_id for item in plan.executions] if plan.executions else plan.cases
    if set(proposals) - set(keys):
        raise ValueError("Budget proposal refers to an unselected case.")
    for name in keys:
        limit = execution.get(name, {}).get("case_limits", {}).get(name, 30)
        proposals.setdefault(name, CaseBudget(request_limit=limit, cost_limit_microusd=limit * max(item.reservation_microusd for item in prices)))
    for item in plan.executions:
        proposals[item.execution_id] = CaseBudget(request_limit=item.request_cap, cost_limit_microusd=item.cost_cap_microusd)
    bundle = PreparedBundle(root=root, source_root=SOURCE, plan=plan, files=files, binding="", execution=execution, environment=environment,
        eligibility=routes, budget_proposals=proposals, prices=prices)
    bundle.binding = bundle_fingerprint(bundle)
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SOURCE / name).read_bytes())
    (root / "prepared.json").write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
    ledger = _LiveLedger(version=2, integrity="", binding=bundle.binding, authorization_binding=None, stopped=False, attempts=[])
    ledger.integrity = digest(ledger.model_dump(mode="json", exclude={"integrity", "candidate_codec"} if ledger.candidate_codec is None else {"integrity"}))
    (root / "ledger.json").write_text(ledger.model_dump_json(indent=2, exclude={"candidate_codec"} if ledger.candidate_codec is None else None) + "\n", encoding="utf-8")
    return bundle


def bundle_material_root(bundle: PreparedBundle) -> Path:
    return bundle.root if bundle.candidate_directory is None else bundle.root / ".candidates" / bundle.candidate_directory


def candidate_plan_matches(previous: PreparedBundle, plan: AcceptancePlan, *, material_root: Path = SOURCE,
                           source_reader: Callable[[Path], bytes] | None = None) -> bool:
    """Only reviewed rubric/package identity may change; tasks and all caps persist."""
    if plan == previous.plan:
        return True
    old = previous.plan
    if (old.quality_packet is None or plan.quality_packet is None
            or old.model_dump(exclude={"executions", "quality_packet"}) != plan.model_dump(exclude={"executions", "quality_packet"})
            or len(old.executions) != len(plan.executions)
            or any(left.model_dump(exclude={"material_path"}) != right.model_dump(exclude={"material_path"})
                for left,right in zip(old.executions, plan.executions))):
        return False
    from tests.support.agent_acceptance.quality import QualityMaterials, read_quality_manifest
    def manifest(path: Path) -> Any:
        if source_reader is None:
            return read_quality_manifest(path)
        raw = source_reader(path)
        value = QualityMaterials.model_validate_json(raw)
        if digest({key: body for key, body in json.loads(raw).items() if key != "binding"}) != value.binding:
            raise SharedControlError("Archived quality manifest identity changed.")
        return value
    old_materials = manifest(bundle_material_root(previous) / old.quality_packet.path)
    new_materials = manifest(material_root / plan.quality_packet.path)
    new_bytes = source_reader(material_root / plan.quality_packet.path) if source_reader else (material_root / plan.quality_packet.path).read_bytes()
    if (sha256(new_bytes).hexdigest() != plan.quality_packet.sha256
            or old_materials.model_dump(exclude={"source_path", "source_version", "criteria_version", "standards", "tasks", "files", "binding", "usage_authority"})
                != new_materials.model_dump(exclude={"source_path", "source_version", "criteria_version", "standards", "tasks", "files", "binding", "usage_authority"})
            or (old_materials.usage_authority or {}).get("path") != (new_materials.usage_authority or {}).get("path")
            or len(old_materials.tasks) != len(new_materials.tasks)):
        return False
    for left,right in zip(old_materials.tasks,new_materials.tasks):
        if (left.model_dump(exclude={"input_path", "gold_path", "domain_standards"}) != right.model_dump(exclude={"input_path", "gold_path", "domain_standards"})
                or old_materials.files[left.input_path.as_posix()] != new_materials.files[right.input_path.as_posix()]
                or old_materials.files[left.gold_path.as_posix()] != new_materials.files[right.gold_path.as_posix()]):
            return False
    for kind,standard in old_materials.standards.items():
        changed = new_materials.standards.get(kind, {}).get("dimensions", {})
        for name,dimension in standard.get("dimensions", {}).items():
            if dimension.get("critical") and (changed.get(name, {}).get("critical") is not True
                    or changed[name].get("applicable") != dimension.get("applicable")):
                return False
    return True


def read_prepared_snapshot(root: Path, *, binding: str | None = None) -> PreparedBundle:
    """Read immutable genesis/candidate originals without claiming current-source readiness."""
    return _read_prepared_storage(root, root, binding=binding)


def read_prepared_archive(storage: Path, *, binding: str | None = None) -> PreparedBundle:
    """Verify preserved originals offline; this does not create a live gate or authority."""
    try:
        with zipfile.ZipFile(storage) as archive:
            original = PreparedBundle.model_validate_json(archive.read("prepared.json"))
    except (OSError, ValueError) as error:
        raise SharedControlError(f"Archived genesis cannot be read: {error}") from error
    return _read_prepared_storage(original.root, storage, binding=binding)


def _read_prepared_storage(root: Path, storage: Path, *, binding: str | None) -> PreparedBundle:
    frozen_archive: zipfile.ZipFile | None = None
    def document(path: Path) -> bytes:
        return frozen_archive.read(path.relative_to(storage).as_posix()) if frozen_archive is not None else path.read_bytes()
    def material_bytes(snapshot: Path, name: str) -> bytes:
        if frozen_archive is not None:
            return document(snapshot / name)
        if storage == root:
            return (snapshot / name).read_bytes()
        with zipfile.ZipFile(snapshot / "source.zip") as archive:
            return archive.read(name)
    def archived_source(path: Path) -> bytes:
        relative = path.relative_to(storage)
        if relative.parts[0] == ".candidates":
            return material_bytes(storage / Path(*relative.parts[:2]), Path(*relative.parts[2:]).as_posix())
        return material_bytes(storage, relative.as_posix())
    try:
        if storage != root:
            frozen_archive = zipfile.ZipFile(storage)
        original = TypeAdapter(dict[str, Any]).validate_json(document(storage / "ledger.json"))
        if original.get("integrity") != digest({key: value for key, value in original.items() if key != "integrity"}):
            raise SharedControlError("Live accounting integrity changed; no terminal HTTP sent.")
        ledger = _LiveLedger.model_validate(original)
        previous_binding = ledger.binding
        previous_bundle = PreparedBundle.model_validate_json(document(storage / "prepared.json"))
        if previous_bundle.binding != ledger.binding or bundle_fingerprint(previous_bundle) != ledger.binding:
            raise SharedControlError("Genesis prepared original changed; no candidate can replace it.")
        if ledger.candidates and any(sha256(material_bytes(storage, name)).hexdigest() != expected for name, expected in previous_bundle.files.items()):
            raise SharedControlError("Genesis frozen source originals changed.")
        active: dict[str, str] = {}
        activation_number = 1
        for item in ledger.candidates:
            record_root = storage / ".candidates" / item["directory"]
            decision_path = storage / item["review_path"]
            if (not record_root.resolve().is_relative_to(storage.resolve())
                    or any(part.is_symlink() or part.is_junction() for part in (record_root, *record_root.parents))
                    or decision_path != record_root / "candidate-review.json"
                    or sha256(document(decision_path)).hexdigest() != item["review_sha256"]):
                raise SharedControlError("Candidate review location or original changed.")
            decision = CandidateFreezeReview.model_validate_json(document(decision_path))
            record = PreparedBundle.model_validate_json(document(record_root / "prepared.json"))
            grant = LiveAuthorization.model_validate(item["grant"])
            if (not activation_number <= item["before_request"] <= len(ledger.attempts) + 1
                    or item["previous_binding"] != previous_binding or decision.previous_binding != previous_binding
                    or item["candidate_binding"] != decision.candidate_binding or record.binding != decision.candidate_binding
                    or record.candidate_directory != item["directory"] or record.root != root.resolve()
                    or bundle_fingerprint(record) != record.binding or grant.binding != record.binding
                    or not candidate_plan_matches(previous_bundle.model_copy(update={"root": storage}), record.plan, material_root=record_root,
                        source_reader=archived_source if storage != root else None)
                    or sorted(decision.affected_execution_ids) != item["affected_execution_ids"]
                    or decision.accounting_root.resolve() != root.resolve() or record.plan.parent_budget is None
                    or decision.parent_id != record.plan.parent_budget.parent_id
                    or ledger.authorization is None
                    or grant.model_dump(exclude={"binding", "authorization_id"}) != ledger.authorization.model_dump(exclude={"binding", "authorization_id"})
                    or any(sha256(material_bytes(record_root, name)).hexdigest() != expected for name,expected in record.files.items())):
                raise SharedControlError("Candidate chain differs from its exact reviewed originals.")
            active.update({name: record.binding for name in decision.affected_execution_ids})
            activation_number = item["before_request"]
            previous_binding = record.binding
            previous_bundle = record
        if active != ledger.active_candidates:
            raise SharedControlError("Active candidate map differs from the reviewed freeze chain.")
        selected = binding or (ledger.candidates[-1]["candidate_binding"] if ledger.candidates else ledger.binding)
        candidate = next((item for item in ledger.candidates if item["candidate_binding"] == selected), None)
        snapshot = storage if selected == ledger.binding else storage / ".candidates" / candidate["directory"] if candidate else None
        if snapshot is None or any(part.is_symlink() or part.is_junction() for part in (snapshot, *snapshot.parents)):
            raise SharedControlError("Candidate binding is missing or uses a redirected path.")
        bundle = PreparedBundle.model_validate_json(document(snapshot / "prepared.json"))
        if (bundle.root != root.resolve() or storage / bundle_material_root(bundle).relative_to(root) != snapshot or bundle.binding != selected
                or bundle_fingerprint(bundle) != bundle.binding or bundle.sending_enabled):
            raise SharedControlError("Prepared candidate identity changed; no connection acquired.")
        if candidate is not None:
            review = storage / candidate["review_path"]
            if (not review.resolve().is_relative_to(storage.resolve()) or frozen_archive is None and not review.is_file()
                    or sha256(document(review)).hexdigest() != candidate["review_sha256"]):
                raise SharedControlError("Candidate review original changed; no connection acquired.")
        if any(sha256(material_bytes(snapshot, name)).hexdigest() != expected
               for name, expected in bundle.files.items()):
            raise SharedControlError("Frozen material binding changed; no connection acquired.")
        return bundle
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise SharedControlError(f"Prepared execution binding cannot be read: {error}") from error

    finally:
        if frozen_archive is not None:
            frozen_archive.close()

def verify_prepared(root: Path, *, registry: CaseRegistry, binding: str | None = None) -> PreparedBundle:
    from tests.support.agent_acceptance.reporting import environment_facts
    bundle = read_prepared_snapshot(root, binding=binding)
    if (not bundle.eligibility or not bundle.budget_proposals or not bundle.prices
            or SOURCE not in (bundle.source_root, bundle_material_root(bundle))
            or bound_files(bundle.plan, registry) != bundle.files or environment_facts() != bundle.environment):
        raise SharedControlError("Prepared execution binding changed; no connection acquired.")
    return bundle


class LiveAuthorization(BaseModel):
    """An explicitly supplied grant; preparation never creates one."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    authorization_id: str = Field(min_length=1)
    binding: str
    total_limit: int = Field(gt=0, strict=True)
    case_limits: dict[str, Annotated[int, Field(gt=0, strict=True)]]
    total_microusd: int | None = Field(default=None, gt=0, strict=True)
    case_microusd: dict[str, Annotated[int, Field(gt=0, strict=True)]] = Field(default_factory=dict)
    transport: Literal["real", "mock"] = "real"
    connection_profile: Path | None = None
    parent_budget_id: str | None = None


class LiveExecutionRequest(BaseModel):
    """Explicit input for the same normal check entry; never created by prepare."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    run_root: Path
    grant: LiveAuthorization
    case_id: str | None = None
    protocol: Protocol | None = None
    execution_ids: tuple[Annotated[str, Field(min_length=1)], ...] | None = None

    @model_validator(mode="after")
    def paired_selection(self) -> "LiveExecutionRequest":
        if (self.case_id is None) != (self.protocol is None):
            raise ValueError("A single live case and protocol must be selected together.")
        if self.execution_ids is not None and (self.case_id is not None or not self.execution_ids or len(set(self.execution_ids)) != len(self.execution_ids)):
            raise ValueError("A stage requires distinct execution IDs and cannot be combined with a case selection.")
        return self


class _ReservedAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    number: int = Field(ge=1)
    authorization_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    protocol: Protocol
    actor: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    delegation_id: str | None = None
    product_number: int | None = None
    product_step_id: str | None = None
    execution_id: str
    role: Literal["product", "judge"] = "product"
    judge_call_id: str | None = None
    reservation_microusd: int = Field(ge=0)
    occupied_microusd: int = Field(ge=0)
    state: Literal["reserved", "not_sent", "http_started", "response_complete", "unknown"] = "reserved"
    sdk_input_id: str | None = None
    request_sha256: str | None = None
    fault_origin: str | None = None
    usage_estimate_microusd: int | None = None
    supplier_final_microusd: int | None = None
    price_version: str = "offline-2026-11-16"
    stage: str = ""


class _CandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    previous_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    directory: str = Field(pattern=r"^[a-f0-9]{32}$")
    grant: LiveAuthorization
    review_path: str = Field(min_length=1)
    review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    affected_execution_ids: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    recorded_at: datetime
    before_request: int = Field(ge=1)


class _LiveLedger(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[2]
    integrity: str
    authorization: LiveAuthorization | None = None
    binding: str
    authorization_binding: str | None
    stopped: bool
    stop_reason: str | None = None
    case_stops: dict[str, dict[str, str]] = Field(default_factory=dict)
    product_request_owners: dict[str, dict[str, Any]] = Field(default_factory=dict)
    product_task_stops: dict[str, dict[str, str]] = Field(default_factory=dict)
    attempts: list[_ReservedAttempt]
    execution_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    phase_microusd: dict[str, int] = Field(default_factory=dict)
    reserve_microusd: int = 0
    transfers: list[dict[str, Any]] = Field(default_factory=list)
    scope_stops: list[dict[str, Any]] = Field(default_factory=list)
    candidate_codec: Literal["active-candidates-v1"] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    active_candidates: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def candidate_codec_binding(self) -> "_LiveLedger":
        if self.candidates and self.candidate_codec != "active-candidates-v1":
            raise ValueError("Active candidate records require their explicit fail-closed reader codec.")
        return self

    @field_validator("candidates")
    @classmethod
    def candidate_records(cls, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for value in values:
            _CandidateRecord.model_validate(value)
        return values


class CandidateFreezeReview(BaseModel):
    """An explicit decision about exact candidate bytes, not a generated approval."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    parent_id: str = Field(min_length=1)
    accounting_root: Path
    previous_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    affected_execution_ids: tuple[str, ...]
    tuning_execution_ids: tuple[str, ...]
    tuning_results: dict[str, str]
    untuned_materials: dict[str, str]
    review_kind: Literal["synthetic_control", "human_review"]
    reviewer: str = Field(min_length=1)
    reason: str = Field(min_length=1)


ACTIVE_CONTROLS: dict[Path, list[tuple[str | None, CancellationToken]]] = {}

P = ParamSpec("P")
T = TypeVar("T")


def ledger_mutation(operation: Callable[Concatenate["LiveGate", P], T]) -> Callable[Concatenate["LiveGate", P], T]:
    @wraps(operation)
    def locked(self: "LiveGate", /, *args: P.args, **kwargs: P.kwargs) -> T:
        with self.transaction():
            return operation(self, *args, **kwargs)
    return locked


@dataclass
class LiveGate:
    root: Path
    registry: CaseRegistry
    authorization: LiveAuthorization | None = None
    bound_execution_id: str | None = None

    @property
    def material_root(self) -> Path:
        return bundle_material_root(self.prepared())

    def prepared(self) -> PreparedBundle:
        return verify_prepared(self.root, registry=self.registry,
            binding=self.authorization.binding if self.authorization is not None else None)

    def propose_candidate(self, *, new_plan: AcceptancePlan | None = None) -> PreparedBundle:
        """Freeze a closed draft under the same root; this grants no sending authority."""
        from uuid import uuid4
        from tests.support.agent_acceptance.reporting import environment_facts
        original = read_prepared_snapshot(self.root,
            binding=self.authorization.binding if self.authorization is not None else None)
        if new_plan is not None and not candidate_plan_matches(original, new_plan):
            raise SharedControlError("Candidate plan substitution changes the original frozen identity.")
        if original.plan.parent_budget is None or not original.plan.executions:
            raise SharedControlError("Candidate freeze requires the original frozen parent.")
        deepseek_standard: RouteStandardVersion = next((route.standard_version for route in original.eligibility
            if route.protocol == "deepseek" and route.role == "product"), "2026-11-16")
        plan = new_plan or original.plan
        registry = self.registry
        if plan.quality_packet is not None:
            from tests.support.agent_acceptance import builtin_registry
            registry = builtin_registry(quality_packet=plan.quality_packet)
        directory = uuid4().hex
        content = self.root / ".candidates" / directory
        content.mkdir(parents=True, exist_ok=False)
        execution: dict[str, Any] = {}
        preparations: dict[Any, dict[str, Any]] = {}
        for case, _ in registry.select(plan):
            if case.prepare is not None and case.case_id not in execution:
                if case.prepare not in preparations:
                    preparations[case.prepare] = case.prepare(content)
                execution[case.case_id] = preparations[case.prepare]
        bundle = original.model_copy(update={"source_root": SOURCE, "candidate_directory": directory,
            "plan": plan, "files": bound_files(plan, registry), "execution": execution,
            "environment": environment_facts(),
            "eligibility": preparation_eligibility(deepseek_standard=deepseek_standard), "binding": ""})
        bundle.binding = bundle_fingerprint(bundle)
        for name in bundle.files:
            target = content / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((SOURCE / name).read_bytes())
        (content / "prepared.json").write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return bundle

    @ledger_mutation
    def refreeze_candidate(self, candidate: PreparedBundle, *, review: Path, review_sha256: str,
                           grant: LiveAuthorization) -> "LiveGate":
        """Activate a reviewed draft without changing genesis grant, counters or stops."""
        ledger = self._read_ledger()
        previous = read_prepared_snapshot(self.root,
            binding=self.authorization.binding if self.authorization is not None else None)
        parent = previous.plan.parent_budget
        current = ledger.candidates[-1]["candidate_binding"] if ledger.candidates else ledger.binding
        if (parent is None or self.authorization is None or previous.binding != current
                or candidate.root != self.root.resolve() or candidate.candidate_directory is None
                or not candidate_plan_matches(previous, candidate.plan, material_root=bundle_material_root(candidate))
                or candidate.budget_proposals != previous.budget_proposals
                or candidate.prices != previous.prices or candidate.sending_enabled
                or candidate.binding != bundle_fingerprint(candidate) or ledger.stopped
                or any(item.state in {"reserved", "http_started"} for item in ledger.attempts)):
            raise SharedControlError("Candidate freeze changed parent/plan/caps or has a stopped/inflight predecessor.")
        content = bundle_material_root(candidate)
        registry = self.registry
        if candidate.plan.quality_packet is not None:
            from tests.support.agent_acceptance import builtin_registry
            registry = builtin_registry(quality_packet=candidate.plan.quality_packet)
        if (any(part.is_symlink() or part.is_junction() for part in (content, *content.parents))
                or PreparedBundle.model_validate_json((content / "prepared.json").read_bytes()) != candidate
                or bound_files(candidate.plan, registry) != candidate.files
                or any(sha256((content / name).read_bytes()).hexdigest() != expected for name, expected in candidate.files.items())):
            raise SharedControlError("Proposed candidate bytes changed; no connection acquired.")
        if not review.resolve().is_relative_to(self.root.resolve()) or not review.is_file() or sha256(review.read_bytes()).hexdigest() != review_sha256:
            raise SharedControlError("Candidate freeze requires its exact preserved review original.")
        decision = CandidateFreezeReview.model_validate_json(review.read_bytes())
        rows = {item.execution_id: item for item in previous.plan.executions}
        tuning = {name for name,item in rows.items() if item.stage == "quality" and ".tuning." in item.scenario_id}
        affected = set(decision.affected_execution_ids)
        if (decision.parent_id != parent.parent_id or decision.accounting_root.resolve() != self.root.resolve()
                or decision.previous_binding != previous.binding or decision.candidate_binding != candidate.binding
                or not affected or len(affected) != len(decision.affected_execution_ids) or not affected <= set(rows)
                or set(decision.tuning_execution_ids) != tuning or not tuning
                or any(name not in ledger.execution_results for name in tuning)
                or decision.tuning_results != {name: sha256(json.dumps(ledger.execution_results[name], sort_keys=True).encode()).hexdigest() for name in tuning}
                or decision.untuned_materials != {name: rows[name].material_sha256 for name in affected}
                or any(rows[name].stage != "quality" or ".formal." not in rows[name].scenario_id for name in affected)
                or any(name in ledger.execution_results or any(item.execution_id == name for item in ledger.attempts) for name in affected)
                or any(name in ledger.case_stops for name in affected)
                or any(not item["expected_block"] and affected.intersection(item.get("execution_ids", [])) for item in ledger.scope_stops)
                or any(any(key.startswith(name + ":") for key in ledger.product_task_stops) for name in affected)):
            raise SharedControlError("Candidate review lacks closed tuning/unseen formal originals or revives a stopped scope.")
        original_grant = ledger.authorization or self.authorization
        if (grant.binding != candidate.binding or grant.model_dump(exclude={"binding", "authorization_id"}) != original_grant.model_dump(exclude={"binding", "authorization_id"})
                or grant.authorization_id in {original_grant.authorization_id, *(item["grant"]["authorization_id"] for item in ledger.candidates)}
                or (grant.transport == "mock") != (decision.review_kind == "synthetic_control")):
            raise SharedControlError("Candidate grant changes original authorization caps or identity.")
        self._parent_binding(previous, self.authorization, activate=not ledger.attempts, required=bool(ledger.attempts))
        if ledger.authorization is None:
            ledger.authorization = self.authorization
            ledger.authorization_binding = digest(self.authorization.model_dump(mode="json"))
        saved_review = content / "candidate-review.json"
        with saved_review.open("xb") as output:
            output.write(review.read_bytes())
        ledger.candidate_codec = "active-candidates-v1"
        ledger.candidates.append({"previous_binding": previous.binding, "candidate_binding": candidate.binding,
            "directory": candidate.candidate_directory, "grant": grant.model_dump(mode="json"),
            "review_path": saved_review.relative_to(self.root).as_posix(), "review_sha256": review_sha256,
            "affected_execution_ids": sorted(affected), "recorded_at": datetime.now(timezone.utc).isoformat(),
            "before_request": len(ledger.attempts) + 1})
        ledger.active_candidates.update({name: candidate.binding for name in affected})
        self._save_ledger(ledger)
        return replace(self, registry=registry, authorization=grant)

    def validate_plan(self, plan: AcceptancePlan) -> None:
        bundle = self.prepared()
        if (plan.executions or bundle.plan.executions) and plan.model_dump(exclude={"mode"}) != bundle.plan.model_dump(exclude={"mode"}):
            raise SharedControlError("Frozen plan changed: execution identities, order, dependencies, caps and material bindings are immutable.")

    def parent_binding_path(self, parent_id: str) -> Path:
        return project_root(SOURCE) / ".scratch/acceptance-parent-budgets" / (sha256(parent_id.encode()).hexdigest() + ".json")

    def _parent_binding(self, bundle: PreparedBundle, grant: LiveAuthorization, *, activate: bool,
                        required: bool) -> None:
        parent = bundle.plan.parent_budget
        if parent is None:
            return
        path = self.parent_binding_path(parent.parent_id)
        if bundle.candidate_directory is not None:
            ledger = self._read_ledger()
            bundle = read_prepared_snapshot(self.root, binding=ledger.binding)
            if ledger.authorization is None:
                raise SharedControlError("Candidate lost its original parent authorization.")
            grant = ledger.authorization
        expected = {"version":1,"parent_id":parent.parent_id,"accounting_root":str(bundle.root.resolve()),
            "binding":bundle.binding,"authorization_binding":digest(grant.model_dump(mode="json")),
            "project_root":str(project_root(SOURCE)),"owner":"acceptance-parent-control",
            "purpose":"Bind this activated parent to its original accounting root; preparation grants no authority.",
            "delete_when":"Persistent control state; retain with the original grant and ledger until their authorized lifecycle ends."}
        if activate and not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True)
            try:
                with path.open("x",encoding="utf-8") as output:
                    output.write(json.dumps(expected,ensure_ascii=False,indent=2)+"\n")
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                pass
            except OSError as error:
                raise SharedControlError(f"Parent accounting activation cannot be saved: {error}") from error
        if required or path.exists():
            try:
                original=json.loads(path.read_bytes())
            except (OSError,ValueError) as error:
                raise SharedControlError(f"Parent accounting activation is missing or damaged: {error}") from error
            if original != expected:
                raise SharedControlError("Parent accounting is already bound to its original root and authorization; no replacement budget is issued.")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize read-check-save against the same parent ledger in all processes."""
        import msvcrt
        with (self.root / "ledger-owner.lock").open("a+b") as lock:
            if (self.root / "ledger-owner.lock").stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as error:
                raise SharedControlError("Parent ledger transaction is unavailable; no terminal send allowed.") from error
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    @contextmanager
    def running(self, token: CancellationToken) -> Iterator[None]:
        controls = ACTIVE_CONTROLS.setdefault(self.root.resolve(), [])
        entry = (self.bound_execution_id, token)
        controls.append(entry)
        try:
            yield
        finally:
            controls.remove(entry)
            if not controls:
                ACTIVE_CONTROLS.pop(self.root.resolve(), None)

    @ledger_mutation
    def request_stop(self, *, scope: Literal["execution", "protocol", "shared"], reason: str,
                     source: Path, case_id: str, protocol: Protocol, expected_block: bool = False) -> None:
        bundle, grant, ledger = self.validate(case_id=case_id, protocol=protocol)
        if not source.is_file() or not reason.strip():
            raise SharedControlError("Stop requires the actual preserved observation and a reason.")
        event = {"scope":scope,"reason":reason,"source":str(source),"source_sha256":sha256(source.read_bytes()).hexdigest(),
            "expected_block":expected_block,"observed_at":datetime.now(timezone.utc).isoformat(),
            "partial_effects_check_required":not expected_block}
        ledger.scope_stops.append(event)
        affected = {item.execution_id for item in bundle.plan.executions if scope == "shared"
            or scope == "protocol" and item.protocol == protocol or scope == "execution" and item.execution_id == self.bound_execution_id}
        event["execution_ids"] = sorted(affected)
        event["case_id"],event["protocol"] = case_id,protocol
        if not expected_block:
            if bundle.plan.executions:
                for identity in affected:
                    ledger.case_stops.setdefault(identity,{"reason":reason,"stop_scope":scope,
                        "unfinished":"Remaining work was not authorized to send."})
            else:
                ledger.case_stops[self.stop_key(case_id, protocol)] = {"reason":reason,"stop_scope":scope}
            if scope == "shared":
                ledger.stopped, ledger.stop_reason = True, reason
            event["inflight_requests"] = [item.number for item in ledger.attempts if item.state in {"reserved","http_started"}
                and (not bundle.plan.executions or item.execution_id in affected)]
        if ledger.authorization is None:
            ledger.authorization, ledger.authorization_binding = grant, digest(grant.model_dump(mode="json"))
        self._save_ledger(ledger)
        if not expected_block:
            for active_identity, token in ACTIVE_CONTROLS.get(self.root.resolve(), []):
                if scope == "shared" or active_identity in affected or not bundle.plan.executions:
                    token.cancel()

    def accepted_stop(self, *, case_id: str, protocol: Protocol, request_number: int | None = None) -> bool:
        ledger=self._read_ledger()
        owner = ledger.product_request_owners.get(str(request_number)) if request_number is not None else None
        task_stopped = bool(owner and f"{owner['execution_id']}:{owner['task_id']}" in ledger.product_task_stops)
        return task_stopped or ledger.stopped or any(not event["expected_block"] and
            (self.bound_execution_id in event.get("execution_ids",[]) if self.bound_execution_id is not None else
                event["scope"] == "shared" or event["protocol"] == protocol and
                (event["scope"] == "protocol" or event["case_id"] == case_id)) for event in ledger.scope_stops)

    @ledger_mutation
    def transfer_reserve(self, *, stage: str, delta_microusd: int, basis: Path) -> None:
        bundle = self.prepared()
        if not bundle.plan.executions or bundle.plan.parent_budget is None:
            raise SharedControlError("Reserve transfers require the frozen parent budget.")
        saved = self._read_ledger()
        active = next((row for row in bundle.plan.executions
            if (self.bound_execution_id is None or row.execution_id == self.bound_execution_id)
            and saved.active_candidates.get(row.execution_id, saved.binding) == bundle.binding), None)
        if active is None:
            raise SharedControlError("Reserve transfer requires an execution active under this candidate.")
        _, grant, ledger = self.for_execution(active.execution_id).validate(case_id=active.case_id, protocol=active.protocol)
        parent = bundle.plan.parent_budget
        if not ledger.phase_microusd:
            ledger.phase_microusd = dict(parent.phase_microusd)
            ledger.reserve_microusd = parent.reserve_microusd
        if (stage not in ledger.phase_microusd or type(delta_microusd) is not int
                or not 0 < delta_microusd <= ledger.reserve_microusd):
            raise SharedControlError("Frozen reserve is insufficient or transfer is invalid.")
        if not basis.resolve().is_relative_to(self.root.resolve()) or not basis.is_file():
            raise SharedControlError("Reserve transfer basis must be a preserved original under the parent root.")
        ledger.phase_microusd[stage] += delta_microusd
        ledger.reserve_microusd -= delta_microusd
        ledger.transfers.append({"stage":stage,"delta_microusd":delta_microusd,"basis":str(basis),
            "basis_sha256":sha256(basis.read_bytes()).hexdigest(),"before_request":len(ledger.attempts)+1,
            "recorded_at":datetime.now(timezone.utc).isoformat()})
        if ledger.authorization is None:
            ledger.authorization, ledger.authorization_binding = grant, digest(grant.model_dump(mode="json"))
        self._save_ledger(ledger)

    def inspect_frozen_material(self, request: httpx2.Request, *, material_root: Path | None = None) -> None:
        bundle = self.prepared()
        execution = next((item for item in bundle.plan.executions if item.execution_id == self.bound_execution_id), None)
        if execution is None:
            return
        material = (bundle_material_root(bundle) / execution.material_path).read_text(encoding="utf-8").strip()
        materials: tuple[str, ...]=(material,)
        if bundle.plan.quality_packet is not None:
            from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput, model_prompts
            if material_root is not None:
                from tests.support.agent_acceptance.formal import verify_authority
                authority, authorized_gate = verify_authority(Path(os.environ["MYRK_ACCEPTANCE_AUTHORITY"]))
                if authority.scenario_root != material_root or authorized_gate.authorization != self.authorization or authorized_gate.bound_execution_id != self.bound_execution_id:
                    raise SharedControlError("Actual quality material root differs from the verified formal authority.")
            materials = model_prompts(QualityVisibleInput.model_validate_json(material), root=material_root)
        body = json.loads(request.content)
        def strings(value: Any) -> Iterator[str]:
            if isinstance(value, str):
                yield value
                if value.startswith(("{", "[")):
                    try:
                        decoded = json.loads(value)
                    except ValueError:
                        return
                    yield from strings(decoded)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
        if not any(expected and expected in value for expected in materials for value in strings(body.get("contents", body.get("messages", [])))):
            raise SharedControlError("Frozen material did not enter the actual native model input; labels cannot replace a variant.")

    def for_execution(self, execution_id: str) -> "LiveGate":
        return replace(self, bound_execution_id=execution_id)

    def stop_key(self, case_id: str, protocol: Protocol) -> str:
        return self.bound_execution_id or f"{case_id}:{protocol}"

    def recorded_outcome(self) -> Any:
        from tests.support.agent_acceptance.core import CaseOutcome
        ledger = self._read_ledger()
        saved = ledger.execution_results.get(self.bound_execution_id or "")
        if saved is None:
            return None
        return CaseOutcome(**{**saved, "evidence": {name: Path(path) for name, path in saved["evidence"].items()}})

    @ledger_mutation
    def finish_execution(self, outcome: Any) -> None:
        if self.bound_execution_id is None:
            return
        ledger = self._read_ledger()
        if self.bound_execution_id in ledger.execution_results:
            raise SharedControlError("Frozen execution result cannot be replaced.")
        ledger.execution_results[self.bound_execution_id] = {**vars(outcome), "evidence": {name:str(path) for name,path in outcome.evidence.items()}}
        self._save_ledger(ledger)

    def inspect_native_request(self, request: httpx2.Request, *, protocol: Protocol,
                               role: Literal["product", "judge"], config: ProviderConfig,
                               now: datetime | None = None) -> NativeEligibilityObservation:
        """Observe the final request without acquiring a connection or authorizing a send."""
        bundle = self.prepared()
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            raise ValueError("Eligibility observation time requires a timezone.")
        routes = [item for item in bundle.eligibility if item.protocol == protocol and item.role == role]
        unresolved = [gap.code for item in routes for gap in item.unresolved]
        current = bool(routes)
        match = False
        if len(routes) == 1:
            route = routes[0]
            try:
                body = TypeAdapter(dict[str, Any]).validate_json(request.content)
            except ValueError as error:
                raise SharedControlError("Native request is not a JSON object; no terminal send allowed.") from error
            current = (config == route.configuration and (route.expires_at is None or observed < route.expires_at))
            if config != route.configuration:
                unresolved.append("configuration_changed")
            if route.expires_at is not None and observed >= route.expires_at:
                unresolved.append("route_expired")
            if protocol == "google":
                generation = body.get("generationConfig", {})
                if not isinstance(generation, dict):
                    raise SharedControlError("Native generationConfig is not an object; no terminal send allowed.")
                url = str(request.url).split("?", 1)[0]
                endpoints = {route.endpoint + "/v1beta/models/" + route.model + ":" + operation
                             for operation in ("generateContent", "streamGenerateContent")}
                candidate_count = generation.get("candidateCount", generation.get("candidate_count", 1))
                max_output_tokens = generation.get("maxOutputTokens")
                match = (request.method == "POST" and url in endpoints
                         and "generation_config" not in body
                         and not {"candidateCount", "candidate_count"}.issubset(generation)
                         and type(candidate_count) is int and candidate_count == 1
                         and "thinking_config" not in generation
                         and generation.get("thinkingConfig") == route.thinking
                         and "max_output_tokens" not in generation
                         and type(max_output_tokens) is int and 0 < max_output_tokens <= route.total_output_limit)
            else:
                max_tokens = body.get("max_tokens")
                unsupported_title = False
                if route.standard_version == "2026-11-26":
                    thinking = body.get("thinking")
                    thinking_match = (route.entry_point == "ordinary_new_chat"
                        and body.get("reasoning_effort") == route.thinking.get("reasoning_effort") == "high"
                        and (thinking is None or isinstance(thinking, dict) and thinking.get("type") == "enabled"))
                    output_match = type(max_tokens) is int and max_tokens == route.total_output_limit
                    title = body.get("title")
                    unsupported_title = "title" in body and (not isinstance(title, dict) or title.get("enabled") is not False)
                else:
                    thinking_match = deepseek_thinking_disabled(body)
                    output_match = type(max_tokens) is int and 0 < max_tokens <= route.total_output_limit
                match = (request.method == "POST" and str(request.url) == route.endpoint + "/chat/completions"
                         and body.get("model") == route.model and thinking_match and output_match)
                tools = body.get("tools")
                safety_check = body.get("safety_check")
                unsupported_tools = tools is not None and (
                    not isinstance(tools, list)
                    or any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools)
                )
                nondefault_safety = safety_check is not None and (
                    not isinstance(safety_check, dict) or safety_check.get("level") != "low"
                )
                if unsupported_tools or nondefault_safety or unsupported_title:
                    match = False
                    unresolved.append("non_text_or_paid_feature")
            native_nodes = [body]
            if protocol == "google":
                native_nodes.append(generation)
                tools = body.get("tools", [])
                if isinstance(tools, list):
                    native_nodes.extend(item for item in tools if isinstance(item, dict))
                contents = body.get("contents", [])
                if isinstance(contents, list):
                    for content in [body.get("systemInstruction"), *contents]:
                        if isinstance(content, dict) and isinstance(content.get("parts"), list):
                            native_nodes.extend(part for part in content["parts"] if isinstance(part, dict))
            else:
                messages = body.get("messages", [])
                if isinstance(messages, list):
                    for message in messages:
                        if isinstance(message, dict) and isinstance(message.get("content"), list):
                            native_nodes.extend(part for part in message["content"] if isinstance(part, dict))
            restricted = {"googleSearch", "google_search", "googleSearchRetrieval", "google_search_retrieval",
                          "urlContext", "url_context", "codeExecution", "code_execution", "cachedContent", "cached_content",
                          "inlineData", "inline_data", "fileData", "file_data", "audioConfig", "audio_config",
                          "image_url", "input_audio", "video_url", "web_search_options"}
            for value in native_nodes:
                modalities = value.get("responseModalities", value.get("response_modalities"))
                if (restricted.intersection(value) or {"responseModalities", "response_modalities"}.issubset(value)
                        or (modalities is not None and modalities != ["TEXT"])):
                    match = False
                    unresolved.append("non_text_or_paid_feature")
                    break
            if not match:
                unresolved.append("native_settings_mismatch")
        else:
            unresolved.append("unsupported_paid_route")
        return NativeEligibilityObservation(request_sha256=sha256(request.content).hexdigest(), route_current=current,
            native_settings_match=match, unresolved=tuple(unresolved), manual_review_required=role == "judge")

    @contextmanager
    def owner(self) -> Iterator[None]:
        """Hold one OS lock for the complete formal execution, across processes."""
        import msvcrt

        with (self.root / "execution-owner.lock").open("a+b") as lock:
            if (self.root / "execution-owner.lock").stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise SharedControlError("Another formal execution owns this prepared run; no connection acquired.") from error
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def _read_ledger(self) -> _LiveLedger:
        try:
            original = TypeAdapter(dict[str, Any]).validate_json((self.root / "ledger.json").read_bytes())
            if original.get("integrity") != digest({key: value for key, value in original.items() if key != "integrity"}):
                raise SharedControlError("Live accounting integrity changed; no terminal HTTP sent.")
            return _LiveLedger.model_validate(original)
        except (OSError, ValueError) as error:
            raise SharedControlError(f"Live accounting cannot be read: {error}") from error

    def _save_ledger(self, ledger: _LiveLedger) -> None:
        ledger.integrity = digest(ledger.model_dump(mode="json", exclude={"integrity", "candidate_codec"} if ledger.candidate_codec is None else {"integrity"}))
        path = self.root / "ledger.json"
        pending = path.with_suffix(".pending")
        try:
            with pending.open("x", encoding="utf-8") as output:
                output.write(ledger.model_dump_json(indent=2, exclude={"candidate_codec"} if ledger.candidate_codec is None else None) + "\n")
                output.flush()
                os.fsync(output.fileno())
            pending.replace(path)
        except OSError as error:
            raise SharedControlError(f"Live accounting cannot be saved: {error}") from error

    def validate(self, *, case_id: str, protocol: Protocol) -> tuple[PreparedBundle, LiveAuthorization, _LiveLedger]:
        return self._validate(case_id=case_id, protocol=protocol, allow_stopped=False)

    def _validate(self, *, case_id: str, protocol: Protocol, allow_stopped: bool) -> tuple[PreparedBundle, LiveAuthorization, _LiveLedger]:
        bundle = self.prepared()
        grant = self.authorization
        if grant is None or grant.binding != bundle.binding:
            raise SharedControlError("Missing or mismatched live authorization; no connection acquired.")
        allowed = {(case.case_id, item) for case, item in self.registry.select(bundle.plan)}
        executions = {item.execution_id: item for item in bundle.plan.executions}
        if (case_id, protocol) not in allowed:
            raise SharedControlError("Request is outside the authorized case/protocol binding.")
        key = self.bound_execution_id if executions else case_id
        allowed_keys = set(executions) if executions else {name for name, _ in allowed}
        if executions and (key not in executions or (executions[key].case_id, executions[key].protocol) != (case_id, protocol)):
            raise SharedControlError("Request lacks its frozen independent execution identity.")
        if (set(grant.case_limits) - allowed_keys or grant.total_limit > sum(grant.case_limits.values())
                or (self.root / "ledger.pending").exists()):
            raise SharedControlError("Live accounting or granted limits differ from the frozen case plan.")
        if grant.transport == "real":
            routes = [item for item in bundle.eligibility if item.protocol == protocol and item.role == "product"]
            gaps = [gap.code for item in routes for gap in item.unresolved]
            raise SharedControlError("Paid route unqualified: " + ", ".join(gaps or ["unsupported_paid_route"]) +
                                     "; no connection acquired and no terminal HTTP sent.")
        assert key is not None
        proposal = bundle.budget_proposals[key]
        if (key not in grant.case_limits or grant.total_microusd is None or set(grant.case_microusd) != set(grant.case_limits)
                or grant.total_microusd > sum(grant.case_microusd.values())):
            raise SharedControlError("Missing explicit amount authorization; no connection acquired.")
        if (grant.case_limits.get(key, 0) > proposal.request_limit
                or grant.case_microusd.get(key, 0) > proposal.cost_limit_microusd):
            raise SharedControlError("Live authorization exceeds the frozen per-case send proposal.")
        parent = bundle.plan.parent_budget
        if executions and (parent is None or grant.parent_budget_id != parent.parent_id
                or grant.total_limit > parent.request_cap or grant.total_microusd > parent.total_microusd):
            raise SharedControlError("Parent budget identity or explicit grant exceeds the frozen parent.")
        ledger = self._read_ledger()
        attempts = ledger.attempts
        self._parent_binding(bundle,grant,activate=False,required=bool(attempts))
        grant_binding = digest(grant.model_dump(mode="json"))
        candidate = next((item for item in ledger.candidates if item["candidate_binding"] == bundle.binding), None)
        authorized_ids = {grant.authorization_id}
        if ledger.authorization is not None:
            authorized_ids.add(ledger.authorization.authorization_id)
        authorized_ids.update(item["grant"]["authorization_id"] for item in ledger.candidates)
        if ((ledger.stopped and not allow_stopped)
                or ledger.active_candidates.get(key, ledger.binding) != bundle.binding
                or (candidate is None and (ledger.binding != bundle.binding or ledger.authorization not in (None, grant)))
                or (candidate is not None and (candidate["grant"] != grant.model_dump(mode="json")
                    or ledger.active_candidates.get(key, ledger.binding) != bundle.binding))
                or ledger.authorization_binding != (digest(ledger.authorization.model_dump(mode="json")) if ledger.authorization is not None else None)
                or any(item.authorization_id not in authorized_ids or item.number != number
                       or (item.case_id, item.protocol) not in allowed for number, item in enumerate(attempts, 1))):
            raise SharedControlError("Live accounting is stopped or authorization changed.")
        validate_product_originals(ledger.product_request_owners)
        prices = {(item.protocol, item.role): item for item in bundle.prices}
        def attempt_key(item: _ReservedAttempt) -> str:
            return item.execution_id if executions else item.case_id
        if (len(attempts) > grant.total_limit or sum(item.occupied_microusd for item in attempts) > grant.total_microusd
                or any(sum(attempt_key(item) == name for item in attempts) > limit for name, limit in grant.case_limits.items())
                or any((item.execution_id not in executions or
                        (item.case_id, item.protocol, item.role, item.stage) != (executions[item.execution_id].case_id,
                            executions[item.execution_id].protocol, executions[item.execution_id].role, executions[item.execution_id].stage)) if executions
                        else item.execution_id != self.execution_id(item.case_id, item.protocol) for item in attempts)
                or any((item.protocol, item.role) not in prices
                    or item.reservation_microusd != prices[item.protocol, item.role].reservation_microusd
                    or item.price_version != prices[item.protocol, item.role].version
                    or item.occupied_microusd != (0 if item.state == "not_sent" else item.reservation_microusd)
                    or item.state == "not_sent" and not item.fault_origin for item in attempts)):
            raise SharedControlError("Live accounting amount, execution or price identity changed.")
        if parent is not None and ledger.phase_microusd:
            expected = dict(parent.phase_microusd)
            reserve = parent.reserve_microusd
            for transfer in ledger.transfers:
                expected[transfer["stage"]] += transfer["delta_microusd"]
                reserve -= transfer["delta_microusd"]
                basis = Path(transfer["basis"])
                if not basis.is_file() or sha256(basis.read_bytes()).hexdigest() != transfer["basis_sha256"]:
                    raise SharedControlError("Reserve accounting basis changed.")
            if ledger.phase_microusd != expected or ledger.reserve_microusd != reserve or reserve < 0:
                raise SharedControlError("Parent reserve accounting no longer conserves the frozen total.")
        if self.stop_key(case_id, protocol) in ledger.case_stops:
            raise CaseBudgetExceeded(ledger.case_stops[self.stop_key(case_id, protocol)]["reason"] + "; live case stopped; no connection acquired.")
        return bundle, grant, ledger

    def execution_id(self, case_id: str, protocol: Protocol) -> str:
        return self.bound_execution_id or f"{self.root.name}:{case_id}:{protocol}:1"

    @ledger_mutation
    def reserve(self, *, case_id: str, protocol: Protocol, actor: str, purpose: str,
                delegation_id: str | None = None, product_number: int | None = None,
                role: Literal["product", "judge"] = "product", product_step_id: str | None = None, sdk_input_id: str | None = None,
                request_sha256: str | None = None, judge_call_id: str | None = None, product_records_root: Path | None = None) -> int:
        bundle, grant, ledger = self.validate(case_id=case_id, protocol=protocol)
        product_owner = None
        if self.registry.cases[case_id].product_stop_scope == "task":
            record_root_allowed = product_records_root is not None and product_records_root.resolve().is_relative_to(bundle_material_root(bundle) / ".scratch")
            if bundle.candidate_directory is not None and bundle.plan.quality_packet is not None and case_id.startswith("quality."):
                from tests.support.agent_acceptance.formal import verify_authority
                authority_path = os.environ.get("MYRK_ACCEPTANCE_AUTHORITY")
                if authority_path is None:
                    raise SharedControlError("A quality candidate requires its actual paired data-root authority.")
                authority, actual_gate = verify_authority(Path(authority_path))
                record_root_allowed = (authority.scenario_root is not None and product_records_root == authority.scenario_root / "records"
                    and actual_gate.authorization == self.authorization and actual_gate.bound_execution_id == self.bound_execution_id
                    and authority.case_id == case_id and authority.protocol == protocol)
            if role != "product" or product_records_root is None or (not product_step_id and purpose != "compaction") or not record_root_allowed:
                raise SharedControlError("Task-scoped sends require the actual frozen product record root and RequestAttempt step.")
            from myrk.conversation import ConversationRecord
            matches = []
            for source in product_records_root.rglob("*.json"):
                if source.parent != product_records_root and not source.parent.name.endswith("-delegations"):
                    continue
                raw = source.read_bytes()
                record = ConversationRecord.model_validate_json(raw)
                for turn in record.turns:
                    if turn.task is None:
                        continue
                    requests = [*turn.request_attempts, *(request for action in turn.actions if action.review is not None for request in action.review.request_attempts)]
                    if any((request.number, request.step_id, request.actor, request.purpose, request.delegation_id) == (product_number, product_step_id, actor, purpose, delegation_id)
                            and (purpose != "compaction" or turn.status == "running" and request.status == "sent") for request in requests):
                        matches.append({"task_id": turn.task.id, "path": str(source), "sha256": sha256(raw).hexdigest(), "bytes": len(raw), "original_codec": "gzip-base64-v1", "original_compressed_base64": b64encode(gzip.compress(raw, mtime=0)).decode("ascii")})
            task_ids = {item["task_id"] for item in matches}
            if len(task_ids) != 1:
                raise SharedControlError("Native request does not resolve to one actual product Task; caller task labels cannot authorize a send.")
            task_id = next(iter(task_ids))
            product_owner = {"task_id": task_id, "execution_id": self.execution_id(case_id, protocol), "sources": matches}
            if f"{product_owner['execution_id']}:{task_id}" in ledger.product_task_stops:
                raise CaseBudgetExceeded("Actual product Task was stopped; a new step, actor or child request cannot resume it.")
        prices = [item for item in bundle.prices if item.protocol == protocol and item.role == role]
        if len(prices) != 1:
            raise SharedControlError("No frozen request price for this role/protocol.")
        price = prices[0].reservation_microusd
        attempts = ledger.attempts
        execution = next((item for item in bundle.plan.executions if item.execution_id == self.bound_execution_id), None)
        key = execution.execution_id if execution else case_id
        owned = [item for item in attempts if (item.execution_id == key if execution else item.case_id == key)]
        reason = None
        if (len(attempts) >= grant.total_limit or len(owned) >= grant.case_limits.get(key, 0)):
            reason = "request budget exhausted"
        assert grant.total_microusd is not None
        if (sum(item.occupied_microusd for item in attempts) + price > grant.total_microusd or
                sum(item.occupied_microusd for item in owned) + price > grant.case_microusd[key]):
            reason = "cost budget exhausted"
        parent = bundle.plan.parent_budget
        if execution is not None and parent is not None:
            if role != execution.role or key in ledger.execution_results:
                raise SharedControlError("Frozen execution role changed or already completed; no replacement send allowed.")
            if role == "judge" and (product_number is not None or judge_call_id != sdk_input_id
                    or not judge_call_id or purpose != execution.judge_purpose or actor != "judge"):
                raise SharedControlError("Judge reservation lacks its actual SDK-owned call identity.")
            if role == "product" and judge_call_id is not None:
                raise SharedControlError("Product reservation cannot borrow judge identity.")
            if not ledger.phase_microusd:
                ledger.phase_microusd = dict(parent.phase_microusd)
                ledger.reserve_microusd = parent.reserve_microusd
            started = {item.execution_id for item in attempts} | set(ledger.execution_results) | {key}
            remaining = [item for item in bundle.plan.executions if item.required and item.execution_id not in started]
            if len(attempts) + 1 + sum(item.request_cap for item in remaining) > grant.total_limit:
                reason = "unexecuted mandatory request envelope protected"
            if sum(item.occupied_microusd for item in attempts) + price + sum(item.cost_cap_microusd for item in remaining) > grant.total_microusd:
                reason = "unexecuted mandatory cost envelope protected"
            if (sum(item.occupied_microusd for item in attempts if item.stage == execution.stage) + price
                    + sum(item.cost_cap_microusd for item in remaining if item.stage == execution.stage) > ledger.phase_microusd[execution.stage]):
                reason = "stage cost budget exhausted or mandatory envelope protected"
            if sum(item.state in {"reserved", "http_started"} for item in attempts) >= parent.inflight_limit:
                reason = "inflight request limit exhausted"
        if reason:
            if ledger.authorization is None:
                ledger.authorization = grant
                ledger.authorization_binding = digest(grant.model_dump(mode="json"))
            ledger.case_stops[self.stop_key(case_id, protocol)] = {"reason": reason,
                "unfinished": "Remaining work was not authorized to send."}
            self._save_ledger(ledger)
            raise CaseBudgetExceeded(reason + "; no terminal HTTP sent.")
        self._parent_binding(bundle,grant,activate=True,required=False)
        number = len(attempts) + 1
        if ledger.authorization is None:
            ledger.authorization_binding = digest(grant.model_dump(mode="json"))
            ledger.authorization = grant
        ledger.attempts.append(_ReservedAttempt(number=number, authorization_id=grant.authorization_id, case_id=case_id,
            protocol=protocol, actor=actor, purpose=purpose, delegation_id=delegation_id, product_number=product_number,
            execution_id=self.execution_id(case_id, protocol), role=role, product_step_id=product_step_id, reservation_microusd=price, occupied_microusd=price,
            sdk_input_id=sdk_input_id, request_sha256=request_sha256, judge_call_id=judge_call_id, stage=execution.stage if execution else ""))
        if product_owner is not None:
            ledger.product_request_owners[str(number)] = product_owner
        self._save_ledger(ledger)
        return number

    def accounting_report(self) -> dict[str, Any]:
        ledger = self._read_ledger()
        owners = {number: {**owner, 'sources': [
            {**{key: value for key, value in source.items() if key not in {'original_codec', 'original_compressed_base64'}},
                'original_base64': b64encode(restore_product_original(source)).decode('ascii')}
            for source in owner['sources']]} for number, owner in ledger.product_request_owners.items()}
        grant = ledger.authorization or self.authorization
        return {"version": ledger.version, "binding": ledger.binding, "stopped": ledger.stopped, "stop_reason": ledger.stop_reason,
            "parent_budget_id":grant.parent_budget_id if grant else None,
            "parent_binding_path":str(self.parent_binding_path(grant.parent_budget_id)) if grant is not None and grant.parent_budget_id is not None else None,
            "request_limit": grant.total_limit if grant else None, "cost_limit_microusd": grant.total_microusd if grant else None,
            "sdk_inputs": len({item.sdk_input_id for item in ledger.attempts if item.sdk_input_id}),
            "completed_responses": sum(item.state == "response_complete" for item in ledger.attempts),
            "usage_estimates": sum(item.usage_estimate_microusd is not None for item in ledger.attempts),
            "supplier_final_microusd": None,
            "reserved_requests": len(ledger.attempts), "not_sent": sum(item.state == "not_sent" for item in ledger.attempts),
            "actual_http": sum(item.state in {"http_started", "response_complete", "unknown"} for item in ledger.attempts),
            "occupied_microusd": sum(item.occupied_microusd for item in ledger.attempts),
            "unknown_fee_requests": sum(item.state != "not_sent" and item.supplier_final_microusd is None for item in ledger.attempts),
            "case_stops": ledger.case_stops, "product_request_owners": owners, "product_task_stops": ledger.product_task_stops, "phase_microusd": ledger.phase_microusd,
            "reserve_microusd": ledger.reserve_microusd, "reserve_transfers": ledger.transfers,
            "completed_executions": list(ledger.execution_results), "scope_stops":ledger.scope_stops,
            "candidates":ledger.candidates,"active_candidates":ledger.active_candidates,
            "role_usage": {role: sum(item.actor == role for item in ledger.attempts) for role in sorted({item.actor for item in ledger.attempts})},
            "attempts": [item.model_dump(mode="json") for item in ledger.attempts]}

    @ledger_mutation
    def observe_product_stop(self, record_path: Path, *, case_id: str, protocol: Protocol, finalizing: bool = False) -> None:
        """Record a saved main-turn cancellation. Finalization leaves an already stopped ledger intact."""
        from myrk.conversation import ConversationRecord

        try:
            original = record_path.read_bytes()
            record = ConversationRecord.model_validate_json(original)
        except (OSError, ValueError) as error:
            raise SharedControlError(f"Product stop original cannot be read: {record_path}: {error}") from error
        cancelled = [turn for turn in record.turns if turn.request_owner_id is None and turn.status == "cancelled"]
        if not cancelled:
            return
        try:
            _, _, ledger = self._validate(case_id=case_id, protocol=protocol, allow_stopped=finalizing)
        except CaseBudgetExceeded:
            ledger = self._read_ledger()
        if finalizing and ledger.stopped:
            return
        bound = [item for item in ledger.attempts if item.case_id == case_id and item.protocol == protocol
            and item.execution_id == self.execution_id(case_id, protocol)]
        matches = [turn for turn in cancelled if any(item.product_number == request.number
            and item.product_step_id == request.step_id
            and (item.actor, item.purpose, item.delegation_id) == (request.actor, request.purpose, request.delegation_id)
            and request.observation is not None and request.observation.wire is not None
            and item.request_sha256 == request.observation.wire.body_sha256
            for item in bound for request in turn.request_attempts)]
        if not matches and not (record.turns and record.turns[-1] in cancelled
                and not record.turns[-1].request_attempts):
            return
        assert self.authorization is not None
        if ledger.authorization is None:
            ledger.authorization = self.authorization
            ledger.authorization_binding = digest(self.authorization.model_dump(mode="json"))
        if self.registry.cases[case_id].product_stop_scope == "task":
            task_ids = {turn.task.id for turn in matches if turn.task is not None}
            if not task_ids and record.turns and record.turns[-1] in cancelled and record.turns[-1].task is not None:
                task_ids.add(record.turns[-1].task.id)
            for task_id in task_ids:
                ledger.product_task_stops[f"{self.execution_id(case_id, protocol)}:{task_id}"] = {"reason": "product saved cancelled main turn; product Task stopped",
                    "source": str(record_path), "source_sha256": sha256(original).hexdigest()}
            for item in bound:
                owner = ledger.product_request_owners.get(str(item.number))
                if owner is not None and owner["task_id"] in task_ids and item.state in {"http_started", "unknown"}:
                    item.state, item.fault_origin = "unknown", "product saved cancelled Task before complete terminal response"
                    item.usage_estimate_microusd = None
            self._save_ledger(ledger)
            return
        ledger.case_stops[self.stop_key(case_id, protocol)] = {"reason": "product saved cancelled turn; execution stopped",
            "unfinished": "Remaining work was not authorized to send.",
            "source": str(record_path), "source_sha256": sha256(original).hexdigest()}
        for item in bound:
            if item.state in {"http_started", "unknown"}:
                item.state = "unknown"
                item.fault_origin = "product saved cancelled turn before complete terminal response"
                item.usage_estimate_microusd = None
        self._save_ledger(ledger)

    @ledger_mutation
    def mark_unknown(self, number: int, *, fault_origin: str, stop_case: bool = False) -> None:
        ledger = self._read_ledger()
        attempt = ledger.attempts[number - 1]
        attempt.state, attempt.fault_origin = "unknown", fault_origin
        if stop_case:
            owner = ledger.product_request_owners.get(str(number))
            if self.registry.cases[attempt.case_id].product_stop_scope == "task":
                if owner is None:
                    raise SharedControlError("Cancelled scoped request lacks its actual product Task owner.")
                ledger.product_task_stops[f"{owner['execution_id']}:{owner['task_id']}"] = {"reason": "request cancelled; product Task stopped", "fault_origin": fault_origin}
            else:
                ledger.case_stops[self.stop_key(attempt.case_id, attempt.protocol)] = {"reason": "request cancelled; execution stopped",
                    "unfinished": "Remaining work was not authorized to send."}
        self._save_ledger(ledger)

    @ledger_mutation
    def mark_not_sent(self, number: int, *, fault_origin: str) -> None:
        ledger = self._read_ledger()
        attempt = ledger.attempts[number - 1]
        if attempt.state != "reserved":
            raise SharedControlError("Cannot release an attempt after terminal HTTP started.")
        attempt.state, attempt.occupied_microusd, attempt.fault_origin = "not_sent", 0, fault_origin
        self._save_ledger(ledger)

    @ledger_mutation
    def mark_response_complete(self, number: int, record: dict[str, Any]) -> None:
        from tests.support.agent_acceptance.recorded_response import response_evidence
        ledger = self._read_ledger()
        attempt = ledger.attempts[number - 1]
        attempt.state = "response_complete"
        view = response_evidence(record)
        if view["status"] == "decoded":
            usages = [event.get("usageMetadata" if attempt.protocol == "google" else "usage") for event in view["events"]]
            usages = [item for item in usages if isinstance(item, dict)]
            if usages:
                usage = usages[-1]
                input_count = usage.get("promptTokenCount" if attempt.protocol == "google" else "prompt_tokens")
                output_count = usage.get("candidatesTokenCount" if attempt.protocol == "google" else "completion_tokens")
                if attempt.protocol == "google":
                    thinking = usage.get("thoughtsTokenCount")
                    output_count = output_count + thinking if type(output_count) is int and type(thinking) is int else None
                if type(input_count) is int and type(output_count) is int and input_count >= 0 and output_count >= 0:
                    bundle = self.prepared()
                    price = next(item for item in bundle.prices if item.protocol == attempt.protocol and item.role == attempt.role)
                    attempt.usage_estimate_microusd = price.microusd(input_count, output_count)
        self._save_ledger(ledger)

    @ledger_mutation
    def mark_http_started(self, number: int) -> None:
        ledger = self._read_ledger()
        validate_product_originals(ledger.product_request_owners)
        attempt = ledger.attempts[number - 1]
        key = attempt.execution_id if self.bound_execution_id is not None else f"{attempt.case_id}:{attempt.protocol}"
        owner = ledger.product_request_owners.get(str(number))
        task_stopped = bool(owner and f"{owner['execution_id']}:{owner['task_id']}" in ledger.product_task_stops)
        if ledger.stopped or key in ledger.case_stops or task_stopped:
            raise CaseBudgetExceeded("Durable execution or actual product Task stop accepted before terminal HTTP start.")
        attempt.state = "http_started"
        self._save_ledger(ledger)

    @ledger_mutation
    def stop(self, *, reason: str = "shared control failed") -> None:
        ledger = self._read_ledger()
        ledger.stopped = True
        ledger.stop_reason = reason
        self._save_ledger(ledger)
        for _, token in list(ACTIVE_CONTROLS.get(self.root.resolve(), [])):
            token.cancel()
