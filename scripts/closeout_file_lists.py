"""Load additional per-file dispositions for closeout."""

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any


def read_file_lists(references: object, manifest: Path, project: Path, metadata: list[Path],
                    resolve: Callable[[object, Path, Path], Path]) -> tuple[list[Path], list[dict[str, Any]]]:
    if not isinstance(references, list):
        raise ValueError("closeout.file_lists must be a list of paths.")

    fragments: dict[Path, None] = {}
    entries: list[dict[str, Any]] = []
    for reference in references:
        path = resolve(reference, project, project)
        if path in fragments:
            raise ValueError(f"Duplicate closeout file list: {path}")
        if any(part.casefold() == ".git" for part in path.relative_to(project).parts):
            raise ValueError(f"A closeout file list cannot be in the Git database: {path}")
        if not path.is_relative_to(manifest.parent) or path.suffix != ".json":
            raise ValueError(f"Closeout file lists must be JSON files within the manifest directory: {path}")
        if any(path.is_relative_to(item) for item in metadata):
            raise ValueError(f"Cannot read a metadata-only closeout file list: {path}")
        if not path.is_file():
            raise ValueError(f"Closeout file list is missing or not a file: {path}")

        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON in closeout file list {path}: {error}") from error
        if not isinstance(content, list) or any(not isinstance(entry, dict) for entry in content):
            raise ValueError(f"Closeout file list must be a JSON array of file dispositions: {path}")
        fragments[path] = None
        entries.extend(content)
    return list(fragments), entries
