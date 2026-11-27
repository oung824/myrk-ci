"""Resolve project ownership without loading the application or its runtime."""

from pathlib import Path


def project_root(source: Path) -> Path:
    source = source.resolve()
    root = next((path for path in (source, *source.parents) if (path / ".git").exists()), source)
    marker = root / ".git"
    if marker.is_file():
        registration = marker.read_text(encoding="utf-8").strip()
        if not registration.startswith("gitdir:"):
            raise ValueError("Git 工作目录登记不完整，未建立运行副本。")
        git_directory = (root / registration.partition(":")[2].strip()).resolve()
        common = git_directory / "commondir"
        if common.is_file():
            git_directory = (git_directory / common.read_text(encoding="utf-8").strip()).resolve()
        if git_directory.name == ".git" and git_directory.is_dir():
            root = git_directory.parent
    return root
