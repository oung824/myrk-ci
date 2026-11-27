#requires -Version 7.0
param(
    [Parameter(Mandatory)]
    [ValidateSet('install', 'check')]
    [string]$Phase,
    [Parameter(Mandatory)]
    [ValidateSet('python-types', 'backend-records', 'backend-executor', 'typescript-types', 'desktop-contracts', 'office-com')]
    [string]$Check
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
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

function Invoke-CiNative {
    param([string]$Program, [string[]]$Arguments, [switch]$AllowFailure)

    & $Program @Arguments > $stdoutFile 2> $stderrFile
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $script:nativeExitCode = $exitCode
        throw 'A pilot command failed.'
    }
    if ($AllowFailure) {
        return $exitCode
    }
}

function Write-PytestResult($Receipt) {
    $reportDirectory = @($Receipt.retained_reports.paths)
    if ($reportDirectory.Count -ne 1 -or -not $reportDirectory[0]) {
        Write-CiSummary '- Pytest counts unavailable.'
        return $false
    }

    $report = Join-Path $reportDirectory[0] 'pilot-results.xml'
    $settings = [System.Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $reader = [System.Xml.XmlReader]::Create($report, $settings)
    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.Load($reader)
    } finally {
        $reader.Dispose()
    }
    $suite = $document.SelectSingleNode('/testsuites/testsuite')
    $tests = [int]$suite.GetAttribute('tests')
    $failed = [int]$suite.GetAttribute('failures')
    $errors = [int]$suite.GetAttribute('errors')
    $skipped = [int]$suite.GetAttribute('skipped')
    $passed = $tests - $failed - $errors - $skipped
    Write-CiSummary "- Pytest counts: $tests collected, $passed passed, $failed failed, $errors errors, $skipped skipped."
    if ($Check -eq 'backend-executor' -and ($failed + $errors) -gt 0) {
        $failureText = ($suite.SelectNodes('testcase/failure | testcase/error') | ForEach-Object { $_.InnerText }) -join "`n"
        $locations = [regex]::Matches($failureText, '(?:tests[\\/]test_executor\.py|myrk[\\/]executor\.py):\d+') |
            ForEach-Object Value | Select-Object -Unique
        if ($locations) {
            Write-CiSummary "- Executor failure frames: $($locations -join ', ')."
        }
        $conditions = [ordered]@{
            'mxc-unavailable-or-failed' = '(?i)\b(?:mxc|windowsmxc)\b'
            'access-denied' = '(?i)access (?:is )?denied|permissionerror|0x80070005'
            'path-not-found' = '(?i)filenotfounderror|file not found|(?:file|path) specified|no python at'
            'invalid-parameter' = '(?i)parameter is incorrect|invalid parameter'
            'unsupported-operation' = '(?i)not supported|not implemented'
            'timeout' = '(?i)timeouterror|timed out'
            'assertion' = '(?i)assertionerror'
        }
        foreach ($condition in $conditions.GetEnumerator()) {
            if ($failureText -match $condition.Value) {
                Write-CiSummary "- Executor failure signal: $($condition.Key)."
            }
        }
        $exitMatch = [regex]::Match($failureText, '(?m)^E\s+assert (-?[0-9]{1,12}) == 0\s*$')
        if ($exitMatch.Success) {
            Write-CiSummary "- Executor child exit code: $($exitMatch.Groups[1].Value)."
        }
    }
    return ($tests -gt 0 -and $passed -eq $tests)
}

function Invoke-OfficeProbe {
    foreach ($application in @('Word', 'Excel', 'PowerPoint')) {
        $progId = "$application.Application"
        if ($null -eq [type]::GetTypeFromProgID($progId, $false)) {
            Write-CiSummary "- ${application}: COM class unavailable."
            continue
        }

        $instance = $null
        try {
            $instance = New-Object -ComObject $progId
            [void]$instance.Quit()
            Write-CiSummary "- ${application}: COM application started and quit."
        } catch {
            Write-CiSummary "- ${application}: COM class registered, start or quit failed."
        } finally {
            if ($null -ne $instance) {
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($instance)
            }
        }
    }
}

