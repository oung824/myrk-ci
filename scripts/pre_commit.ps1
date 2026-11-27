$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$commonDirectory = & git -C $sourceRoot rev-parse --path-format=absolute --git-common-dir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$python = if ($env:MYRK_PYTHON) { $env:MYRK_PYTHON } else { Join-Path (Split-Path -Parent $commonDirectory) '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Project Python is missing: $python" }
& $python -B (Join-Path $PSScriptRoot 'pre_commit.py') @args
exit $LASTEXITCODE
