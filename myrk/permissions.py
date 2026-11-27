"""Persisted access, approval conditions, and reviewer choices."""

from typing import Literal
from pathlib import Path
from uuid import uuid4

from typing import Self
from pydantic import BaseModel, ConfigDict, Field, model_validator

ApprovalMode = Literal["request_approval", "auto_review", "full_access"]


class NetworkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_hosts: list[str] = Field(default_factory=list)
    denied_hosts: list[str] = Field(default_factory=list)
    allow_local_binding: bool = True
    decision_timeout_seconds: float = Field(default=90, gt=0, le=3600, allow_inf_nan=False)


class AccessSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sandbox: Literal["restricted", "full_access"] = "restricted"
    writable_roots: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)
    read_only: bool = False
    legacy_scope: bool = False
    network: NetworkSettings = Field(default_factory=NetworkSettings)


class ApprovalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: ApprovalMode = "request_approval"
    access: AccessSettings = Field(default_factory=AccessSettings)
    condition: Literal["on_request", "never"] = "on_request"
    reviewer: Literal["user", "assistant", "none"] = "user"
    review_mandatory: bool = False
    revision: str = Field(default_factory=lambda: uuid4().hex)

    @model_validator(mode="after")
    def selected_mode(self) -> Self:
        self.access.sandbox = "full_access" if self.mode == "full_access" else "restricted"
        if "condition" not in self.model_fields_set:
            self.condition = "never" if self.mode == "full_access" else "on_request"
            self.model_fields_set.add("condition")
        if "reviewer" not in self.model_fields_set:
            self.reviewer = "none" if self.mode == "full_access" else "assistant" if self.mode == "auto_review" else "user"
            self.model_fields_set.add("reviewer")
        return self


def requires_write_approval(target: Path, access: AccessSettings, storage: list[Path]) -> bool:
    """Writable project roots retain narrower protected configuration rules."""
    if access.sandbox == "full_access" and not access.read_only:
        return False
    roots = [Path(root).resolve() for root in access.writable_roots]
    protected = [*(path.resolve() for path in storage), *(root / name for root in roots for name in (".git", ".agents", ".codex", ".aws"))]
    return access.read_only or any(target.is_relative_to(path) for path in protected) or not any(target.is_relative_to(root) for root in roots)
