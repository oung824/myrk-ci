param([Parameter(Mandatory)][string] $Group)

$ErrorActionPreference = 'Stop'
if (!$IsWindows -or $env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_ENVIRONMENT -cne 'github-hosted') {
    throw 'This check requires a disposable GitHub-hosted Windows runner.'
}
$checkout = (Get-Item -LiteralPath $env:GITHUB_WORKSPACE).FullName
if ((Get-Location).Path -ine $checkout) {
    throw 'Run this check from the public checkout.'
}
$python = Join-Path $checkout '.venv/Scripts/python.exe'
$driver = Join-Path $PSScriptRoot 'windows-public-quality-packet.py'
if ($Group -ne 'quality-packet-summary') {
    & $python -B $driver $Group
    exit $LASTEXITCODE
}

if ('E:\' -in [IO.DriveInfo]::GetDrives().Name -or (Test-Path -LiteralPath 'E:\')) {
    throw 'Drive E already exists. The Electron compatibility path was not created.'
}
$scratch = Get-Item -LiteralPath (Join-Path $checkout '.scratch') -Force
if (!$scratch.PSIsContainer -or ($scratch.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'The public checkout scratch area must be an ordinary directory.'
}
$token = [guid]::NewGuid().ToString('N')
$mount = Join-Path $scratch "public-quality-electron-$token"
$marker = Join-Path $mount $token
$junction = Join-Path $mount 'Myrk/desktop/node_modules/electron/dist'
$target = Join-Path $checkout 'desktop/node_modules/electron/dist'
$check = 'from pathlib import Path; import sys; sys.exit(Path(sys.argv[1]).resolve(strict=True) != Path(sys.argv[2]).resolve(strict=True))'
$electron = 'desktop/node_modules/electron/dist/electron.exe'
$subst = Join-Path $env:SystemRoot 'System32/subst.exe'
$mounted = $false
$linked = $false
$null = New-Item -ItemType Directory -Path $mount
try {
    $null = New-Item -ItemType File -Path $marker
    $null = [IO.Directory]::CreateDirectory((Split-Path -Parent $junction))
    $null = New-Item -ItemType Junction -Path $junction -Target $target
    $linked = $true
    & $subst 'E:' $mount
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the disposable Electron compatibility drive.' }
    $mounted = $true
    & $python -B -c $check (Join-Path 'E:/Myrk' $electron) (Join-Path $checkout $electron)
    if ($LASTEXITCODE -ne 0) { throw 'The compatibility drive does not resolve to locked Electron.' }
    & $python -B $driver $Group
    $exitCode = $LASTEXITCODE
}
finally {
    if ($mounted) {
        if (!(Test-Path -LiteralPath "E:\$token" -PathType Leaf)) {
            throw 'The compatibility drive changed during the check.'
        }
        & $subst 'E:' '/D'
        if ($LASTEXITCODE -ne 0 -or 'E:\' -in [IO.DriveInfo]::GetDrives().Name) {
            throw 'The Electron compatibility drive was not released.'
        }
    }
    if ($linked) {
        $link = Get-Item -LiteralPath $junction -Force
        if ($link.LinkType -cne 'Junction' -or $link.Target -ine $target) {
            throw 'The Electron compatibility junction changed during the check.'
        }
        [IO.Directory]::Delete($junction, $false)
    }
    if (Test-Path -LiteralPath $marker -PathType Leaf) {
        [IO.File]::Delete($marker)
    }
    foreach ($path in @('Myrk/desktop/node_modules/electron', 'Myrk/desktop/node_modules', 'Myrk/desktop', 'Myrk')) {
        $directory = Join-Path $mount $path
        if (Test-Path -LiteralPath $directory -PathType Container) {
            [IO.Directory]::Delete($directory, $false)
        }
    }
    [IO.Directory]::Delete($mount, $false)
    $cleanup = '- Removed the job-owned Electron compatibility path.'
    Write-Output $cleanup
    Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value $cleanup
}
exit $exitCode
