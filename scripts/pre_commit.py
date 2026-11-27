"""Run lightweight checks for the proposed Git commit."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import TypedDict, cast


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_navigation import EVIDENCE_INDEXES, check_navigation
from scripts.check_comments import BASELINE, WEB_SCANNER

HOOK_INPUTS = [
    ".githooks/pre-commit", "scripts/pre_commit.ps1", "scripts/pre_commit.py", "scripts/check_navigation.py",
    "scripts/check_comments.py", BASELINE, WEB_SCANNER,
]
TOKEN_LIMIT_EXEMPTIONS = ".githooks/token-limit-exemptions.txt"
CLOSEOUT_SCRIPT = "scripts/check_closeout.py"
CLOSEOUT_FILE_LISTS = "scripts/closeout_file_lists.py"
BYTES_PER_TOKEN = 4
MAX_FILE_TOKENS = 5_000
MAX_STAGED_FILE_BYTES = MAX_FILE_TOKENS * BYTES_PER_TOKEN


class CheckCommand(TypedDict):
    argv: list[str]
    cwd: str


def git_output(root: Path, *arguments: str, input_data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True,
                          input=input_data).stdout


def git_paths(root: Path, command: str, *arguments: str) -> list[str]:
    return [value.decode("utf-8") for value in git_output(root, command, "-z", *arguments).split(b"\0") if value]


def check_staged_file_sizes(root: Path) -> bool:
    exemption_index_spec = f":{TOKEN_LIMIT_EXEMPTIONS}".encode("ascii")
    header, _, content = git_output(root, "cat-file", "--batch=%(objecttype)",
                                    input_data=exemption_index_spec + b"\n").partition(b"\n")
    if header not in {b"blob", exemption_index_spec + b" missing"}:
        raise ValueError(f"Cannot read {TOKEN_LIMIT_EXEMPTIONS} from the proposed Git index")
    exclusions = [f":(top,exclude,glob){pattern}" for line in content.decode("utf-8-sig").splitlines()
                  if (pattern := line.strip()) and not pattern.startswith("#")]
    diff = git_output(root, "diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames",
                      "--diff-filter=AMT", "--ita-invisible-in-index", "--", *exclusions)
    entries = diff.split(b"\0")[:-1]
    blob_ids_by_path = {}
    for header, path in zip(entries[::2], entries[1::2], strict=True):
        _, mode, _, object_id, _ = header.split()
        if mode in {b"100644", b"100755"}:
            blob_ids_by_path[path] = object_id
    if not blob_ids_by_path:
        return True

    sizes = git_output(root, "cat-file", "--batch-check=%(objectsize)",
                       input_data=b"\n".join(blob_ids_by_path.values()) + b"\n").splitlines()
    oversized = {path: size for path, size in zip(blob_ids_by_path, map(int, sizes), strict=True)
                 if size > MAX_STAGED_FILE_BYTES}
    if not oversized:
        return True

    print(f"以下暂存文件超过 {MAX_STAGED_FILE_BYTES:,} 字节"
          f"（{MAX_FILE_TOKENS:,} 个 token，按字节数 ÷ {BYTES_PER_TOKEN} 估算）。", file=sys.stderr)
    for path, size in oversized.items():
        name = json.dumps(path.decode("utf-8"), ensure_ascii=False)
        print(f"  {name}  {size:,} 字节（{size / BYTES_PER_TOKEN:,.2f} 个 token）", file=sys.stderr)
    print(
        "1. 按职责拆分代码、按功能拆分测试、按章节拆分文档。原文件保留入口或目录，把独立内容移到新文件。\n"
        f"2. 更新导入、调用、测试引用和文档链接，确保每个拆分后的文件最多 {MAX_STAGED_FILE_BYTES:,} 字节。\n"
        "3. 用 git add -- <原文件> <新文件> ... 重新暂存所有改动，然后再提交。",
        file=sys.stderr,
    )
    return False


def unsaved_inputs(root: Path, staged: list[str], inputs: list[str], targets: list[str] | None = None) -> list[str]:
    changed = set(git_paths(root, "diff", "--name-only", "--no-renames"))
    tracked = set(git_paths(root, "ls-files", "--cached"))
    tracked.difference_update(git_paths(root, "diff", "--name-only", "--no-renames", "--diff-filter=A", "--ita-invisible-in-index"))
    pending = changed & (set(staged) | set(inputs))
    pending.update(set(inputs) - tracked)
    pending.update(path for path in staged if path not in tracked and (root / path).exists())
    for target in targets or []:
        if target not in tracked and not any(path.startswith(target.rstrip("/") + "/") for path in tracked):
            pending.add(target)
    return sorted(pending)


def require_staged_inputs(staged: list[str], inputs: list[str], targets: list[str] | None = None) -> bool:
    unsaved = unsaved_inputs(ROOT, staged, inputs, targets)
    if unsaved:
        print("提交检查涉及未暂存、未跟踪或不在暂存版本中的内容，请暂存拟提交的修改，或另行处理以下文件：", file=sys.stderr)
        print("\n".join(unsaved), file=sys.stderr)
    return not unsaved


def run_check(command: CheckCommand, environment: dict[str, str]) -> int:
    print("Myrk pre-commit: " + subprocess.list2cmdline(command["argv"]), flush=True)
    return subprocess.run(command["argv"], cwd=command["cwd"], env=environment).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="Print selected commands without running checks")
    options = parser.parse_args()
    staged = git_paths(ROOT, "diff", "--cached", "--name-only", "--no-renames")
    common = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    project = Path(common.stdout.strip()).parent
    python = os.environ.get("MYRK_PYTHON", str(project / ".venv/Scripts/python.exe"))
    comment_check: CheckCommand = {"argv": [python, "-B", "scripts/check_comments.py", "--staged"], "cwd": str(ROOT)}
    commands: list[CheckCommand] = [
        {"argv": ["git", "diff", "--cached", "--check"], "cwd": str(ROOT)},
        {"argv": [python, "-B", "scripts/check_navigation.py"], "cwd": str(ROOT)},
        comment_check,
    ]
    inputs = HOOK_INPUTS + git_paths(ROOT, "ls-files", "--cached", "--",
                                   ":(top,glob)*.md", ":(top,glob)docs/**/*.md", *EVIDENCE_INDEXES)
    manifest = os.environ.get("MYRK_CLOSEOUT_MANIFEST")
    preflight: CheckCommand | None = None
    if manifest:
        preflight_argv = [python, "-B", CLOSEOUT_SCRIPT, "preflight", str((ROOT / manifest).resolve()),
                          "--root", str(ROOT)]
        proposed_index = os.environ.get("GIT_INDEX_FILE")
        if proposed_index:
            preflight_argv.extend(["--index-file", str((ROOT / proposed_index).resolve())])
        preflight = {"argv": preflight_argv, "cwd": str(ROOT)}
    if options.plan:
        if preflight:
            commands.insert(0, preflight)
        print(json.dumps({"staged": staged, "staged_file_size_limit_bytes": MAX_STAGED_FILE_BYTES,
                          "commands": commands}, ensure_ascii=False))
        return 0
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    local_variables = subprocess.run(["git", "rev-parse", "--local-env-vars"], cwd=ROOT,
        check=True, capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
    worker_environment = {key: value for key, value in environment.items() if key not in local_variables}
    if preflight:
        closeout_inputs = [CLOSEOUT_SCRIPT]
        if CLOSEOUT_FILE_LISTS in staged or (ROOT / CLOSEOUT_FILE_LISTS).exists():
            closeout_inputs.append(CLOSEOUT_FILE_LISTS)
        if not require_staged_inputs([], closeout_inputs):
            return 1
        result = run_check(preflight, worker_environment)
        if result:
            return result
    if not check_staged_file_sizes(ROOT):
        return 1
    if not require_staged_inputs(staged, inputs):
        return 1
    navigation = check_navigation(ROOT)
    if not navigation["ok"]:
        print(json.dumps({key: value for key, value in navigation.items() if key not in {"inputs", "targets"}}, ensure_ascii=False))
        return 1
    if not require_staged_inputs(staged, inputs + cast(list[str], navigation["inputs"]),
                                 cast(list[str], navigation["targets"])):
        return 1
    for command in commands:
        result = run_check(command, environment if command["argv"][0] == "git" or command is comment_check
                           else worker_environment)
        if result:
            return result
    return 0


if __name__ == "__main__":
    sys.exit(main())
