#requires -Version 7.0
param([string]$SourceBinary)

$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$release = Get-Content -LiteralPath (Join-Path $repositoryRoot 'myrk/executor-release.json') -Raw | ConvertFrom-Json
$destinationDirectory = Join-Path $repositoryRoot "myrk/runtime/$($release.release)"
$destination = Join-Path $destinationDirectory $release.executable

if (Test-Path -LiteralPath $destination) {
    if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $release.sha256) {
        throw '执行组件的文件指纹（用于核对文件内容的编号）不匹配；保留原文件，未启动或覆盖。'
    }
    Write-Output "执行组件已通过核对：$destination"
    exit 0
}

if ($SourceBinary) {
    $candidate = (Resolve-Path -LiteralPath $SourceBinary).Path
} else {
    $downloadDirectory = Join-Path $repositoryRoot ('.myrk/executor-download/' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $downloadDirectory | Out-Null
    $archive = Join-Path $downloadDirectory 'official.zip'
    Write-Output '正在获取固定版本的执行组件；不会安装系统账号或请求管理员初始化。'
    Invoke-WebRequest -Uri $release.archive_url -OutFile $archive
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $release.archive_sha256) {
        throw "下载文件指纹不匹配；未解压或执行，原文件保留在：$archive"
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $downloadDirectory
    $candidate = Join-Path $downloadDirectory $release.executable
}

if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant() -ne $release.sha256) {
    throw "执行组件文件指纹不匹配；未复制或执行，原文件保留在：$candidate"
}
New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
Copy-Item -LiteralPath $candidate -Destination $destination
if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $release.sha256) {
    throw "复制后的执行组件核对失败；未启动，文件保留在：$destination"
}
if (-not $SourceBinary) {
    Remove-Item -LiteralPath $archive
    Remove-Item -LiteralPath $candidate
    if ((Get-ChildItem -LiteralPath $downloadDirectory -Force).Count -eq 0) {
        Remove-Item -LiteralPath $downloadDirectory
    }
}
Write-Output "执行组件已通过核对：$destination"
