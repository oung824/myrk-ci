param([Parameter(Mandatory)][string]$Directory, [string]$SourcePath, [ValidateSet('convert','cleanup')][string]$Mode='convert')
$ErrorActionPreference='Stop'
$env:TEMP=Join-Path $Directory 'temp'; $env:TMP=$env:TEMP; $env:TMPDIR=$env:TEMP
$ownerPath=Join-Path $Directory 'office-owner.json'

function Test-OfficeArchive([IO.Compression.ZipArchive]$Archive, [bool]$AllowChartWorkbook) {
  if ($Archive.Entries.Count -gt 10000 -or ($Archive.Entries | Measure-Object Length -Sum).Sum -gt 536870912) { throw 'Office package is too large to preview safely' }
  if (-not $Archive.GetEntry('[Content_Types].xml')) { throw 'Invalid Office package' }
  foreach ($entry in $Archive.Entries) {
    if ($entry.FullName -match '(?i)(vbaProject|vbaData|/macrosheets/|/activeX/|/externalLinks/|connections\.xml$)') { throw 'Office preview rejects macros, embedded objects and external data' }
    if ($entry.FullName -match '(?i)/embeddings/') {
      if (-not $AllowChartWorkbook -or $entry.FullName -notmatch '(?i)^ppt/embeddings/[^/]+\.xlsx$') { throw 'Office preview rejects embedded executable objects' }
      $stream=$entry.Open()
      $embedded=[IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Read)
      try { Test-OfficeArchive $embedded $false } finally { $embedded.Dispose(); $stream.Dispose() }
    }
    if ($entry.FullName.EndsWith('.xml') -or $entry.FullName.EndsWith('.rels')) {
      $stream=$entry.Open()
      $settings=[Xml.XmlReaderSettings]::new(); $settings.DtdProcessing=[Xml.DtdProcessing]::Prohibit; $settings.XmlResolver=$null
      $reader=[Xml.XmlReader]::Create($stream,$settings)
      try {
        $xml=[Xml.XmlDocument]::new(); $xml.XmlResolver=$null; $xml.Load($reader)
        foreach ($node in $xml.DocumentElement.ChildNodes) {
          if ($node -isnot [Xml.XmlElement]) { continue }
          if ($node.GetAttribute('TargetMode') -eq 'External' -or $node.GetAttribute('ContentType') -match '(?i)(macroEnabled|vbaProject)' -or $node.GetAttribute('Type') -match '(?i)/(oleObject|attachedTemplate)$') { throw 'Office preview rejects macros and external links' }
        }
        foreach ($field in $xml.SelectNodes('//*[local-name()="instrText"]')) {
          if ($field.InnerText -match '(?i)\b(DDE|DDEAUTO|LINK|INCLUDETEXT|INCLUDEPICTURE)\b') { throw 'Office preview rejects external document fields' }
        }
      } finally { $reader.Dispose(); $stream.Dispose() }
    }
  }
}

if ($Mode -eq 'cleanup') {
  if (Test-Path -LiteralPath $ownerPath) {
    $owner=Get-Content -LiteralPath $ownerPath -Raw | ConvertFrom-Json
    $owned=Get-Process -Id $owner.pid -ErrorAction SilentlyContinue
    if ($null -ne $owned) {
      if ($owned.ProcessName -ne $owner.name -or $owned.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -ne $owner.creation_file_time) { throw 'Office ownership no longer matches; process was not stopped' }
      if (-not $owned.WaitForExit(3000)) { Stop-Process -InputObject $owned -Force -ErrorAction Stop; $owned.WaitForExit() }
    }
  } elseif (Test-Path -LiteralPath (Join-Path $Directory 'office-launch.json')) {
    $launch=Get-Content -LiteralPath (Join-Path $Directory 'office-launch.json') -Raw | ConvertFrom-Json
    $unproven=@(Get-Process -Name $launch.name -ErrorAction SilentlyContinue | Where-Object { $_.StartTime.ToUniversalTime() -ge [DateTime]$launch.started_at })
    if ($unproven.Count -gt 0) { throw 'An Office process appeared before ownership was recorded; retain conversion evidence and do not stop that process' }
  }
  exit 0
}

