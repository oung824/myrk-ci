"""Explicit, immutable execution identities and their real material bindings."""

from hashlib import sha256
from pathlib import Path
from typing import Literal, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tests.support.agent_acceptance.core import AcceptancePlan, CaseRegistry

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


SOURCE = Path(__file__).resolve().parents[3]


def relative_material_path(path: Path) -> Path:
    if path.is_absolute():
        if not path.resolve().is_relative_to(SOURCE):
            raise ValueError("Frozen material must belong to this source tree.")
        return path.resolve().relative_to(SOURCE)
    if ".." in path.parts:
        raise ValueError("Frozen material cannot traverse its source tree.")
    return path


def source_material_path(path: Path) -> Path:
    return SOURCE / path


class SharedCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scenario_id: str = Field(min_length=1)
    stage: Literal["quality"] = "quality"
    material_family_id: str
    material_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_cap: int = Field(gt=0,strict=True)
    criteria_path: Path
    criteria_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    never_tuned: Literal[True]
    applicability_reason: str = Field(min_length=1)


    @field_validator("criteria_path")
    @classmethod
    def portable_criteria(cls, value: Path) -> Path:
        return relative_material_path(value)


class FrozenExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(min_length=1)
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    protocol: Literal["deepseek", "openai-chat", "google"]
    scenario_id: str = Field(min_length=1)
    material_family_id: str = Field(min_length=1)
    material_variant: Literal["baseline", "variant"]
    material_path: Path
    material_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    startpoint: str = Field(min_length=1)
    order: int = Field(ge=1, strict=True)
    dependencies: tuple[str, ...] = ()
    risk_branch: str | None = None
    stage: Literal["screening", "quality", "judge"] = "screening"
    role: Literal["product", "judge"] = "product"
    judge_purpose: Literal["calibration", "qualification", "scoring"] | None = None
    request_cap: int = Field(gt=0, strict=True)
    cost_cap_microusd: int = Field(gt=0, strict=True)
    required: bool = True
    shared_with: tuple[SharedCoverage, ...] = ()
    equivalence_path: Path | None = None
    equivalence_sha256: str | None = None

    @field_validator("material_path", "equivalence_path")
    @classmethod
    def portable_material(cls, value: Path | None) -> Path | None:
        return relative_material_path(value) if value is not None else None

    @model_validator(mode="after")
    def judge_binding(self) -> "FrozenExecution":
        if (self.role == "judge") != (self.judge_purpose is not None) or self.role == "judge" and self.stage != "judge":
            raise ValueError("Judge calls require their frozen judge stage and actual purpose.")
        return self

    @property
    def directory_name(self) -> str:
        return sha256(self.execution_id.encode()).hexdigest()[:24]


class FrozenParentBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    parent_id: str = Field(min_length=1)
    accounting_root: Path
    request_cap: int = Field(gt=0, strict=True)
    phase_microusd: dict[str, int]
    reserve_microusd: int = Field(ge=0, strict=True)
    inflight_limit: int = Field(default=4, ge=1, le=4, strict=True)
    profile: Literal["full-proposal", "synthetic"] = "full-proposal"

    @model_validator(mode="after")
    def fixed_envelope(self) -> "FrozenParentBudget":
        if (set(self.phase_microusd) != {"screening", "quality", "judge"}
                or any(type(value) is not int or value < 0 for value in self.phase_microusd.values())):
            raise ValueError("Parent stages require nonnegative integer microdollars.")
        if self.profile == "full-proposal" and (self.phase_microusd != {"screening":70000000,"quality":40000000,"judge":60000000}
                or self.reserve_microusd != 10000000 or self.request_cap != 7256):
            raise ValueError("The complete proposal freezes 70/40/60 plus reserve 10 USD and 7256 requests.")
        return self

    @property
    def total_microusd(self) -> int:
        return sum(self.phase_microusd.values()) + self.reserve_microusd


