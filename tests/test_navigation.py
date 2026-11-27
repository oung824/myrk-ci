"""Public guidance must lead to real files, headings and the current record writer."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.check_navigation import EVIDENCE_INDEXES, check_navigation


@pytest.fixture
def guidance(tmp_path: Path) -> Path:
    (tmp_path / "docs/agents").mkdir(parents=True)
    (tmp_path / "myrk").mkdir()
    (tmp_path / "AGENTS.md").write_text("Read `docs/agents/domain.md`.\n", encoding="utf-8")
    (tmp_path / "CODING_STANDARDS.md").write_text("# Standards\n", encoding="utf-8")
    (tmp_path / "GLOSSARY.md").write_text("# Terms\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("当前采用第9版记录格式。\n[入口](docs/agents/domain.md#功能导航)\n", encoding="utf-8")
    (tmp_path / "docs/agents/domain.md").write_text("# Domain\n\n## 功能导航\n", encoding="utf-8")
    (tmp_path / "myrk/conversation.py").write_text(
        "raise RuntimeError('Navigation checks must not import the app')\n"
        "record = ConversationRecord(version=9)\n", encoding="utf-8",
    )
    return tmp_path


def test_guidance_accepts_unicode_headings_spaces_and_line_links_without_importing_app(guidance: Path) -> None:
    (guidance / "docs/guide name.md").write_text("# 同名\n\n# 同名\n", encoding="utf-8")
    with (guidance / "README.md").open("a", encoding="utf-8") as output:
        output.write(
            "[重复标题](<docs/guide name.md#同名-1>)\n"
            "[编码标题](docs/agents/domain.md#%E5%8A%9F%E8%83%BD%E5%AF%BC%E8%88%AA)\n"
            "[源码行](myrk/conversation.py:2)\n"
            "[外部](https://example.invalid/missing.md)\n"
            "```markdown\n[示例](missing.md)\n```\n"
        )
    assert check_navigation(guidance)["ok"] is True


@pytest.mark.parametrize(("file", "content", "expected"), [
    ("docs/agents/domain.md", "[不存在](missing.py)\n", "missing local target missing.py"),
    ("docs/agents/domain.md", "# 已改名\n", "missing heading #功能导航"),
    ("AGENTS.md", "Read `docs/agents/missing.md`.\n", "missing local target docs/agents/missing.md"),
    ("README.md", "当前采用第9版记录格式。\n[行](myrk/conversation.py:99)\n", "invalid target line"),
    ("README.md", "当前采用第8版记录格式。\n", "format 8 differs from writer format 9"),
    ("README.md", "[入口](docs/agents/domain.md)\n", "Cannot compare current format"),
    ("myrk/conversation.py", "record = ConversationRecord(version=10)\n", "format 9 differs from writer format 10"),
])
def test_guidance_rejects_navigation_and_version_drift(guidance: Path, file: str, content: str, expected: str) -> None:
    (guidance / file).write_text(content, encoding="utf-8")
    result = check_navigation(guidance)
    assert result["ok"] is False
    assert expected in "\n".join(result["errors"])


def test_deleted_guidance_entry_is_rejected(guidance: Path) -> None:
    (guidance / "CODING_STANDARDS.md").unlink()
    result = check_navigation(guidance)
    assert result["ok"] is False
    assert "Missing guidance entry: CODING_STANDARDS.md" in result["errors"]


def test_historical_evidence_is_a_link_target_not_a_second_current_guidance_source(guidance: Path) -> None:
    evidence = guidance / ".scratch/old-result"
    evidence.mkdir(parents=True)
    (evidence / "report.md").write_text("[旧运行文件](already-removed.txt)\n", encoding="utf-8")
    with (guidance / "README.md").open("a", encoding="utf-8") as output:
        output.write("[历史证据](.scratch/old-result/report.md)\n")
    assert check_navigation(guidance)["ok"] is True
    (evidence / "report.md").unlink()
    result = check_navigation(guidance)
    assert result["ok"] is False
    assert "missing local target .scratch/old-result/report.md" in "\n".join(result["errors"])


@pytest.mark.parametrize("name", EVIDENCE_INDEXES)
def test_curated_evidence_indexes_check_their_successor_links(guidance: Path, name: str) -> None:
    index = guidance / name
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("[后续结果](missing-successor.md)\n", encoding="utf-8")
    result = check_navigation(guidance)
    assert result["ok"] is False
    assert "missing local target missing-successor.md" in "\n".join(result["errors"])


def test_navigation_cli_returns_failure_for_an_invalid_link(guidance: Path) -> None:
    (guidance / "AGENTS.md").write_text("Read `missing.md`.\n", encoding="utf-8")
    process = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "scripts/check_navigation.py"), "--root", str(guidance)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert process.returncode == 1
    assert json.loads(process.stdout)["ok"] is False
