"""Check public guidance targets and the documented conversation format, without importing Myrk."""

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import unquote


EVIDENCE_INDEXES = (
    ".scratch/usage-acceptance-audit-20261111/acceptance.md",
    ".scratch/workspace-cleanup-20261110/curated-acceptance.md",
    ".scratch/workspace-closeout-20261112/result.md",
)


def markdown_body(text: str) -> str:
    """Keep line numbers while excluding fenced command examples from link checks."""
    return re.sub(r"(?ms)^```[^\n]*\n.*?^```[ \t]*$", lambda match: re.sub(r"[^\n]", " ", match[0]), text)


def anchors(text: str) -> set[str]:
    seen: Counter[str] = Counter()
    result = set(re.findall(r'<a\s+(?:id|name)=[\"\x27]([^\"\x27]+)', text))
    for heading in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", markdown_body(text)):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        result.add(slug + (f"-{seen[slug]}" if seen[slug] else ""))
        seen[slug] += 1
    return result


def check_navigation(root: Path) -> dict[str, object]:
    root = root.resolve()
    documents = sorted([
        *root.glob("*.md"), *(root / "docs").rglob("*.md"),
        *(root / name for name in EVIDENCE_INDEXES if (root / name).is_file()),
    ])
    inputs = {document.relative_to(root).as_posix() for document in documents}
    targets: set[str] = set()
    errors: list[str] = []
    checked = 0
    for required in ("AGENTS.md", "CODING_STANDARDS.md", "GLOSSARY.md", "README.md", "docs/agents/domain.md"):
        if not (root / required).is_file():
            errors.append(f"Missing guidance entry: {required}")
    for document in documents:
        body = markdown_body(document.read_text(encoding="utf-8-sig"))
        references = [(match.start(), match[1].strip("<>")) for match in re.finditer(
            r'\[[^\]\n]+\]\((<[^>\n]+>|[^\s)\n]+)(?:\s+"[^"\n]*")?\)', body,
        )]
        if document.name == "AGENTS.md":
            references.extend((match.start(), match[1]) for match in re.finditer(r"`([^`\n]+\.md)`", body))
        for offset, reference in references:
            if not re.match(r"^[A-Za-z]:[/\\]", reference) and re.match(r"^[A-Za-z][\w+.-]*:", reference):
                continue
            if reference.startswith("//"):
                continue
            location, _, fragment = unquote(reference).partition("#")
            line_match = re.search(r":(\d+)$", location)
            source_line = int(line_match[1]) if line_match else None
            if line_match:
                location = location[:line_match.start()]
            target = document.parent / location if location else document
            resolved = target.resolve()
            if resolved.is_relative_to(root):
                targets.add(resolved.relative_to(root).as_posix())
            checked += 1
            label = f"{document.relative_to(root).as_posix()}:{body.count(chr(10), 0, offset) + 1}"
            if not target.exists():
                errors.append(f"{label}: missing local target {reference}")
            elif source_line is not None or (fragment and target.suffix.lower() == ".md"):
                if resolved.is_relative_to(root):
                    inputs.add(resolved.relative_to(root).as_posix())
                if not target.is_file():
                    errors.append(f"{label}: expected a file for {reference}")
                    continue
                target_text = target.read_text(encoding="utf-8-sig")
                if source_line is not None and (source_line < 1 or source_line > len(target_text.splitlines())):
                    errors.append(f"{label}: invalid target line {reference}")
                if fragment and target.suffix.lower() == ".md" and fragment not in anchors(target_text):
                    errors.append(f"{label}: missing heading #{fragment} in {location or document.name}")

    readme = root / "README.md"
    source = root / "myrk/conversation.py"
    if not source.is_file():
        errors.append("Missing conversation format source: myrk/conversation.py")
    elif readme.is_file():
        inputs.add("myrk/conversation.py")
        versions = {
            keyword.value.value
            for call in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig")))
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "ConversationRecord"
            for keyword in call.keywords
            if keyword.arg == "version" and isinstance(keyword.value, ast.Constant) and type(keyword.value.value) is int
        }
        documented = re.findall(r"当前采用第(\d+)版记录格式", readme.read_text(encoding="utf-8-sig"))
        if len(versions) != 1 or len(documented) != 1:
            errors.append("Cannot compare current format: keep one explicit ConversationRecord(version=...) and one README current-format declaration")
        elif int(documented[0]) not in versions:
            errors.append(f"README conversation format {documented[0]} differs from writer format {next(iter(versions))}")
    return {
        "ok": not errors, "documents": len(documents), "references": checked, "errors": errors,
        "inputs": sorted(inputs), "targets": sorted(targets),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    options = parser.parse_args()
    try:
        result = check_navigation(options.root)
    except (OSError, SyntaxError, ValueError) as error:
        result = {"ok": False, "errors": [str(error)]}
    print(json.dumps({key: value for key, value in result.items() if key not in {"inputs", "targets"}}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