$timer = [Diagnostics.Stopwatch]::StartNew()
$stage = 'prepare'
$nativeExitCode = $null
try {
    $root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
    Set-Location -LiteralPath $root
    $run = Join-Path $root '.scratch/github-ci'
    $temporary = Join-Path $run 'tmp'
    New-Item -ItemType Directory -Path $temporary -Force | Out-Null
    $env:TEMP = $env:TMP = $env:TMPDIR = $temporary
    $env:npm_config_cache = Join-Path $run 'npm-cache'
    $env:electron_config_cache = Join-Path $run 'electron-cache'
    $env:MYRK_TEST_RUN_DIRECTORY = $run
    $env:MYRK_TASK_ID = 'github-windows-pilot'
    $env:PYTHONIOENCODING = 'utf-8'
    $stdoutFile = Join-Path $run "$Phase.stdout.txt"
    $stderrFile = Join-Path $run "$Phase.stderr.txt"
    $python = Join-Path $root '.venv/Scripts/python.exe'

    if ($Phase -eq 'install' -or $Check -eq 'office-com') {
        Write-CiSummary "### $Check"
        Write-CiSummary 'Pilot on windows-2025. Selected offline checks only. Full backend and desktop behavior suites and Office document conversion are outside this run.'
        if ($env:ImageVersion -match '^\d{4}(?:\.\d+){1,5}$') {
            Write-CiSummary "- Runner image version: $($env:ImageVersion)."
        }
        Write-CiSummary "- Available logical processors: $([Environment]::ProcessorCount)."
    }

    if ($Phase -eq 'install') {
        if ($Check -in @('typescript-types', 'desktop-contracts')) {
            $stage = 'node-version'
            Invoke-CiNative -Program node.exe -Arguments @('-e', "if (process.version !== 'v24.20.0' || process.arch !== 'x64') process.exit(1)")
            Set-Location -LiteralPath (Join-Path $root 'desktop')
            $stage = 'npm-install'
            Invoke-CiNative -Program npm.cmd -Arguments @('ci', '--no-audit', '--no-fund')
            $stage = 'electron-install'
            Invoke-CiNative -Program node.exe -Arguments @('-e', "if (!require('node:fs').existsSync(require('electron'))) process.exit(1)")
            Write-CiSummary '- Node.js 24.20.0 x64, npm lock and Electron executable verified.'
        } else {
            $stage = 'python-download'
            $asset = 'cpython-3.12.14%2B20260924-x86_64-pc-windows-msvc-install_only_stripped.tar.gz'
            $archive = Join-Path $run 'python.tar.gz'
            Invoke-WebRequest -Uri "https://github.com/astral-sh/python-build-standalone/releases/download/20260924/$asset" -OutFile $archive
            $stage = 'python-checksum'
            $digest = 'c5bf8edfe858c1df9891be498b5bbc8761d383df5b9790658b088fea4870433a'
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $digest) {
                throw 'Python archive checksum does not match.'
            }
            $stage = 'python-extract'
            Invoke-CiNative -Program tar.exe -Arguments @('-xzf', $archive, '-C', $run)
            $stage = 'python-environment'
            $basePython = Join-Path $run 'python/python.exe'
            Invoke-CiNative -Program $basePython -Arguments @('-B', '-m', 'venv', '.venv')
            Invoke-CiNative -Program $python -Arguments @('-B', '-c', 'import struct, sys; sys.exit(0 if sys.version_info[:3] == (3, 12, 14) and struct.calcsize("P") == 8 else 1)')
            $stage = 'pip-install'
            Invoke-CiNative -Program $python -Arguments @('-B', '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-cache-dir', 'install', '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--no-deps', '-r', 'requirements.lock')
            $stage = 'pip-check'
            Invoke-CiNative -Program $python -Arguments @('-B', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'check')
            Write-CiSummary '- CPython 3.12.14 x64 archive checksum and Python lock installation verified.'
            if ($Check -eq 'backend-executor') {
                $stage = 'executor-install'
                $powerShell = Join-Path $PSHOME 'pwsh.exe'
                Invoke-CiNative -Program $powerShell -Arguments @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', 'scripts/setup-executor.ps1')
                Write-CiSummary '- Pinned executor archive and executable checksums verified.'
            }
        }
    } else {
        $stage = $Check
        $exitCode = 0
        switch ($Check) {
            'office-com' { Invoke-OfficeProbe }
            'typescript-types' {
                Set-Location -LiteralPath (Join-Path $root 'desktop')
                $exitCode = Invoke-CiNative -Program npm.cmd -Arguments @('run', 'typecheck') -AllowFailure
            }
            'desktop-contracts' {
                Set-Location -LiteralPath (Join-Path $root 'desktop')
                $exitCode = Invoke-CiNative -Program node.exe -Arguments @('--test', '--test-reporter=tap', 'tests/run-context.test.mjs', 'tests/run-runner.test.mjs') -AllowFailure
                foreach ($line in Get-Content -LiteralPath $stdoutFile) {
                    if ($line -match '^# (tests|pass|fail|cancelled|skipped|todo) ([0-9]+)$') {
                        Write-CiSummary "- Node test $($Matches[1]): $([int]$Matches[2])."
                    }
                }
            }
            default {
                $arguments = @('-B', 'scripts/run_checks.py')
                if ($Check -eq 'python-types') {
                    $arguments += 'mypy'
                } else {
                    $arguments += @('pytest', '-q', '--junitxml=pilot-results.xml')
                    if ($Check -eq 'backend-records') {
                        $arguments += @('tests/test_conversation_format.py', 'tests/test_context_task_goal.py')
                    } else {
                        $arguments += 'tests/test_executor.py::test_official_program_returns_all_output_after_the_program_has_exited'
                    }
                }
                $exitCode = Invoke-CiNative -Program $python -Arguments $arguments -AllowFailure
                $receipt = Get-Content -LiteralPath $stdoutFile -Raw | ConvertFrom-Json
                if ($receipt.runtime_removed -is [bool]) {
                    Write-CiSummary "- Backend runtime removed: $($receipt.runtime_removed)."
                }
                if ($Check -ne 'python-types') {
                    $allPassed = Write-PytestResult $receipt
                    if ($exitCode -eq 0 -and -not $allPassed) { $exitCode = 1 }
                }
            }
        }
        if ($exitCode -ne 0) {
            $nativeExitCode = $exitCode
            throw 'A pilot check failed.'
        }
    }
    Write-CiSummary "- $Phase passed in $([int]$timer.Elapsed.TotalSeconds) s."
} catch {
    Write-CiSummary "- $Phase failed at $stage in $([int]$timer.Elapsed.TotalSeconds) s."
    if ($null -ne $nativeExitCode) {
        Write-CiSummary "- Exit code: $nativeExitCode."
    }
    exit 1
}
