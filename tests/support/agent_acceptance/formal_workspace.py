"""Build the desktop and run its isolated lifecycle validation."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from myrk.project_paths import project_root
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, SharedControlError
from tests.support.agent_acceptance.formal_authority import provider_config
from tests.support.agent_acceptance.preparation import SOURCE


def environment(root: Path) -> dict[str, str]:
    for leaf in ("temp", "local", "roaming", "cache"):
        (root / leaf).mkdir(parents=True, exist_ok=True)
    return {**os.environ, "TEMP": str(root / "temp"), "TMP": str(root / "temp"), "TMPDIR": str(root / "temp"),
        "LOCALAPPDATA": str(root / "local"), "APPDATA": str(root / "roaming"), "PYTHONPATH": str(SOURCE),
        "PYTHONDONTWRITEBYTECODE": "1", "PYDANTIC_AI_NO_BANNER": "1", "MYRK_PYTHON": sys.executable,
        "npm_config_cache": str(root / "cache"), "MYRK_BACKGROUND_TEST": "1", "ELECTRON_RENDERER_URL": ""}


def build(root: Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=False)
    output = root / "project/desktop/out"
    output.parent.mkdir(parents=True)
    (output.parent / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    modules = project_root(SOURCE) / "desktop/node_modules"
    if not (SOURCE / "desktop/node_modules").exists():
        subprocess.run([r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoProfile", "-Command", "New-Item", "-ItemType", "Junction",
            "-Path", str(SOURCE / "desktop/node_modules"), "-Target", str(modules)], check=True, capture_output=True)
    result = subprocess.run(["node", str(modules / "electron-vite/bin/electron-vite.js"), "build", "--outDir", str(output)],
        cwd=SOURCE / "desktop", env=environment(root), capture_output=True, check=False)
    (root / "build.stdout").write_bytes(result.stdout)
    (root / "build.stderr").write_bytes(result.stderr)
    if result.returncode:
        raise SharedControlError(f"Formal desktop build failed ({result.returncode}): {root}")
    return {path.relative_to(output).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob("*")) if path.is_file()}


def prepare(root: Path) -> dict[str, Any]:
    from tests.support.agent_acceptance.formal_scenarios import SCENARIOS
    build_root = root / "preparation-build"
    hashes = build(build_root)
    shutil.rmtree(build_root / "project")
    return {"compiled_sources": hashes, "case_limits": {name: spec[2] for name, spec in SCENARIOS.items()},
        "provider_configs": {name: provider_config(name).model_dump(mode="json")
        for name in ("deepseek", "openai-chat", "google")}}


def bindings() -> tuple[Path, ...]:
    return tuple(path for directory in (SOURCE / "desktop/src", SOURCE / "desktop/tests", SOURCE / "desktop/scripts")
        for path in directory.rglob("*") if path.is_file()) + (SOURCE / "desktop/electron.vite.config.ts",
        SOURCE / "desktop/package.json", SOURCE / "desktop/package-lock.json", SOURCE / "tests/support/process_observation.py",
        SOURCE / "tests/support/process_receipt.py")


async def lifecycle(context: CaseContext) -> CaseOutcome:
    await asyncio.to_thread(build, context.root / "build")
    env = {**environment(context.root), "MYRK_TEST_RUN_DIRECTORY": str(context.root / "build")}
    process = await asyncio.create_subprocess_exec("node", str(SOURCE / "tests/support/agent_acceptance/desktop/lifecycle.mjs"),
        str(context.root), cwd=SOURCE, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    (context.root / "runner.stdout").write_bytes(stdout)
    (context.root / "runner.stderr").write_bytes(stderr)
    path = context.root / "lifecycle-result.json"
    if not path.is_file():
        raise SharedControlError(f"Desktop lifecycle evidence missing ({process.returncode}): {context.root}")
    facts = json.loads(path.read_text(encoding="utf-8"))
    facts.update(real_http_attempts=0, normal_profile_accesses=sum(not item["isolated_profile"] for item in facts["observations"]))
    evidence = {"lifecycle": path}
    for item in context.root.rglob("*"):
        if item.is_file() and (item.name in {"processes.json", "electron.jsonl", "service.jsonl", "crash.jsonl", "recover.jsonl",
            "normal-reopen.jsonl", "save.json", "restore.json", "forgotten.json", "before-crash.json"}):
            evidence[item.relative_to(context.root).as_posix()] = item
    for path in (context.root / "build/project", *(context.root / leaf for leaf in ("temp", "local", "roaming", "cache")),
                 *(context.root / leaf / "user-data" for leaf in ("credentials", "lifecycle", "recovery"))):
        if path.exists():
            shutil.rmtree(path)
    return CaseOutcome("passed" if facts["passed"] and process.returncode == 0 else "failed", facts, evidence)
