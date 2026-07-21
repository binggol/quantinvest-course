function Get-RdagentMiningLogState {
  param([Parameter(Mandatory = $true)][string]$Path)

  $empty = [pscustomobject]@{
    PipeFailure = $false
    RecoveredAfterFailure = $false
    LastWriteTimeUtc = [datetime]::MinValue
  }
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $empty }

  try {
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    $text = (Get-Content -LiteralPath $Path -Encoding UTF8 -Tail 2000 -ErrorAction Stop) -join "`n"
  } catch {
    return $empty
  }

  # Require the complete Windows named-pipe worker signature.  A lone
  # AssertionError is far too broad to terminate a paid research run.
  $signature = [regex]::Match(
    $text,
    'Process SpawnPoolWorker-\d+:[\s\S]{0,12000}multiprocessing\\connection\.py[\s\S]{0,4000}_get_more_data[\s\S]{0,4000}assert left > 0[\s\S]{0,1000}AssertionError',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
  )
  $recovered = $false
  if ($signature.Success) {
    $healthyLines = [regex]::Matches(
      $text,
      '(?m)^20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)? \| (?:INFO|WARNING|ERROR)\s+\| rdagent\.'
    )
    if ($healthyLines.Count -gt 0) {
      $recovered = $healthyLines[$healthyLines.Count - 1].Index -gt $signature.Index
    }
  }

  return [pscustomobject]@{
    PipeFailure = $signature.Success
    RecoveredAfterFailure = $recovered
    LastWriteTimeUtc = $item.LastWriteTimeUtc
  }
}

function Get-RdagentMiningLatestWriteUtc {
  param([Parameter(Mandatory = $true)][string[]]$Paths)

  $latest = [datetime]::MinValue
  foreach ($path in $Paths) {
    try {
      $value = (Get-Item -LiteralPath $path -ErrorAction Stop).LastWriteTimeUtc
      if ($value -gt $latest) { $latest = $value }
    } catch {}
  }
  return $latest
}

function Test-RdagentMiningAttemptIdentity {
  param(
    [Parameter(Mandatory = $true)][int]$ProcessId,
    [Parameter(Mandatory = $true)][int]$WatcherProcessId,
    [Parameter(Mandatory = $true)][int]$LoopN,
    [Parameter(Mandatory = $true)][datetime]$StartedAtUtc,
    [Parameter(Mandatory = $true)][string]$LeasePath,
    [Parameter(Mandatory = $true)][string]$StatusPath,
    [AllowEmptyString()][string]$RequestId,
    [Parameter(Mandatory = $true)][string]$AttemptId
  )

  if (-not $AttemptId -or -not (Test-Path -LiteralPath $LeasePath -PathType Leaf)) { return $false }
  try {
    $status = Get-Content -LiteralPath $StatusPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    if ([string]$status.state -ne 'running') { return $false }
    if ([string]$status.attempt_id -cne $AttemptId) { return $false }
    if ($RequestId -and [string]$status.request_id -cne $RequestId) { return $false }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
    if (-not $process -or [string]$process.Name -ine 'rdagent.exe') { return $false }
    if ([int]$process.ParentProcessId -ne $WatcherProcessId) { return $false }
    $command = [string]$process.CommandLine
    if ($command -notmatch '(?i)(?:^|\s)fin_factor(?:\s|$)') { return $false }
    $loopPattern = '(?i)--loop-n(?:=|\s+)' + [regex]::Escape([string]$LoopN) + '(?:\s|$)'
    if ($command -notmatch $loopPattern) { return $false }

    $createdUtc = ([datetime]$process.CreationDate).ToUniversalTime()
    if ([math]::Abs(($createdUtc - $StartedAtUtc).TotalSeconds) -gt 30) { return $false }
    return $true
  } catch {
    return $false
  }
}

function Stop-RdagentMiningAttempt {
  param([Parameter(Mandatory = $true)][int]$ProcessId)

  # The caller validates the exact PID, parent, command, creation time, request,
  # attempt and lease immediately before this exact process-tree termination.
  $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
  & $taskkill /PID $ProcessId /T /F | Out-Null
  return $LASTEXITCODE -eq 0
}
