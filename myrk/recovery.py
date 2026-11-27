"""Read-only observations of interrupted actions; never replay their operations."""

from datetime import datetime, timezone
from typing import Literal
from hashlib import sha256
from pathlib import Path
import re

from pydantic import BaseModel, Field
from myrk.actions import Action


class RecoveryOwnership:
    """Local controller receipts are never imported with a conversation payload."""

    fields = {"id", "task_id", "kind", "target", "backup_path", "before_sha256", "after_sha256",
              "receipt_path", "stdout_path", "stderr_path", "job_name", "cwd", "prepared_parameters", "approval_settings"}

    def __init__(self, record: Path) -> None:
        self.directory = record.parent / "settings" / "recovery" / sha256(str(record.resolve()).encode()).hexdigest()[:24]

    def remember(self, action: Action) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", action.id) is None:
            raise ValueError("本机动作归属编号不合法，未保存归属凭据。")
        self.directory.mkdir(parents=True, exist_ok=True)
        pending = self.directory / f"{action.id}.pending"
        pending.write_text(sha256(action.model_dump_json(include=self.fields).encode()).hexdigest(), encoding="ascii")
        pending.replace(self.directory / f"{action.id}.sha256")

    def owns(self, action: Action) -> bool:
        if re.fullmatch(r"[0-9a-f]{32}", action.id) is None:
            return False
        expected = self.directory / f"{action.id}.sha256"
        try:
            return expected.read_text(encoding="ascii") == sha256(action.model_dump_json(include=self.fields).encode()).hexdigest()
        except (OSError, UnicodeError):
            return False


class ActionEvidence(BaseModel):
    id: str
    recorded_at: str
    phase: Literal["prepared", "started", "checkpoint", "result"]
    action: Action


class RecoveryCheck(BaseModel):
    action_id: str
    effect: Literal["not_started", "completed", "unknown"]
    observation: str
    checked_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    exit_code: int | None = None


class RecoveryReport(BaseModel):
    task_id: str
    required: bool
    actions: list[RecoveryCheck]
    can_resume: bool
    message: str = "意外中断后未自动续办；请先核查影响，再由你选择继续。旧批准不能用于新动作。"
