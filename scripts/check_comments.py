"""Reject new source comments and retire unused baseline entries."""

import argparse
from codecs import BOM_UTF16_BE, BOM_UTF16_LE
from collections import Counter
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tokenize
from typing import TypedDict


ROOT = Path(__file__).resolve().parents[1]
BASELINE = "scripts/comment_baseline.json"
WEB_SCANNER = "desktop/scripts/comment-tokens.mjs"
SHELL_LAUNCHER = ".githooks/pre-commit"
SOURCE_PATHS = ("myrk", "scripts", "tests", "desktop", "conftest.py", SHELL_LAUNCHER)
EXCLUDED_PATHS = ("myrk/vendor/", "tests/acceptance/materials/", "tests/fixtures/",
                  "desktop/node_modules/", "desktop/out/", "desktop/coverage/")
SHELL_LAUNCHER_TOKENS = re.compile(
    r"""(?P<comment>#[^\r\n]*)|[ \t\r\n;&|()<>]+|\\\r?\n"""
    r"""|(?:[^ \t\r\n'"\\;&|()<>]|'[^']*'|"(?:[^"\\]|\\[\s\S])*"|\\(?:\r\n|[\s\S]))+""",
)
WEB_MARKERS = {
    ".js": (b"//", b"/*", b"<!--", b"-->", b"\\"),
    ".css": (b"/*",),
    ".html": (b"<",),
}
POWERSHELL = r"C:\Program Files\PowerShell\7\pwsh.exe"
POWERSHELL_SCANNER = """
$sources = [Console]::In.ReadToEnd() | ConvertFrom-Json -AsHashtable
$result = @{}
function Get-Comments($tokens) {
    foreach ($token in $tokens) {
        if ($token.Kind -eq 'Comment') {
            @{ line = $token.Extent.StartLineNumber; text = $token.Text }
        }
        if ($token -is [System.Management.Automation.Language.StringExpandableToken]) {
            Get-Comments $token.NestedTokens
        }
    }
}
foreach ($source in $sources.GetEnumerator()) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput($source.Value, [ref]$tokens, [ref]$errors)
    $result[$source.Key] = @(Get-Comments $tokens)
}
ConvertTo-Json -InputObject $result -Depth 4 -Compress
"""


class Comment(TypedDict):
    text: str
    line: int


def git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], cwd=ROOT, check=check, capture_output=True)


def staged_sources(paths: list[str]) -> dict[str, bytes]:
    if not paths:
        return {}
    requests = b"".join(f":{path}\0".encode() for path in paths)
    result = subprocess.run(["git", "cat-file", "--batch", "-Z"], cwd=ROOT, check=True, input=requests,
                            capture_output=True)
    sources = {}
    offset = 0
    for path in paths:
        header_end = result.stdout.index(b"\0", offset)
        header = result.stdout[offset:header_end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError(f"{path}: Git source is missing or is not a file")
        start = header_end + 1
        end = start + int(header[2])
        sources[path] = result.stdout[start:end]
        offset = end + 1
    return sources


def source_kind(path: str) -> str | None:
    if path == SHELL_LAUNCHER:
        return ".sh"
    if path.startswith(EXCLUDED_PATHS):
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".js", ".cjs", ".mjs", ".ts", ".cts", ".mts", ".jsx", ".tsx"}:
        return ".js"
    return suffix if suffix in {".py", ".ps1", ".psm1", ".css", ".html"} else None


def read_scanner_comments(stdout: bytes, sources: dict[str, str], scanner: str) -> dict[str, list[Comment]]:
    response: object = json.loads(stdout)
    if not isinstance(response, dict) or response.keys() != sources.keys():
        raise ValueError(f"{scanner} comment parser returned unexpected paths")
    comments: dict[str, list[Comment]] = {}
    for path, entries in response.items():
        if not isinstance(entries, list):
            raise ValueError(f"{scanner} comment parser did not return a list for {path}")
        tokens: list[Comment] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{scanner} comment parser returned an invalid comment for {path}")
            line, text = entry.get("line"), entry.get("text")
            if type(line) is not int or line < 1 or not isinstance(text, str) or not text:
                raise ValueError(f"{scanner} comment parser returned an invalid comment for {path}")
            tokens.append(Comment(text=text, line=line))
        comments[path] = tokens
    return comments


