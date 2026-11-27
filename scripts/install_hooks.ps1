$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot '.githooks/pre-commit') -PathType Leaf)) {
    throw 'The repository pre-commit hook is missing.'
}
$configured = & git -C $sourceRoot config --get core.hooksPath
if ($LASTEXITCODE -notin 0, 1) { exit $LASTEXITCODE }
if ($configured -and $configured -ne '.githooks') {
    throw "Existing repository hook configuration was preserved: $configured"
}
& git -C $sourceRoot config --local core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output 'Myrk repository pre-commit hook installed: core.hooksPath=.githooks'
