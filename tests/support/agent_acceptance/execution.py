"""Use the already installed project executor before importing task modules."""

from hashlib import sha256
import json
import os
from pathlib import Path

from myrk.project_paths import project_root


def configure_executor() -> Path:
    source = Path(__file__).resolve().parents[3]
    release = json.loads((source / "myrk/executor-release.json").read_bytes())
    selected = Path(os.environ.get("MYRK_EXECUTOR_PATH", str(project_root(source) / "myrk/runtime" / release["release"] / release["executable"])))
    if not selected.is_absolute() or not selected.is_file() or sha256(selected.read_bytes()).hexdigest() != release["sha256"]:
        raise ValueError("Acceptance requires the installed pinned MYRK_EXECUTOR_PATH; no runtime is downloaded or substituted.")
    os.environ["MYRK_EXECUTOR_PATH"] = str(selected)
    return selected
