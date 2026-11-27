"""Observe original save failures without changing persistence or retry policy."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import traceback
from typing import Any
from unittest.mock import patch

from myrk.conversation import Conversation


@contextmanager
def observe_saves(root: Path, report: Path) -> Iterator[None]:
    """Retain bounded metadata and stack locations, never record contents or locals."""
    original = Conversation.save
    calls = 0
    failures: list[dict[str, Any]] = []

    def location(value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name

    def observed(conversation: Conversation) -> None:
        nonlocal calls
        calls += 1
        try:
            original(conversation)
        except OSError as error:
            frames = traceback.extract_tb(error.__traceback__)
            failures.append({"at": datetime.now(timezone.utc).isoformat(), "save_call": calls,
                "record": location(str(conversation.path)), "exception_type": type(error).__name__,
                "errno": error.errno, "winerror": getattr(error, "winerror", None),
                "filename": location(error.filename), "filename2": location(error.filename2),
                "operation": frames[-1].name if frames else None,
                "stack": [{"file": location(frame.filename), "line": frame.lineno, "function": frame.name}
                    for frame in frames]})
            raise

    report.parent.mkdir(parents=True, exist_ok=True)
    report.touch(exist_ok=False)
    with patch.object(Conversation, "save", observed):
        original_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            original_error = error
            raise
        finally:
            try:
                report.write_text(json.dumps({"kind": "save_diagnostics", "pid": os.getpid(),
                    "save_calls": calls, "errors": failures}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as diagnostic_error:
                if original_error is None:
                    raise
                original_error.add_note("Save diagnostic output failed: " + type(diagnostic_error).__name__)
