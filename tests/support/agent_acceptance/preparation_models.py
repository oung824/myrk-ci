"""Schemas and codecs for frozen acceptance bundles and their ledger."""

import gzip
import json
import zlib
from base64 import b64decode
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from tests.support.agent_acceptance.core import AcceptancePlan, Protocol, SharedControlError
from tests.support.agent_acceptance.eligibility import RouteEligibility
from typing import Annotated, Any, Literal


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
