"""Confine requested check reports and preserve them before runtime removal."""

from pathlib import Path
from typing import Any


def validate_pytest_arguments(arguments: list[str]) -> None:
    for index, argument in enumerate(arguments):
        if argument.startswith("@"):
            raise ValueError("Pytest response files can hide output paths; expand their arguments directly.")
        plugin = arguments[index + 1] if argument == "-p" and index + 1 < len(arguments) else argument[2:] if argument.startswith("-p") else ""
        if argument == "--noconftest" or plugin.strip() in {"no:myrk.check_plugin", "no:pytest_myrk.check_plugin"}:
            raise ValueError("Pytest protection cannot be disabled; remove " + argument + (" " + plugin if argument == "-p" else ""))


def report_path(value: str, *, kind: str, run: Path, project: Path, source: Path) -> Path:
    requested = Path(value)
    if not value or ".." in requested.parts or (requested.drive and not requested.is_absolute()):
        raise ValueError(f"Output path must not escape its owned run ({kind}): {value}")
    original = requested if requested.is_absolute() else source / requested
    if any(part.is_symlink() or part.is_junction() for part in (original, *original.parents)):
        raise ValueError(f"Output paths must not use links ({kind}): {value}")
    original = original.resolve()
    if not original.is_relative_to(project):
        raise ValueError(f"Output path must be inside the project ({kind}): {value}")
    if original.is_relative_to(run / "reports"):
        destination = original
    else:
        relative = original.relative_to(project) if requested.is_absolute() else requested
        destination = run / "reports" / relative
    if any(part.is_symlink() or part.is_junction() for part in (destination, *destination.parents)):
        raise ValueError(f"Output paths must not use links ({kind}): {value}")
    return destination


def retain_reports(run: Path, result_path: Path, *, owner: str) -> dict[str, Any] | None:
    reports = run / "reports"
    if not reports.exists():
        return None
    if any(path.is_symlink() or path.is_junction() for path in (reports, *reports.rglob("*"), result_path.parent, *result_path.parent.parents)):
        raise ValueError("Check report preservation refused linked output paths.")
    files = [path for path in reports.rglob("*") if path.is_file()]
    if not files:
        return None
    size = sum(path.stat().st_size for path in files)
    destination = result_path.parent / f"{run.name}.reports"
    reports.replace(destination)
    return {
        "owner": owner, "paths": [str(destination)], "bytes": size,
        "reason": "Reports explicitly requested for this check command.",
        "delete_when": "The requested reports have been reviewed or their necessary results saved with the task.",
    }
