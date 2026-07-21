$ErrorActionPreference = "Stop"

$scriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$helperPath = Join-Path $scriptPath "watch_predict_pc_docker.ps1"
. $helperPath

function Assert-True($condition, $message) {
  if (-not $condition) { throw $message }
}

$source = [System.IO.File]::ReadAllText($helperPath)
Assert-True ($source.Contains('$TimeoutMilliseconds = 15000')) "Docker readiness probe must have a bounded timeout"
Assert-True ($source.Contains('$probe.WaitForExit($TimeoutMilliseconds)')) "Docker readiness probe can still hang indefinitely"
Assert-True ($source.Contains('Stop-Process -Id $probe.Id -Force')) "timed-out Docker probe is not terminated"
Assert-True ($source.Contains('$probe.WaitForExit(2000)')) "timed-out Docker probe has an unbounded termination wait"
Assert-True (-not $source.Contains('$probe.WaitForExit()')) "Docker probe contains an unbounded WaitForExit call"

$calls = [ordered]@{
  DockerChecks = 0
  Starts = 0
  Sleeps = 0
  Statuses = @()
}

$ready = Ensure-DockerReady `
  -MaxAttempts 2 `
  -DelaySeconds 0 `
  -DockerCheck {
    $calls.DockerChecks += 1
    return ($calls.DockerChecks -ge 2)
  } `
  -StartDocker {
    $calls.Starts += 1
  } `
  -Sleep {
    param($Seconds)
    $calls.Sleeps += 1
  } `
  -WriteStatus {
    param($State, $Message)
    $calls.Statuses += "$State|$Message"
  }

Assert-True $ready "expected Docker to become ready after retry"
Assert-True ($calls.DockerChecks -eq 2) "expected exactly two Docker checks"
Assert-True ($calls.Starts -eq 1) "expected Docker Desktop to be started once"
Assert-True ($calls.Sleeps -eq 1) "expected one wait between attempts"
Assert-True (($calls.Statuses -join "`n") -match "Docker not ready") "expected startup status to be reported"

Write-Host "test_watch_predict_pc_docker.ps1 passed"