$started=(Get-Date).ToUniversalTime()
$worker=Get-Process -Id $PID
@{ owner='Myrk Office preview'; path=$Directory; purpose='Fixed local read-only Office conversion'; pid=$PID; process_started_at=$worker.StartTime.ToUniversalTime().ToString('o'); created_at=$started.ToString('o'); delete_when='Preview closes or conversion fails' } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Directory 'owner.json')
$result=@{ engine='microsoft-office'; status='failed'; sections=@(); started_at=$started.ToString('o') }
$application=$null; $document=$null; $ownedProcess=$null
try {
  $extension=[IO.Path]::GetExtension($SourcePath).ToLowerInvariant()
  if ($extension -notin @('.docx','.xlsx','.pptx')) { throw 'Office preview supports only .docx, .xlsx and .pptx' }
  $result.sourceSha256=(Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
  $archive=$null
  try {
    $archive=[IO.Compression.ZipFile]::OpenRead($SourcePath)
    Test-OfficeArchive $archive ($extension -eq '.pptx')
  } catch {
    $cause=$_.Exception
    while ($null -ne $cause.InnerException) { $cause=$cause.InnerException }
    if ($cause -is [IO.InvalidDataException] -or $cause -is [Xml.XmlException]) { throw "Invalid Office package: $($cause.Message)" }
    throw
  } finally { if ($null -ne $archive) { $archive.Dispose() } }
  if (Test-Path -LiteralPath (Join-Path $Directory 'cancel.flag')) { throw 'Office preview cancelled' }
  $processName=switch ($extension) { '.docx' { 'WINWORD' } '.xlsx' { 'EXCEL' } '.pptx' { 'POWERPNT' } }
  $comName=switch ($extension) { '.docx' { 'Word.Application' } '.xlsx' { 'Excel.Application' } '.pptx' { 'PowerPoint.Application' } }
  $displayName=switch ($extension) { '.docx' { 'Word' } '.xlsx' { 'Excel' } '.pptx' { 'PowerPoint' } }
  if (Get-Process -Name $processName -ErrorAction SilentlyContinue) { throw "本机 $displayName 正在运行。为避免影响已打开的文档，请先关闭 $displayName 后再预览。" }
  @{ name=$processName; started_at=$started.ToString('o'); preexisting_processes=@(); worker_pid=$PID } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Directory 'office-launch.json')
  $application=New-Object -ComObject $comName
  $fresh=@(Get-Process -Name $processName -ErrorAction Stop | Where-Object { $_.StartTime.ToUniversalTime() -ge $started })
  if ($fresh.Count -ne 1) { throw 'Cannot prove ownership of a unique fresh Office process' }
  $ownedProcess=$fresh[0]
  @{ pid=$ownedProcess.Id; name=$ownedProcess.ProcessName; creation_file_time=$ownedProcess.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(); created_at=$ownedProcess.StartTime.ToUniversalTime().ToString('o'); worker_pid=$PID } | ConvertTo-Json | Set-Content -LiteralPath $ownerPath
  if (Test-Path -LiteralPath (Join-Path $Directory 'cancel.flag')) { throw 'Office preview cancelled' }
  $application.AutomationSecurity=3
  if ($extension -eq '.docx') {
    $application.Visible=$false; $application.DisplayAlerts=0
    $application.Options.UpdateLinksAtOpen=$false
    $document=$application.Documents.Open($SourcePath,$false,$true,$false)
    if (Test-Path -LiteralPath (Join-Path $Directory 'cancel.flag')) { throw 'Office preview cancelled' }
    $pdf=Join-Path $Directory 'document.pdf'
    $document.ExportAsFixedFormat($pdf,17)
    $result.sections=@(@{ id='document'; label='文档'; pdfPath=$pdf })
    $result.pages=$document.ComputeStatistics(2)
  } elseif ($extension -eq '.xlsx') {
    $application.Visible=$false; $application.DisplayAlerts=$false
    $application.AskToUpdateLinks=$false; $application.EnableEvents=$false
    $document=$application.Workbooks.Open($SourcePath,0,$true)
    for ($index=1; $index -le $document.Sheets.Count; $index++) {
      if (Test-Path -LiteralPath (Join-Path $Directory 'cancel.flag')) { throw 'Office preview cancelled' }
      $sheet=$document.Sheets.Item($index)
      try {
        if ($sheet.Visible -ne -1) { continue }
        $pdf=Join-Path $Directory "sheet-$index.pdf"
        $sheet.ExportAsFixedFormat(0,$pdf,0,$false,$true)
        $result.sections+=@{ id="sheet-$index"; label=$sheet.Name; pdfPath=$pdf }
      } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($sheet) }
    }
  } else {
    $application.DisplayAlerts=1
    $document=$application.Presentations.Open($SourcePath,-1,0,0)
    for ($index=1; $index -le $document.Slides.Count; $index++) {
      if (Test-Path -LiteralPath (Join-Path $Directory 'cancel.flag')) { throw 'Office preview cancelled' }
      $document.PrintOptions.Ranges.ClearAll()
      $range=$document.PrintOptions.Ranges.Add($index,$index)
      try {
        $pdf=Join-Path $Directory "slide-$index.pdf"
        $document.ExportAsFixedFormat($pdf,2,2,0,1,1,0,$range,4)
        $result.sections+=@{ id="slide-$index"; label="幻灯片 $index"; pdfPath=$pdf }
      } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($range) }
    }
  }
  $result.read_only=$document.ReadOnly; $result.office_version=$application.Version
  if ((Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $result.sourceSha256) { throw 'Office changed the fixed source snapshot' }
  $result.status='converted'
} catch { $result.error=$_.Exception.Message }
finally {
  if ($null -ne $document) {
    if ($extension -eq '.pptx') { $document.Close() } else { $document.Close(0) }
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document)
  }
  if ($null -ne $application -and $null -ne $ownedProcess) {
    if ($extension -eq '.docx') { $application.Quit(0) } else { $application.Quit() }
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application)
  }
  [GC]::Collect(); [GC]::WaitForPendingFinalizers()
  $result.ended_at=(Get-Date).ToUniversalTime().ToString('o')
  $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Directory 'result.json')
}
if ($result.status -ne 'converted') { exit 1 }
