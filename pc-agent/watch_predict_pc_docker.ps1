function Test-DockerReady {
  param([int]$TimeoutMilliseconds = 15000)

  $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
  if (-not $docker) { return $false }
  $probe = $null
  try {
    $probe = Start-Process -FilePath $docker.Source `
      -ArgumentList @("ps", "--format", "{{.ID}}") `
      -WindowStyle Hidden -PassThru -ErrorAction Stop
    if (-not $probe.WaitForExit($TimeoutMilliseconds)) {
      Stop-Process -Id $probe.Id -Force -ErrorAction SilentlyContinue
      try { [void]$probe.WaitForExit(2000) } catch {}
      return $false
    }
    $probe.Refresh()
    return ($probe.ExitCode -eq 0)
  } catch {
    return $false
  } finally {
    if ($probe) { $probe.Dispose() }
  }
}

function Start-DockerDesktopIfAvailable {
  $desktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
  if (Test-Path $desktop) {
    Start-Process -FilePath $desktop -WindowStyle Hidden -ErrorAction SilentlyContinue
    return $true
  }
  return $false
}

function Ensure-DockerReady {
  param(
    [int]$MaxAttempts = 30,
    [int]$DelaySeconds = 5,
    [scriptblock]$DockerCheck = { Test-DockerReady },
    [scriptblock]$StartDocker = { Start-DockerDesktopIfAvailable },
    [scriptblock]$Sleep = { param($Seconds) Start-Sleep -Seconds $Seconds },
    [scriptblock]$WriteStatus = { param($State, $Message) }
  )

  $started = $false
  for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    if (& $DockerCheck) {
      return $true
    }

    if (-not $started) {
      & $WriteStatus "running" "Docker not ready; starting Docker Desktop and waiting..."
      & $StartDocker | Out-Null
      $started = $true
    }

    if ($attempt -lt $MaxAttempts) {
      & $Sleep $DelaySeconds
    }
  }

  return $false
}
