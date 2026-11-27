"""Observed file changes and explicit deliveries owned by one conversation."""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class FileChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(default_factory=lambda: uuid4().hex)
    path: str
    change_kind: Literal['created', 'modified']
    before_sha256: str | None
    after_sha256: str
    size_bytes: int = Field(ge=0)
    backup_path: str | None = None


class DeliveredArtifact(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    source_action_id: str
    file_change_id: str
    delegation_id: str | None = None
    source_record_path: str
    name: str
    path: str
    change_kind: Literal['created', 'modified']
    delivered_sha256: str
    size_bytes: int = Field(ge=0)
    delivered_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
