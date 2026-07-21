param(
  [switch]$Hidden
)

$ErrorActionPreference = "Continue"
$proj = Split-Path -Parent $PSScriptRoot
$watcher = Join-Path $PSScriptRoot "watch_predict_pc.ps1"
$pidFile = Join-Path $proj "data\watch_predict_pc.pid"
$instanceLock = Join-Path $proj "data\watch_predict_pc.instance.lock"
$watcherCommandPattern = '(?i)(?:^|\s)-File\s+"?[^"\r\n]*[\\/]watch_predict_pc\.ps1"?(?:\s|$)'

function Test-WatcherPidOwnership($proc) {
  if (-not $proc -or [string]$proc.Name -notmatch '^(?i:powershell|pwsh)\.exe$') {
    return $false
  }
  if ([string]$proc.CommandLine -match $watcherCommandPattern) {
    return $true
  }
  if (-not (Test-Path -LiteralPath $pidFile) -or -not (Test-Path -LiteralPath $instanceLock)) {
    return $false
  }

  # Elevated PowerShell processes can hide CommandLine from a non-elevated
  # restart shell.  In that case, the watcher-written PID timestamp plus its
  # exclusively-held lifetime lock provide a narrow, verifiable fallback.
  try {
    $pidWritten = (Get-Item -LiteralPath $pidFile).LastWriteTime
    $processStarted = [datetime]$proc.CreationDate
    if ([math]::Abs(($pidWritten - $processStarted).TotalSeconds) -gt 120) {
      return $false
    }
    $probe = [System.IO.File]::Open(
      $instanceLock,
      [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::ReadWrite,
      [System.IO.FileShare]::None
    )
    $probe.Dispose()
    return $false
  }
  catch [System.IO.IOException] {
    return $true
  }
  catch {
    return $false
  }
}

if (-not (Test-Path $watcher)) {
  Write-Host "[restart] watcher not found: $watcher" -ForegroundColor Red
  exit 1
}

# Never detach a live fin_factor tree from its watcher. Recovery/restart is safe only
# after mining exits (or through the explicit stuck-attempt recovery procedure).
try {
  $activeMiner = @(
    Get-CimInstance Win32_Process -Filter "Name='rdagent.exe' OR Name='python.exe' OR Name='pythonw.exe'" |
      Where-Object { $_.CommandLine -match '(?i)(?:^|\s)fin_factor(?:\s|$)' }
  )
} catch {
  Write-Host "[restart] cannot verify mining state; restart aborted: $_" -ForegroundColor Yellow
  exit 76
}
if ($activeMiner.Count -gt 0) {
  Write-Host "[restart] fin_factor is still running; watcher restart aborted" -ForegroundColor Yellow
  exit 75
}

if (Test-Path $pidFile) {
  try {
    $oldPid = [int]((Get-Content $pidFile -Raw).Trim())
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid" -ErrorAction SilentlyContinue
    if (Test-WatcherPidOwnership $proc) {
      Write-Host "[restart] stopping old watcher PID=$oldPid" -ForegroundColor Yellow
      Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 1
      if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
        Write-Host "[restart] old watcher PID=$oldPid could not be stopped; run this script as administrator" -ForegroundColor Red
        exit 78
      }
    }
  } catch {
    Write-Host "[restart] old PID file ignored: $_" -ForegroundColor Yellow
  }
}

try {
  Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -match $watcherCommandPattern
  } | ForEach-Object {
    Write-Host "[restart] stopping watcher by command line PID=$($_.ProcessId)" -ForegroundColor Yellow
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
} catch {
  Write-Host "[restart] command-line process scan skipped: $_" -ForegroundColor DarkYellow
}

try {
  $remainingWatchers = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction Stop | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -match $watcherCommandPattern
  })
} catch {
  Write-Host "[restart] cannot verify old watcher exit; new instance was not started" -ForegroundColor Red
  exit 76
}
if ($remainingWatchers.Count -gt 0) {
  Write-Host "[restart] old watcher is still alive; new instance was not started" -ForegroundColor Red
  exit 77
}

$args = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", "`"$watcher`""
)

$style = if ($Hidden) { "Hidden" } else { "Normal" }
$restartStarted = Get-Date
$newWatcher = Start-Process -FilePath "powershell.exe" -ArgumentList $args -WorkingDirectory $proj -WindowStyle $style -PassThru
$startupDeadline = (Get-Date).AddSeconds(12)
while ((Get-Date) -lt $startupDeadline) {
  if ($newWatcher.HasExited) {
    Write-Host "[restart] new watcher exited during startup (exit=$($newWatcher.ExitCode))" -ForegroundColor Red
    exit 79
  }
  $pidMatches = $false
  try {
    $pidMatches = (
      [int]((Get-Content -LiteralPath $pidFile -Raw).Trim()) -eq $newWatcher.Id -and
      (Get-Item -LiteralPath $pidFile).LastWriteTime -ge $restartStarted.AddSeconds(-1)
    )
  } catch {}
  if ($pidMatches) {
    $lockHeld = $false
    try {
      $probe = [System.IO.File]::Open(
        $instanceLock,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
      )
      $probe.Dispose()
    } catch [System.IO.IOException] {
      $lockHeld = $true
    }
    if ($lockHeld) {
      Write-Host "[restart] started watcher PID=$($newWatcher.Id) from $watcher" -ForegroundColor Green
      exit 0
    }
  }
  Start-Sleep -Milliseconds 250
}
Write-Host "[restart] new watcher did not publish its PID and instance lock" -ForegroundColor Red
exit 79
