#requires -Version 7.0
param(
    [Parameter(Mandatory)]
    [ValidateSet('electron-workflows', 'python-service', 'renderer-and-state', 'pdf-and-programming',
        'diagnose-index', 'diagnose-lifecycle', 'diagnose-exit-acceptance')]
    [string]$Group
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$summaryFile = $env:GITHUB_STEP_SUMMARY
foreach ($name in @('GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_OUTPUT', 'GITHUB_PATH', 'GITHUB_STATE')) {
    [Environment]::SetEnvironmentVariable($name, $null)
}

function Write-CiSummary([string]$Message) {
    Write-Host $Message
    if ($summaryFile) {
        Add-Content -LiteralPath $summaryFile -Value $Message -Encoding utf8
    }
}

$groups = @{
    'electron-workflows' = @(
        'src/main/approval-modes.integration.test.ts'
        'src/main/connection-persistence.test.ts'
        'src/main/connection-profile-context.test.ts'
        'src/main/connection-store.integration.test.ts'
        'src/main/exit-acceptance.integration.test.ts'
        'src/main/image-preview-electron.test.ts'
        'src/main/lifecycle.test.ts'
        'src/main/recovery.integration.test.ts'
    )
    'python-service' = @(
        'src/main/connection-handoff.test.ts'
        'src/main/connection-manager.test.ts'
        'src/main/connection-store.test.ts'
        'src/main/index.test.ts'
        'src/main/python-service.test.ts'
        'src/renderer/App.integration.test.tsx'
        'src/renderer/Compaction.integration.test.tsx'
        'src/renderer/Memory.integration.test.tsx'
    )
    'renderer-and-state' = @(
        'src/renderer/App.layout.test.tsx'
        'src/renderer/App.preview.test.tsx'
        'src/renderer/App.test.tsx'
        'src/renderer/ModelControls.test.tsx'
        'src/renderer/RequestObservationDetails.test.tsx'
        'src/renderer/SettingsDialog.test.tsx'
        'src/shared/artifacts.test.ts'
        'src/shared/decode.test.ts'
    )
    'pdf-and-programming' = @(
        'src/main/document-preview.serial.test.ts'
        'src/main/document-preview-electron.serial.test.ts'
        'src/main/workflow-acceptance.serial.test.ts'
    )
}
$localOfficeFile = 'src/main/office-preview.serial.test.ts'
$publicSerialCases = @(
    'a PDF preview keeps the exact fixed bytes after its caller changes the input buffer'
    'the built desktop renders a real delivered PDF with a local worker, page navigation and zoom'
    'the narrow production desktop reserves readable document and chat areas at 125 percent zoom'
    'the production desktop completes the programming workflow with saved real deliveries and reading controls'
)
$diagnosticGroups = @{
    'diagnose-index' = @{
        file = 'src/main/index.test.ts'
        cases = @(
            'a native folder selection belongs to its conversation and cannot authorize another conversation'
            'a delivered artifact opens a fixed current snapshot only in its owning active conversation'
        )
        sources = @('desktop/src/main/index.ts', 'desktop/src/main/python-service.ts', 'tests/desktop_fixture.py')
    }
    'diagnose-lifecycle' = @{
        file = 'src/main/lifecycle.test.ts'
        cases = @(
            'real hidden desktop closes, reopens and waits for owned work; blocked save=false'
            'real hidden desktop closes, reopens and waits for owned work; blocked save=true'
        )
        sources = @('desktop/tests/lifecycle.electron.mjs', 'desktop/tests/reopen.electron.mjs', 'desktop/tests/desktop-process.ts',
            'desktop/tests/process-observation.mjs', 'tests/desktop_fixture.py')
    }
    'diagnose-exit-acceptance' = @{
        file = 'src/main/exit-acceptance.integration.test.ts'
        cases = @(
            'real desktop exits and reopens without rerunning owned work: running'
            'real desktop exits and reopens without rerunning owned work: approval'
            'real desktop exits and reopens without rerunning owned work: timeout'
        )
        sources = @('desktop/tests/exit-acceptance.electron.mjs', 'desktop/tests/desktop-process.ts',
            'desktop/tests/process-observation.mjs', 'tests/exit_acceptance_fixture.py', 'tests/support/observed_check_runner.py')
    }
}

function Write-DesktopDiagnostic($Diagnostic, $Module, [string]$Root) {
    $publishedTest = "desktop/$($Diagnostic.file)"
    $lineLimits = @{}
    foreach ($source in (@($publishedTest) + $Diagnostic.sources)) {
        $lineLimits[$source] = ([IO.File]::ReadLines((Join-Path $Root $source)) | Measure-Object).Count
    }
    foreach ($case in $Module.assertionResults) {
        if ($case.status -eq 'passed') { continue }
        $caseNumber = [array]::IndexOf($Diagnostic.cases, [string]$case.title) + 1
        if ($caseNumber -eq 0) {
            Write-CiSummary '- Desktop diagnostic signal: unknown-test-name.'
            continue
        }
        $details = @($case.failureMessages) -join "`n"
        $signal = switch -Regex ($details) {
            '(?i)\b(?:timeout(?:error)?|timed out)(?:\s*:|\s+waiting|\s+in\s+\d)|Test timed out' { 'timeout'; break }
            '(?i)\b(?:EPERM|EACCES|EBUSY)\b|\bWinError (?:5|32)\b|access is denied|being used by another process' { 'access-or-sharing'; break }
            '(?i)\b(?:ENOENT|FileNotFoundError)\b|\bWinError (?:2|3)\b' { 'file-or-path-missing'; break }
            '(?i)AssertionError|Assertion failed' { 'assertion-failure'; break }
            default { if ($case.status -eq 'failed') { 'test-failure' } else { 'test-incomplete' } }
        }
        Write-CiSummary "- Desktop diagnostic case ${caseNumber}: $signal."
        $declaration = [int]$case.location.line
        if ($declaration -gt 0 -and $declaration -le $lineLimits[$publishedTest]) {
            Write-CiSummary "- Test source: ${publishedTest}:$declaration."
        }
        $normalized = [regex]::Replace($details, '[\\/]+', '/')
        foreach ($source in $lineLimits.Keys | Sort-Object) {
            $relative = if ($source.StartsWith('desktop/')) { $source.Substring('desktop/'.Length) } else { $source }
            $pathPattern = [regex]::Escape($source) + '|' + [regex]::Escape($relative)
            $frame = [regex]::Match($normalized, '(?:^|[\s/(''\"])(?:' + $pathPattern + ')(?::|[\x22''], line )([1-9]\d{0,4})(?!\d)', 'IgnoreCase')
            if ($frame.Success -and [int]$frame.Groups[1].Value -le $lineLimits[$source]) {
                Write-CiSummary "- Failure source: ${source}:$($frame.Groups[1].Value)."
            }
        }
    }
}

$timer = [Diagnostics.Stopwatch]::StartNew()
$stage = 'prepare'
try {
    $root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
    $desktop = Join-Path $root 'desktop'
    $checkRoot = Join-Path $root '.scratch/check-runs'
    $run = Join-Path $root '.scratch/github-ci/public-desktop'
    $temporary = Join-Path $run 'tmp'
    New-Item -ItemType Directory -Path $temporary -Force | Out-Null
    $env:TEMP = $env:TMP = $env:TMPDIR = $temporary
    $env:npm_config_cache = Join-Path $root '.scratch/github-ci/npm-cache'
    $env:MYRK_PYTHON = (Resolve-Path -LiteralPath (Join-Path $root '.venv/Scripts/python.exe')).Path
    $env:MYRK_TASK_ID = 'github-windows-public-desktop'
    $env:PYTHONIOENCODING = 'utf-8'
    Set-Location -LiteralPath $desktop

    Write-CiSummary "### Desktop $Group"
    $stage = 'file-selection'
    $inventory = @(Get-ChildItem -LiteralPath 'src' -File -Recurse |
        Where-Object Name -Match '\.test\.tsx?$' |
        ForEach-Object { [IO.Path]::GetRelativePath($desktop, $_.FullName).Replace('\', '/') } |
        Sort-Object)
    $assigned = @($groups.Values | ForEach-Object { $_ }) + $localOfficeFile
    if (($inventory -join "`n") -cne (($assigned | Sort-Object) -join "`n")) {
        throw 'Desktop file assignments differ from the test inventory.'
    }
    $diagnostic = $diagnosticGroups[$Group]
    $selected = if ($diagnostic) { @($diagnostic.file) } else { $groups[$Group] }
    $selected = @($selected)
    $arguments = $selected
    $filteredSerial = $Group -eq 'pdf-and-programming'
    if ($filteredSerial) {
        $namePattern = '^(?:' + ($publicSerialCases -join '|') + ')$'
        $arguments += @('--testNamePattern', $namePattern)
    }
    if ($diagnostic) {
        $arguments += @('--reporter', 'json', '--includeTaskLocation')
    }
    Write-CiSummary "- Selected Vitest files: $($selected.Count)."

    $stage = 'npm-test'
    $stdout = Join-Path $run 'desktop.stdout.txt'
    $stderr = Join-Path $run 'desktop.stderr.txt'
    & npm.cmd test -- @arguments > $stdout 2> $stderr
    $exitCode = $LASTEXITCODE
    Write-CiSummary "- Managed desktop exit code: $exitCode."

    $stage = 'node-contract-counts'
    $contracts = @{}
    foreach ($line in (Get-Content -LiteralPath $stdout)) {
        if ($line -match '^(?:#|ℹ)\s+(tests|pass|fail|cancelled|skipped|todo)\s+([0-9]+)$') {
            $contracts[$Matches[1]] = [int]$Matches[2]
        }
    }
    if ($contracts.Count -eq 6) {
        Write-CiSummary "- Node contracts: $($contracts.tests) collected, $($contracts.pass) passed, $($contracts.fail) failed, $($contracts.cancelled) cancelled, $($contracts.skipped) skipped, $($contracts.todo) todo."
        if ($contracts.fail -gt 0) {
            $sources = @(Get-ChildItem -LiteralPath 'tests' -File -Filter '*.test.mjs' |
                ForEach-Object { Get-Content -Raw -LiteralPath $_.FullName }) -join "`n"
            foreach ($line in (Get-Content -LiteralPath $stdout)) {
                if ($line -match '^✖ ([A-Za-z0-9 .,''/-]{10,155}) \([0-9.]+ms\)$' -and $sources.Contains($Matches[1])) {
                    Write-CiSummary "- Failed Node contract: $($Matches[1])."
                }
            }
        }
    } else {
        Write-CiSummary '- Node contract counts unavailable.'
    }

    $stage = 'desktop-receipt'
    $results = Join-Path $checkRoot 'results'
    if (!(Test-Path -LiteralPath $results -PathType Container)) {
        throw 'The desktop runner did not create a result directory.'
    }
    $receipts = @(Get-ChildItem -LiteralPath $results -File -Filter 'desktop-run-*.json')
    if ($receipts.Count -ne 1 -or $receipts[0].BaseName -cnotmatch '^desktop-run-[A-Za-z0-9]+$') {
        throw 'There is no unique desktop result receipt for this job.'
    }
    $receipt = Get-Content -LiteralPath $receipts[0].FullName -Raw | ConvertFrom-Json
    $expectedRuntime = Join-Path $checkRoot 'runtime-tmp' $receipts[0].BaseName.Substring('desktop-'.Length)
    $runtimeRemoved = $receipt.directory -ieq $expectedRuntime -and !(Test-Path -LiteralPath $expectedRuntime)
    $cleanupVerified = $receipt.generatedRemoved -eq $true
    $configurationRemoved = $null -ne $receipt.temporaryConfigFiles -and @($receipt.temporaryConfigFiles).Count -eq 0
    $protectedUnchanged = $receipt.protectedUnchanged -eq $true
    Write-CiSummary "- Generated runtime cleanup recorded: $cleanupVerified. Runtime directory absent: $runtimeRemoved."
    Write-CiSummary "- Temporary configuration removed: $configurationRemoved. Protected generated locations unchanged: $protectedUnchanged."

    $diagnosticVerified = $true
    if ($diagnostic) {
        $diagnosticVerified = $false
        $diagnosticReport = Join-Path $results "$($receipts[0].BaseName)-reports" 'json/output.json'
        if ($diagnosticReport -iin @($receipt.reports) -and (Test-Path -LiteralPath $diagnosticReport -PathType Leaf)) {
            $report = Get-Content -LiteralPath $diagnosticReport -Raw | ConvertFrom-Json
            $modules = @($report.testResults)
            $expectedModule = Join-Path $desktop $diagnostic.file
            if ($modules.Count -eq 1 -and ([string]$modules[0].name).Replace('/', '\') -ieq $expectedModule) {
                $module = $modules[0]
                $actualCases = ($module.assertionResults.title | Sort-Object) -join "`n"
                $expectedCases = ($diagnostic.cases | Sort-Object) -join "`n"
                $diagnosticVerified = $actualCases -ceq $expectedCases
                Write-DesktopDiagnostic $diagnostic $module $root
            }
        }
        Write-CiSummary "- Diagnostic case selection verified: $diagnosticVerified."
    }

    $stage = 'vitest-coverage'
    $schedule = $receipt.scheduling
    $discovered = @($schedule.discovered | Where-Object { $_ })
    $executed = @($schedule.executed | Where-Object { $_ })
    $expected = ($selected | Sort-Object) -join "`n"
    $coverageVerified = (($discovered.path | Sort-Object) -join "`n") -ceq $expected -and
        (($executed.path | Sort-Object) -join "`n") -ceq $expected
    Write-CiSummary "- Vitest files: $($discovered.Count) discovered, $($executed.Count) executed. Selection verified: $coverageVerified."
    if ($null -ne $schedule.unhandled_errors) {
        $total = [int](($executed.test_cases | Measure-Object -Property collected -Sum).Sum)
        $passed = [int](($executed.test_cases | Measure-Object -Property passed -Sum).Sum)
        $requested = $total
        if ($filteredSerial) { $requested = $publicSerialCases.Count }
        Write-CiSummary "- Vitest cases: $total collected, $requested requested, $passed passed. Unhandled errors: $([int]$schedule.unhandled_errors)."
        if ($passed -ne $requested -and !$filteredSerial) {
            foreach ($file in $executed) {
                if ($file.path -cin $selected -and [int]$file.test_cases.passed -lt [int]$file.test_cases.collected) {
                    Write-CiSummary "- Vitest incomplete file: $($file.path), $([int]$file.test_cases.passed)/$([int]$file.test_cases.collected) passed."
                }
            }
        }
    } else {
        Write-CiSummary '- Vitest case counts unavailable.'
    }
    $stage = 'result-verification'
    if ($exitCode -ne 0 -or $receipt.result -ne 0 -or !$coverageVerified -or !$diagnosticVerified -or !$cleanupVerified -or !$runtimeRemoved -or
        !$configurationRemoved -or !$protectedUnchanged -or $schedule.unhandled_errors -ne 0 -or $requested -lt 1 -or
        $passed -ne $requested -or $contracts.Count -ne 6 -or $contracts.tests -lt 1 -or $contracts.pass -ne $contracts.tests) {
        throw 'The managed desktop result did not pass verification.'
    }
    Write-CiSummary "- Desktop behavior passed in $([Math]::Round($timer.Elapsed.TotalSeconds, 3)) s."
} catch {
    Write-CiSummary "- Desktop behavior failed at $stage in $([Math]::Round($timer.Elapsed.TotalSeconds, 3)) s."
    exit 1
}