class ProposedExecution(BaseModel):
    """A countable proposal; deliberately not an executable material binding."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str
    scenario_id: str
    protocol: Literal["deepseek", "google"]
    stage: Literal["screening", "quality", "judge"]
    role: Literal["product", "judge"] = "product"
    request_cap: int
    material_variant: Literal["baseline", "variant"] = "baseline"
    risk_branch: str | None = None
    judge_purpose: Literal["calibration", "qualification", "scoring"] | None = None


class CompletePlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    executions: tuple[ProposedExecution, ...]

    def totals(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for row in self.executions:
            counts = result.setdefault(row.stage, {}).setdefault(row.protocol, {"executions":0,"requests":0})
            counts["executions"] += 1
            counts["requests"] += row.request_cap
        return {**result,"product_executions":sum(row.role == "product" for row in self.executions),
            "requests":sum(row.request_cap for row in self.executions),"sending_authorized":False,"ready":False}

    def freeze(self, bindings: dict[str, FrozenExecution], *, registry: "CaseRegistry",
               parent_budget: FrozenParentBudget | None) -> "AcceptancePlan":
        from tests.support.agent_acceptance.core import AcceptancePlan
        if set(bindings) != {row.execution_id for row in self.executions} or parent_budget is None:
            raise ValueError("Complete plan lacks actual material, implemented adapter or parent bindings; proposal is not ready.")
        rows = []
        for number, proposed in enumerate(self.executions,1):
            actual = bindings[proposed.execution_id]
            if any(getattr(actual,name) != getattr(proposed,name) for name in
                    ("execution_id","scenario_id","protocol","stage","role","request_cap","material_variant","risk_branch","judge_purpose")):
                raise ValueError("Actual execution differs from the frozen complete proposal.")
            rows.append(actual.model_copy(update={"order":number}))
        plan = AcceptancePlan(executions=tuple(rows),parent_budget=parent_budget,mode="prepare-live")
        registry.select(plan)
        return plan


def full_plan_proposal() -> CompletePlanProposal:
    """Expand the approved I5/I6/I11 table without inventing ready materials."""
    rows = []
    def add(scenario: str, protocol: Literal["deepseek","google"], cap: int, *,
            stage: Literal["screening","quality","judge"] = "screening", risk: bool = False,
            judge_purpose: Literal["calibration","qualification","scoring"] | None = None) -> None:
        for repeat in range(1,4 if risk else 2):
            rows.append(ProposedExecution(execution_id=f"{stage}:{protocol}:{scenario}:{repeat}",scenario_id=scenario,
                protocol=protocol,stage=stage,request_cap=cap,role="judge" if judge_purpose else "product",
                judge_purpose=judge_purpose,material_variant="variant" if risk and repeat == 3 else "baseline",
                risk_branch=scenario if risk else None))
    for number, cap in enumerate((4,6,12,12,12,12,12,16,20,16,20,24,12,16,24,12,6,20),1):
        add(f"D{number:02}","deepseek",cap)
    risk_caps=(12,12,12,10,10,10,16,16,16,30,24,30,16,16,16,20,30,30,80,80,80,80,80,80,80,80)
    for number, cap in enumerate(risk_caps,1):
        add(f"R{number:02}","deepseek",cap,risk=True)
    for number, cap in enumerate((6,16,16,12,20),1):
        add(f"G{number:02}","google",cap)
    for number in (1,4,7,10,13,16,20,21,22):
        add(f"R{number:02}","google",risk_caps[number-1],risk=True)
    for group in range(1,7):
        cap=60 if group == 6 else 24
        for number in range(1,11):
            add(f"QG{group}.formal.{number}","deepseek",cap,stage="quality")
            if number in (3,9):
                add(f"QG{group}.formal.{number}","google",cap,stage="quality")
        for number in (1,2):
            add(f"QG{group}.tuning.{number}","deepseek",cap,stage="quality")
    judge_stages: tuple[tuple[Literal["calibration","qualification","scoring"],int], ...] = (("calibration",60),("qualification",100),("scoring",320))
    for purpose, count in judge_stages:
        for number in range(1,count+1):
            add(f"judge.{purpose}.{number}","google",1,stage="judge",judge_purpose=purpose)
    return CompletePlanProposal(executions=tuple(rows))
