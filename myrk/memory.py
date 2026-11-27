"""Small local fact notes, separate from the original conversation history."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from myrk.vendor.hermes_threat_patterns import scan_for_threats


class MemoryChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add", "replace", "patch", "remove"]
    text: str = ""
    old_text: str = ""
    replacement: str = ""
    scope: Literal["personal", "project"] = "personal"


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid4().hex)
    scope: Literal["personal", "project"] = "personal"
    project: str | None = None
    text: str = Field(min_length=1)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    approve_writes: bool = False
    facts: list[Fact] = Field(default_factory=list)


class MemorySaveError(OSError):
    """A fact-store filesystem failure, separate from task authorization."""


class MemoryStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "facts.json"

    def read(self) -> MemoryRecord:
        return MemoryRecord.model_validate_json(self.path.read_text(encoding="utf-8")) if self.path.exists() else MemoryRecord()

    def save(self, record: MemoryRecord) -> None:
        content = record.model_dump_json(indent=2)
        pending = self.path.with_suffix(".json.tmp")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            pending.write_text(content, encoding="utf-8")
            pending.replace(self.path)
        except OSError as error:
            raise MemorySaveError("事实笔记保存失败：" + str(error)) from error

    def background(self, project: str | None, warnings: list[str] | None = None) -> str:
        lines: list[str] = []
        for fact in self.read().facts:
            if fact.scope != "personal" and (project is None or fact.project != project):
                continue
            findings = scan_for_threats(fact.text, scope="strict")
            if findings:
                warning = f"事实笔记{fact.id}命中有限危险文字规则，给助理的背景副本已挡住，原文件保留，可在长期记忆管理中检查。"
                if warnings is not None and warning not in warnings:
                    warnings.append(warning)
                lines.append("[已挡住一条危险笔记；原文件保留]")
            else:
                lines.append(fact.text)
        return "\n".join(lines)

    def state(self) -> dict[str, object]:
        record = self.read()
        warnings = [f"笔记{fact.id}命中有限危险文字规则；管理中仍显示原文，给助理的副本会挡住。" for fact in record.facts if scan_for_threats(fact.text, scope="strict")]
        return {"facts": [fact.model_dump() for fact in record.facts], "approve_writes": record.approve_writes, "warnings": warnings, "location": str(self.path)}

    def change(self, changes: list[MemoryChange], project: str | None) -> str:
        record = self.read()
        results: list[str] = []
        for change in changes:
            if change.scope == "project" and project is None:
                raise ValueError("项目笔记须属于本次已选任务文件夹。")
            if change.action == "add":
                fact = Fact(text=change.text, scope=change.scope, project=project if change.scope == "project" else None)
                record.facts.append(fact)
            else:
                matches = [fact for fact in record.facts if fact.scope == change.scope and fact.project == (project if change.scope == "project" else None) and change.old_text and change.old_text in fact.text]
                if len(matches) != 1:
                    raise ValueError("未找到唯一事实条目，请提供更明确的原文字片段；笔记未改变。")
                fact = matches[0]
                if change.action == "remove":
                    record.facts.remove(fact)
                    results.append("已删除整条事实：" + fact.text + "。只影响当前事实笔记；旧聊天、摘要、备份、导出和其他待审提议保留，当前聊天背景不立即更新。")
                    continue
                if change.action == "replace":
                    fact.text = change.text
                else:
                    if not change.text or fact.text.count(change.text) != 1:
                        raise ValueError("局部修改须唯一匹配原文；笔记未改变。")
                    fact.text = fact.text.replace(change.text, change.replacement, 1)
                if not fact.text.strip():
                    raise ValueError("事实内容不能为空；删除请明确选择删除动作。")
            findings = scan_for_threats(fact.text, scope="strict")
            if findings:
                raise ValueError("已拒绝危险文字；整批笔记未改变。命中特征编号（规则识别名称）：" + ", ".join(findings))
            results.append("已保存精简事实笔记：" + fact.text)
        self.save(record)
        return "\n".join(results)
