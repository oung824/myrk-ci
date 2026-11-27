"""Scoped historical references never establish a current case's live result."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from myrk.project_paths import project_root
from tests.support.agent_acceptance.core import Protocol


SOURCE = Path(__file__).resolve().parents[3]
INDEX = SOURCE / "tests/acceptance/materials/history/index.json"


class HistoricalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def project_relative(cls, value: str) -> str:
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("Historical originals require a stable project-relative path.")
        return value


class HistoricalObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    protocol: Protocol
    model: str
    source_version: str
    source_binding: str = "unknown"
    result: str
    scope: str
    resolution_status: str
    related_cases: list[str]
    evidence: list[HistoricalFile]

    def report(self) -> dict[str, Any]:
        row = self.model_dump(mode="json")
        row["evidence"] = [{"path": str(project_root(SOURCE) / item.path), "sha256": item.sha256} for item in self.evidence]
        return row


class HistoricalIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    historical_real_references: list[HistoricalObservation]
    historical_offline_product_observations: list[HistoricalObservation]

    def report(self) -> dict[str, Any]:
        return {name: [item.report() for item in rows] for name, rows in (
            ("historical_real_references", self.historical_real_references),
            ("historical_offline_product_observations", self.historical_offline_product_observations))}

    def coverage(self, case_id: str, protocol: str) -> dict[str, Any]:
        return {"historical_real": "not_measured", "historical_real_evidence": [],
            **{name: [item.report() for item in rows if item.protocol == protocol and case_id in item.related_cases]
                for name, rows in (("historical_real_references", self.historical_real_references),
                    ("historical_offline_product_observations", self.historical_offline_product_observations))}}


def read_history() -> HistoricalIndex:
    return HistoricalIndex.model_validate_json(INDEX.read_bytes())