def scan(paths: dict[str, bytes]) -> dict[str, list[Comment]]:
    comments: dict[str, list[Comment]] = {}
    external: dict[str, dict[str, str]] = {"powershell": {}, "web": {}}
    for path, source in paths.items():
        kind = source_kind(path)
        if kind == ".py":
            try:
                comments[path] = [{"text": token.string, "line": token.start[0]}
                                  for token in tokenize.tokenize(BytesIO(source).readline)
                                  if token.type == tokenize.COMMENT]
            except (SyntaxError, tokenize.TokenError) as error:
                raise ValueError(f"{path}: {error}") from error
        elif kind == ".sh" and b"#" in source:
            launcher = source.decode("utf-8")
            comments[path] = [Comment(text=token.group(), line=launcher.count("\n", 0, token.start()) + 1)
                              for token in SHELL_LAUNCHER_TOKENS.finditer(launcher) if token.lastgroup == "comment"]
        elif kind in {".ps1", ".psm1"} and b"#" in source:
            encoding = "utf-16" if source.startswith((BOM_UTF16_LE, BOM_UTF16_BE)) else "utf-8-sig"
            external["powershell"][path] = source.decode(encoding)
        elif kind in WEB_MARKERS and any(marker in source for marker in WEB_MARKERS[kind]):
            external["web"][path] = source.decode("utf-8-sig")
    for name, sources in external.items():
        if not sources:
            continue
        if name == "powershell":
            command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_SCANNER]
        else:
            common = git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.decode().strip()
            command = ["node", WEB_SCANNER, str(Path(common).parent / "desktop")]
        result = subprocess.run(command, cwd=ROOT, check=True, input=json.dumps(sources).encode("ascii"),
                                stdout=subprocess.PIPE)
        comments.update(read_scanner_comments(result.stdout, sources, name))
    return comments


def load_baseline(content: bytes) -> dict[str, Counter[str]]:
    entries = json.loads(content)
    if not isinstance(entries, dict):
        raise ValueError(f"{BASELINE} must contain a mapping from paths to comments")
    baseline = {}
    for path, comments in entries.items():
        if (not source_kind(path) or not isinstance(comments, list) or not comments
                or any(not isinstance(comment, str) or not comment for comment in comments)):
            raise ValueError(f"{BASELINE}: invalid entry for {path}")
        baseline[path] = Counter(comments)
    return baseline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Check the proposed Git index")
    parser.add_argument("--inventory", action="store_true", help="List parsed comments and line numbers as JSON")
    options = parser.parse_args()
    try:
        listing = ["--cached"] if options.staged else ["--cached", "--others", "--exclude-standard"]
        files = set(git("ls-files", "-z", *listing, "--", *SOURCE_PATHS).stdout.decode().split("\0"))
        paths = {path for path in files if source_kind(path)}
        baseline = {}
        errors = []
        selected = None
        if not options.inventory:
            content = git("show", f":{BASELINE}").stdout if options.staged else (ROOT / BASELINE).read_bytes()
            baseline = load_baseline(content)
            if options.staged:
                staged = set(git("diff", "--cached", "--name-only", "--no-renames", "-z").stdout.decode().split("\0"))
                selected = staged
                if BASELINE in staged:
                    previous = git("show", f"HEAD:{BASELINE}", check=False)
                    selected = None
                    if previous.returncode == 0:
                        old_baseline = load_baseline(previous.stdout)
                        selected = staged | {path for path in baseline.keys() | old_baseline.keys()
                                             if baseline.get(path) != old_baseline.get(path)}
                        for path, allowed in baseline.items():
                            if allowed - old_baseline.get(path, Counter()):
                                errors.append(f"{BASELINE}: cannot add baseline comments for {path}")
        if selected is not None:
            paths.intersection_update(selected)
        sources = (staged_sources(sorted(paths)) if options.staged else
                   {path: (ROOT / path).read_bytes() for path in sorted(paths) if (ROOT / path).is_file()})
        comments = scan(sources)
        if options.inventory:
            print(json.dumps({path: comments[path] for path in sorted(comments) if comments[path]}, ensure_ascii=True))
            return 0
        checked = paths | (set(baseline) & selected if selected is not None else set(baseline))
        for path in sorted(checked):
            allowed = baseline.get(path, Counter()).copy()
            for comment in comments.get(path, []):
                value = comment["text"].replace("\r\n", "\n")
                if allowed[value] > 0:
                    allowed[value] -= 1
                else:
                    errors.append(f"{path}:{comment['line']}: source comment is not in the baseline")
            if any(allowed.values()):
                errors.append(f"{BASELINE}: remove unused baseline comments for {path}")
        for error in errors:
            print(error, file=sys.stderr)
        return int(bool(errors))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Comment check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
