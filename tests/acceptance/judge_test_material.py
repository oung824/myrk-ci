from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from myrk.check_plugin import project_test_directory


@contextmanager
def judge_test_material(source: Path) -> Iterator[Path]:
    """Put generated frozen inputs inside the source tree and attribute them to this check."""
    run = project_test_directory(os.environ["MYRK_CHECK_RUN_DIRECTORY"])
    run_is_in_source = run.is_relative_to(source)
    parent = project_test_directory(run / "tmp" if run_is_in_source else source / ".scratch/runtime")
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix=f"judge-{run.name}-") as directory:
        root = Path(directory)
        if not run_is_in_source:
            owner = json.loads((run / "owner.json").read_bytes())
            owner.update(
                runtime_directory=str(root),
                managed_run_directory=str(run),
                reason="Source-local frozen judge inputs for the managed check.",
                delete_when="After the managed check and its child processes exit.",
            )
            (root / "owner.json").write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
        yield root
