param(
    [Parameter(Mandatory)][string] $Group,
    [Parameter(Mandatory)][int] $NodeCount,
    [Parameter(Mandatory)][string] $NodeDigest,
    [switch] $UseIndexAlias,
    [switch] $UseSamplingPython
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or !$IsWindows) {
    throw 'This check requires a disposable GitHub-hosted Windows runner.'
}
$checkout = (Get-Item -LiteralPath $env:GITHUB_WORKSPACE).FullName
if ((Get-Location).Path -ine $checkout) {
    throw 'Run this check from the actual checkout.'
}
$python = Join-Path $checkout '.venv/Scripts/python.exe'
if ((Get-Item -LiteralPath $checkout -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'The checkout must be an ordinary directory.'
}
$scratch = Join-Path $checkout '.scratch'
$index = Join-Path $scratch 'acceptance-parent-budgets'
foreach ($path in @($scratch, $index)) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        $item = New-Item -ItemType Directory -Path $path
    }
    if (!$item.PSIsContainer -or $item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'The checkout and parent-budget index must be ordinary directories.'
    }
}
if (@(Get-ChildItem -LiteralPath $index -Force).Count) {
    throw 'The disposable checkout already has an acceptance parent index.'
}
$tempItem = Get-Item -LiteralPath $env:RUNNER_TEMP -Force
if (!$tempItem.PSIsContainer -or ($tempItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Runner temporary storage must be an ordinary directory.'
}
$runnerTemp = $tempItem.FullName
$token = [guid]::NewGuid().ToString('N')
$runtime = Join-Path $runnerTemp "myrk-frozen-$token"
$null = New-Item -ItemType Directory -Path $runtime
$mount = Join-Path $runtime 'drive'
$linkPath = Join-Path $mount 'Myrk\.scratch\acceptance-parent-budgets'
$alias = 'E:\Myrk\.scratch\acceptance-parent-budgets'
$subst = Join-Path $env:SystemRoot 'System32/subst.exe'
$mounted = $false
$linked = $false
$exitCode = 1
$oldSampler = $env:MYRK_JUDGE_SAMPLING_PYTHON
$verifyLink = 'from pathlib import Path; import sys; sys.exit(Path(sys.argv[1]).resolve() != Path(sys.argv[2]).resolve())'

try {
    if ($UseIndexAlias) {
        if ('E:\' -in [IO.DriveInfo]::GetDrives().Name -or (Test-Path -LiteralPath 'E:\')) {
            throw 'Drive E already exists. The compatibility alias was not created.'
        }
        $null = New-Item -ItemType Directory -Path $mount
        [IO.File]::WriteAllText((Join-Path $mount 'myrk-owner.txt'), $token)
        & $subst 'E:' $mount
        if ($LASTEXITCODE -ne 0) { throw 'Could not mount the disposable compatibility drive.' }
        $mounted = $true
        if ([IO.File]::ReadAllText('E:\myrk-owner.txt') -cne $token) {
            throw 'The compatibility drive does not resolve to this job.'
        }
        $null = [IO.Directory]::CreateDirectory((Split-Path -Parent $linkPath))
        $null = New-Item -ItemType Junction -Path $linkPath -Target $index
        $linked = $true
        & $python -B -c $verifyLink $alias $index
        if ($LASTEXITCODE -ne 0) { throw 'The compatibility index does not resolve to the checkout index.' }
    }

    if ($UseSamplingPython) {
        $archive = Join-Path $runtime 'python-3.13.0-embed-amd64.zip'
        $sampler = Join-Path $runtime 'sampler'
        Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.0/python-3.13.0-embed-amd64.zip' -OutFile $archive
        $expectedSha256 = '01c32d0737432240adcf0bbc1d32327f0976d3a1e1427774bc8febc8f1c03111'
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedSha256) {
            throw 'The frozen sampler archive checksum changed.'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $sampler
        $env:MYRK_JUDGE_SAMPLING_PYTHON = Join-Path $sampler 'python.exe'
        & $env:MYRK_JUDGE_SAMPLING_PYTHON -I -S -B -c 'import sys; sys.exit(sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 13, 0))'
        if ($LASTEXITCODE -ne 0) { throw 'The sampler did not start as CPython 3.13.0.' }
        Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value '- Verified the pinned CPython 3.13.0 sampler archive and runtime.'
    }

    & $python -B (Join-Path $PSScriptRoot 'windows-public-frozen.py') $Group $NodeCount $NodeDigest
    $exitCode = $LASTEXITCODE
    if (@(Get-ChildItem -LiteralPath $index -Force).Count) {
        throw 'The audited check left an acceptance parent index.'
    }
}
finally {
    $env:MYRK_JUDGE_SAMPLING_PYTHON = $oldSampler
    if ($mounted -and [IO.File]::ReadAllText('E:\myrk-owner.txt') -cne $token) {
        throw 'The compatibility drive changed during the check.'
    }
    if ($linked) {
        $link = Get-Item -LiteralPath $linkPath -Force
        if ($link.LinkType -cne 'Junction') {
            throw 'The compatibility junction no longer belongs to this check.'
        }
        & $python -B -c $verifyLink $linkPath $index
        if ($LASTEXITCODE -ne 0) { throw 'The compatibility junction target changed during the check.' }
        [IO.Directory]::Delete($linkPath, $false)
    }
    if ($mounted) {
        & $subst 'E:' '/D'
        if ($LASTEXITCODE -ne 0 -or 'E:\' -in [IO.DriveInfo]::GetDrives().Name) {
            throw 'The disposable compatibility drive was not released.'
        }
    }
    $ownedRuntime = Get-Item -LiteralPath $runtime -Force
    if ($ownedRuntime.FullName -ine (Join-Path $runnerTemp "myrk-frozen-$token") -or
        ($ownedRuntime.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        @(Get-ChildItem -LiteralPath $runtime -Force -Recurse -Attributes ReparsePoint).Count) {
        throw 'The disposable helper directory changed before cleanup.'
    }
    Remove-Item -LiteralPath $runtime -Recurse -Force
    Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value '- Removed the job-owned compatibility mount and sampler files.'
}
exit $exitCode
