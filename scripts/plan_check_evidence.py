"""Plan Git file sizes for check evidence before and after a run."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import sys
from tempfile import TemporaryDirectory


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from myrk.project_paths import project_root, require_project_path
from scripts.pre_commit import MAX_STAGED_FILE_BYTES, git_output, git_paths, staged_size_exemptions


ARCHIVE_PART_SEPARATOR = ".part-"


def archive_part_name(archive: str, number: int) -> str:
    return f"{archive}{ARCHIVE_PART_SEPARATOR}{number:02d}"


def git_temp_environment(directory: Path) -> dict[str, str]:
    location = str(directory)
    return dict(os.environ, TEMP=location, TMP=location, TMPDIR=location)


def exempt_paths(root: Path, project: Path, paths: set[str]) -> set[str]:
    patterns = staged_size_exemptions(root, env=git_temp_environment(project))
    if not patterns:
        return set()
    scratch = require_project_path(project, ".scratch")
    scratch.mkdir(exist_ok=True)
    ancestors = {parent.as_posix() for name in paths for parent in PurePosixPath(name).parents} & paths
    matches: set[str] = set()
    with TemporaryDirectory(prefix="evidence-plan-", dir=scratch) as temporary:
        directory = Path(temporary)
        owner = {
            "owner": "check-evidence-planner", "task_id": os.environ.get("MYRK_TASK_ID"),
            "project_root": str(project), "path": str(directory), "purpose": "Match planned Git size exemptions.",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (directory / "owner.json").write_text(json.dumps(owner, ensure_ascii=False) + "\n", encoding="utf-8")
        env = git_temp_environment(directory)
        empty_blob = git_output(root, "hash-object", "--stdin", input_data=b"", env=env).strip()
        for index, batch in enumerate([paths - ancestors, *({name} for name in sorted(ancestors))]):
            env["GIT_INDEX_FILE"] = str(directory / f"index-{index}")
            index_entries = b"".join(b"100644 " + empty_blob + b"\t" + path.encode() + b"\0" for path in sorted(batch))
            git_output(root, "-c", "core.splitIndex=false", "update-index", "--info-only", "-z", "--index-info",
                       input_data=index_entries, env=env)
            matches.update(git_paths(root, "ls-files", "--cached", "--",
                                     *(f":(top,glob){pattern}" for pattern in patterns), env=env))
    return matches


def destination_conflict(paths: set[str], parts: dict[str, int]) -> tuple[str, str] | None:
    files: dict[PurePosixPath, str] = {}
    split_archives: dict[PurePosixPath, str] = {}
    for name in sorted(paths):
        key = PurePosixPath(os.path.normcase(name).replace("\\", "/"))
        outputs = split_archives if name in parts else files
        if key in outputs:
            if name in parts:
                return archive_part_name(outputs[key], 1), archive_part_name(name, 1)
            return outputs[key], name
        outputs[key] = name

    def archive_part(path: PurePosixPath) -> str | None:
        stem, _, suffix = path.name.rpartition(ARCHIVE_PART_SEPARATOR)
        if stem in {"", ".", ".."} or not suffix.isascii() or not suffix.isdecimal():
            return None
        archive = split_archives.get(path.with_name(stem))
        if archive is None or len(path.name) > len(archive_part_name(stem, parts[archive])):
            return None
        number = int(suffix)
        if 1 <= number <= parts[archive] and path.name == archive_part_name(stem, number):
            return archive_part_name(archive, number)
        return None

    for path, name in files.items():
        if part := archive_part(path):
            return part, name
    for path, name in [*files.items(), *split_archives.items()]:
        for parent in path.parents:
            if file := files.get(parent) or archive_part(parent):
                return file, name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", action="append", nargs=2, default=[], metavar=("GIT_PATH", "BYTES_OR_FILE"),
                        help="Plan a submitted file using estimated bytes, ?, or a file with --measure")
    parser.add_argument("--archive", action="append", nargs=2, default=[], metavar=("GIT_PATH", "BYTES_OR_FILE"),
                        help="Plan an already compressed archive and any parts needed for submission")
    parser.add_argument("--measure", action="store_true", help="Measure existing files in place of estimates")
    args = parser.parse_args()
    entries = [(kind, path, value) for kind, proposals in (("file", args.file), ("archive", args.archive))
               for path, value in proposals]
    if not entries:
        parser.error("Provide at least one --file or --archive.")
    paths = set()
    for _, name, _ in entries:
        path = PurePosixPath(name)
        if (path.as_posix() != name or path.is_absolute() or name == "." or ":" in name or "\\" in name
                or any(part.casefold() in {"..", ".git"} for part in path.parts)):
            parser.error(f"Use a repository-relative Git path: {name}")
        if os.name == "nt" and any(part.endswith((".", " ")) for part in path.parts):
            parser.error(f"Windows destination {name!r} contains a component ending in a dot or space")
        if name in paths:
            parser.error(f"Duplicate destination: {name}")
        paths.add(name)

    cwd = Path.cwd()
    checkout = git_output(cwd, "rev-parse", "--show-toplevel", env=git_temp_environment(project_root(cwd)))
    root = Path(checkout.decode().strip())
    project = project_root(root)
    exempt = exempt_paths(root, project, paths)
    limit = MAX_STAGED_FILE_BYTES
    report = []
    parts: dict[str, int] = {}
    for kind, path, value in entries:
        size = None
        if args.measure:
            source = require_project_path(project, str(cwd / value))
            if not source.is_file():
                parser.error(f"Evidence file is missing or is not a regular file: {source}")
            size = source.stat().st_size
        elif value != "?":
            if not value.isascii() or not value.isdecimal():
                parser.error(f"Use a byte count or ?: {value}")
            size = int(value)
        oversized = size is not None and path not in exempt and size > limit
        row = {"kind": kind, "path": path, "input": value, "bytes": size, "exempt": path in exempt,
               "exceeds_limit": None if size is None and path not in exempt else oversized}
        if kind == "archive" and size is not None and oversized:
            count = (size + limit - 1) // limit
            parts[path] = count
            row["parts"] = {"count": count, "first": archive_part_name(path, 1), "last": archive_part_name(path, count),
                            "full_part_bytes": limit, "last_part_bytes": size - (count - 1) * limit}
        report.append(row)
    if conflict := destination_conflict(paths, parts):
        parser.error(f"Conflicting destinations: {conflict[0]} and {conflict[1]}")
    print(json.dumps({"basis": "measured_file_bytes" if args.measure else "estimated_bytes",
                      "exemptions_from": "git_index", "limit_bytes": limit, "files": report}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
