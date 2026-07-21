param(
  [string]$ProjectDir = "C:\path\to\quantinvest-course",  # TODO: 改为你的项目路径
  [string]$Python = "D:\anaconda3\python.exe",
  [switch]$DisableLegacyPipeline
)

$ErrorActionPreference = "Stop"

function New-ReliableSettings([timespan]$Limit) {
  return New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit $Limit `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
}

function Set-ExistingTaskTiming(
  [string]$TaskName,
  [Microsoft.Management.Infrastructure.CimInstance]$Trigger,
  [Microsoft.Management.Infrastructure.CimInstance]$Settings
) {
  $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $existing) {
    Write-Warning "Scheduled task does not exist; timing update skipped: $TaskName"
    return
  }
  try {
    Set-ScheduledTask -TaskName $TaskName -Trigger $Trigger -Settings $Settings | Out-Null
    Write-Host "updated $TaskName"
  }
  catch [System.UnauthorizedAccessException] {
    Write-Warning "Scheduled task is administrator-owned; timing update skipped: $TaskName"
  }
  catch {
    if ($_.Exception.Message -match "Access is denied|0x80070005") {
      Write-Warning "Scheduled task is administrator-owned; timing update skipped: $TaskName"
      return
    }
    throw
  }
}

if (-not (Test-Path -LiteralPath $ProjectDir -PathType Container)) {
  throw "Project directory unavailable: $ProjectDir"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw "Python unavailable: $Python"
}

# Heavy six-model refresh runs after the prior evening's 21:00 market-data job.
$pipelineTaskName = "quantinvest" + (-join @(
  [char]0x6BCF, [char]0x65E5, [char]0x81EA, [char]0x52A8,
  [char]0x7BA1, [char]0x7EBF
))
Set-ExistingTaskTiming `
  -TaskName $pipelineTaskName `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 1:30AM) `
  -Settings (New-ReliableSettings (New-TimeSpan -Hours 4))

# The pre-open console build includes a bounded 65-minute Qlib repair path and
# weekly research, hence a three-hour ceiling rather than the old 72-hour limit.
Set-ExistingTaskTiming `
  -TaskName "quantinvest_daily" `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 6:00AM) `
  -Settings (New-ReliableSettings (New-TimeSpan -Hours 3))

# Keep the request watcher alive because transfer/placement/announcement jobs use
# its retry-aware background loop.  The task action and logon trigger are kept.
$watcherTaskName = "quantinvest" + (-join @(
  [char]0x9884, [char]0x6D4B, [char]0x76D1, [char]0x542C
))
$watcherTask = Get-ScheduledTask -TaskName $watcherTaskName -ErrorAction SilentlyContinue
if ($watcherTask) {
  $watcherSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit ([timespan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
  try {
    Set-ScheduledTask -TaskName $watcherTaskName -Settings $watcherSettings | Out-Null
    Write-Host "updated $watcherTaskName reliability settings"
  }
  catch {
    if ($_.Exception.Message -match "Access is denied|0x80070005") {
      Write-Warning "Scheduled task is administrator-owned; reliability update skipped: $watcherTaskName"
    }
    else {
      throw
    }
  }
}

# Preserve the proven intraday trigger/action while allowing sleep recovery.
$weekdayNames = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$intradayTask = Get-ScheduledTask -TaskName "quantinvest_intraday_t" -ErrorAction SilentlyContinue
if ($intradayTask) {
  try {
    Set-ScheduledTask `
      -TaskName "quantinvest_intraday_t" `
      -Trigger (New-ScheduledTaskTrigger `
        -Weekly `
        -WeeksInterval 1 `
        -DaysOfWeek $weekdayNames `
        -At "07:58") `
      -Settings (New-ReliableSettings (New-TimeSpan -Hours 7)) | Out-Null
    Write-Host "updated quantinvest_intraday_t at 07:58 with reliability settings"
  }
  catch {
    if ($_.Exception.Message -match "Access is denied|0x80070005") {
      Write-Warning "Scheduled task is administrator-owned; timing update skipped: quantinvest_intraday_t"
    }
    else {
      throw
    }
  }
}

$consoleRunner = Join-Path $ProjectDir "scripts\refresh_daily_console.py"
$pageRunner = Join-Path $ProjectDir "scripts\refresh_page_sources.py"
foreach ($requiredRunner in @($consoleRunner, $pageRunner)) {
  if (-not (Test-Path -LiteralPath $requiredRunner -PathType Leaf)) {
    throw "Refresh runner unavailable: $requiredRunner"
  }
}
$principal = New-ScheduledTaskPrincipal `
  -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
  -LogonType Interactive `
  -RunLevel Limited

function Test-DedicatedJobConfigured(
  [Microsoft.Management.Infrastructure.CimInstance]$Task,
  [hashtable]$Job,
  [string]$ExpectedArguments
) {
  if (-not $Task -or @($Task.Actions).Count -ne 1) {
    return $false
  }
  $expectedUser = ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name -split '\\')[-1]
  $actualUser = ([string]$Task.Principal.UserId -split '\\')[-1]
  if (
    -not $Task.Settings.Enabled -or
    $actualUser -ine $expectedUser -or
    [string]$Task.Principal.LogonType -notmatch '^Interactive'
  ) {
    return $false
  }
  $action = @($Task.Actions)[0]
  if (
    [string]$action.Execute -ine $Python -or
    [string]$action.Arguments -cne $ExpectedArguments -or
    [string]$action.WorkingDirectory -ine $ProjectDir
  ) {
    return $false
  }

  $expectedDayMask = 0
  $dayMasks = @{
    Sunday = 1; Monday = 2; Tuesday = 4; Wednesday = 8
    Thursday = 16; Friday = 32; Saturday = 64
  }
  foreach ($day in $Job.Days) {
    $expectedDayMask += [int]$dayMasks[[string]$day]
  }
  $triggers = @($Task.Triggers)
  if ($triggers.Count -ne @($Job.Times).Count) {
    return $false
  }
  $actualTimes = @()
  foreach ($trigger in $triggers) {
    if (
      -not $trigger.Enabled -or
      [int]$trigger.WeeksInterval -ne 1 -or
      [int]$trigger.DaysOfWeek -ne $expectedDayMask
    ) {
      return $false
    }
    if (
      [string]$trigger.EndBoundary -and
      [datetimeoffset]::Parse([string]$trigger.EndBoundary) -le [datetimeoffset]::Now
    ) {
      return $false
    }
    $actualTimes += ([datetimeoffset]::Parse([string]$trigger.StartBoundary)).ToString("HH:mm")
  }
  if (Compare-Object @($Job.Times | Sort-Object) @($actualTimes | Sort-Object)) {
    return $false
  }

  try {
    $limit = [System.Xml.XmlConvert]::ToTimeSpan(
      [string]$Task.Settings.ExecutionTimeLimit
    )
    $restartInterval = [System.Xml.XmlConvert]::ToTimeSpan(
      [string]$Task.Settings.RestartInterval
    )
  }
  catch {
    return $false
  }
  return (
    $Task.Settings.StartWhenAvailable -and
    $Task.Settings.WakeToRun -and
    [string]$Task.Settings.MultipleInstances -eq "IgnoreNew" -and
    $limit -eq (New-TimeSpan -Minutes ([int]$Job.LimitMinutes)) -and
    [int]$Task.Settings.RestartCount -eq 2 -and
    $restartInterval -eq (New-TimeSpan -Minutes 15)
  )
}

$scheduledJobs = @(
  @{
    Name = "quantinvest-console-cross-market"
    Times = @("09:20")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "cross-market"
    LimitMinutes = 45
    Description = "Weekday 09:20 daily-console cross-market storage snapshot"
  },
  @{
    Name = "quantinvest-console-snowball"
    Times = @("09:35")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "snowball"
    LimitMinutes = 45
    Description = "Weekday 09:35 daily-console snowball risk snapshot"
  },
  @{
    Name = "quantinvest-console-korea"
    Times = @("14:35")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "korea"
    LimitMinutes = 45
    Description = "Weekday 14:35 daily-console SK Hynix close signal"
  },
  @{
    Name = "quantinvest-console-advisor"
    Times = @("09:05")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "advisor"
    LimitMinutes = 60
    Description = "Weekday 09:05 base regime-advisor snapshot after the bounded 06:00 Qlib refresh"
  },
  @{
    Name = "quantinvest-console-transfer-documents"
    Times = @("06:35", "18:40")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "transfer-documents"
    LimitMinutes = 90
    Description = "Weekday pre-open and after-close transfer announcement refresh"
  },
  @{
    Name = "quantinvest-console-placement-documents"
    Times = @("06:45", "18:50")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "placement-documents"
    LimitMinutes = 90
    Description = "Weekday pre-open and after-close placement lifecycle refresh"
  },
  @{
    Name = "quantinvest-console-earnings-announcements"
    Times = @("06:55", "19:00")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "earnings-announcements"
    LimitMinutes = 120
    Description = "Weekday pre-open and after-close earnings-announcement refresh"
  },
  @{
    Name = "quantinvest-page-company-events"
    Times = @("07:10", "18:10")
    Days = $weekdayNames
    Runner = $pageRunner
    Argument = "company-events --lock-wait-seconds 900 --state `"$ProjectDir\data\page_refresh_state_company_events.json`""
    LimitMinutes = 180
    Description = "Weekday pre-open and 18:10 company-event risk refresh"
  },
  @{
    Name = "quantinvest-console-growth-queue"
    Times = @("19:20")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "growth-queue"
    LimitMinutes = 60
    Description = "Weekday 19:20 after-close growth-report queue refresh"
  },
  @{
    Name = "quantinvest-console-rolling"
    Times = @("19:40")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "rolling"
    LimitMinutes = 60
    Description = "Weekday 19:40 rolling-earnings production snapshot"
  },
  @{
    Name = "quantinvest-page-closing-risk"
    Times = @("20:40")
    Days = $weekdayNames
    Runner = $pageRunner
    Argument = "closing-risk --lock-wait-seconds 900 --state `"$ProjectDir\data\page_refresh_state_closing_risk.json`""
    LimitMinutes = 120
    Description = "Weekday 20:40 leverage/LHB/big-bath closing-risk refresh"
  },
  @{
    Name = "quantinvest-console-top-risk"
    Times = @("22:15")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "top-risk"
    LimitMinutes = 180
    Description = "Weekday 22:15 broad and sector ETF top-risk refresh"
  },
  @{
    Name = "quantinvest-console-money-outflow"
    Times = @("23:00")
    Days = $weekdayNames
    Runner = $consoleRunner
    Argument = "money-outflow"
    LimitMinutes = 180
    Description = "Weekday 23:00 money-outflow signal refresh after market data"
  },
  @{
    Name = "quantinvest-page-weekly-sources"
    Times = @("10:30")
    Days = @("Monday")
    Runner = $pageRunner
    Argument = "weekly-sources --lock-wait-seconds 900 --state `"$ProjectDir\data\page_refresh_state_weekly_sources.json`""
    LimitMinutes = 120
    Description = "Monday 10:30 late-disclosure and foreign-index weekly refresh"
  },
  @{
    Name = "quantinvest-console-rolling-backtest"
    Times = @("03:30")
    Days = @("Sunday")
    Runner = $consoleRunner
    Argument = "rolling-backtest"
    LimitMinutes = 240
    Description = "Sunday 03:30 rolling-earnings production backtest refresh"
  },
  @{
    Name = "quantinvest-console-earnings-entry-lag"
    Times = @("05:00")
    Days = @("Sunday")
    Runner = $consoleRunner
    Argument = "earnings-entry-lag"
    LimitMinutes = 360
    Description = "Sunday 05:00 earnings entry-lag research refresh"
  }
)

$dedicatedMarker = Join-Path $ProjectDir "data\dedicated_refresh_tasks.enabled"
$dedicatedWriterTaskNames = @(
  "quantinvest-console-transfer-documents",
  "quantinvest-console-placement-documents",
  "quantinvest-console-earnings-announcements"
)

function Restore-LegacyWriterOwnership([string]$Reason) {
  $cleanupFailures = @()
  foreach ($name in $dedicatedWriterTaskNames) {
    $writerTask = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $writerTask) {
      continue
    }
    try {
      Disable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null
      Write-Warning "disabled dedicated writer $name while restoring legacy ownership: $Reason"
    }
    catch {
      $disableFailure = $_.Exception.Message
      try {
        # Registration and rollback run as the same principal.  Unregistering is
        # a fail-safe for a task that cannot be left disabled after a partial run.
        Unregister-ScheduledTask `
          -TaskName $name `
          -Confirm:$false `
          -ErrorAction Stop
        Write-Warning "unregistered dedicated writer $name while restoring legacy ownership: $Reason"
      }
      catch {
        $cleanupFailures += "${name}: disable failed ($disableFailure); unregister failed ($($_.Exception.Message))"
      }
    }
  }

  if ($cleanupFailures.Count -gt 0) {
    # Retaining an existing marker is safer than re-enabling the watcher legacy
    # path while a dedicated writer could still be active.
    throw "dedicated writer rollback failed; marker retained: $($cleanupFailures -join '; ')"
  }
  if (Test-Path -LiteralPath $dedicatedMarker) {
    Remove-Item -LiteralPath $dedicatedMarker -Force -ErrorAction Stop
  }
}

try {
  foreach ($job in $scheduledJobs) {
    $actionArguments = '"' + [string]$job.Runner + '" ' + [string]$job.Argument
    $existing = Get-ScheduledTask -TaskName $job.Name -ErrorAction SilentlyContinue
    if (Test-DedicatedJobConfigured $existing $job $actionArguments) {
      Write-Host "preserved compliant $($job.Name) at $($job.Times -join ', ')"
      continue
    }
    $action = New-ScheduledTaskAction `
      -Execute $Python `
      -Argument $actionArguments `
      -WorkingDirectory $ProjectDir
    $triggers = @(
      foreach ($time in $job.Times) {
        New-ScheduledTaskTrigger `
          -Weekly `
          -WeeksInterval 1 `
          -DaysOfWeek $job.Days `
          -At $time
      }
    )
    Register-ScheduledTask `
      -TaskName $job.Name `
      -Action $action `
      -Trigger $triggers `
      -Settings (New-ReliableSettings (New-TimeSpan -Minutes ([int]$job.LimitMinutes))) `
      -Principal $principal `
      -Description $job.Description `
      -Force | Out-Null
    Write-Host "registered $($job.Name) at $($job.Times -join ', ')"
  }

  # The long-running watcher previously owned transfer/placement/announcement
  # timers.  Publish ownership only after every dedicated task is configured.
  # Keeping this write in the try block makes marker publication part of the
  # same rollback boundary as task registration.
  Set-Content `
    -LiteralPath $dedicatedMarker `
    -Value ("installed=" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) `
    -Encoding ASCII
  Write-Host "enabled dedicated refresh-task ownership: $dedicatedMarker"
}
catch {
  $installFailure = $_
  try {
    Restore-LegacyWriterOwnership -Reason "dedicated task registration or marker publication failed"
  }
  catch {
    throw "dedicated task installation failed ($($installFailure.Exception.Message)); rollback failed ($($_.Exception.Message))"
  }
  throw $installFailure
}

# A running PowerShell watcher has already parsed its script, so the marker is
# effective only after a verified restart.  Try directly first.  If the old
# watcher is elevated, route one PID-bound request through the existing highest-
# privilege daily task; that maintenance path can only call the fixed restart
# script and exits before model training.
$restartScript = Join-Path $ProjectDir "scripts\restart_watch_predict_pc.ps1"
$handoffOk = $false
if (Test-Path -LiteralPath $restartScript -PathType Leaf) {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $restartScript -Hidden
  $restartExit = $LASTEXITCODE
  if ($restartExit -eq 0) {
    $handoffOk = $true
  }
  elseif ($restartExit -eq 78) {
    $pidFile = Join-Path $ProjectDir "data\watch_predict_pc.pid"
    $maintenanceRequest = Join-Path $ProjectDir "data\watcher_restart_admin.request.json"
    try {
      $expectedPid = [int]((Get-Content -LiteralPath $pidFile -Raw).Trim())
      $requestTemp = "$maintenanceRequest.$PID.tmp"
      @{
        operation = "restart_watch_predict_pc"
        expected_pid = $expectedPid
        reason = "dedicated refresh-task ownership handoff"
      } | ConvertTo-Json -Compress | Set-Content -LiteralPath $requestTemp -Encoding UTF8
      Move-Item -LiteralPath $requestTemp -Destination $maintenanceRequest -Force
      Start-ScheduledTask -TaskName $pipelineTaskName
      $deadline = (Get-Date).AddSeconds(45)
      while ((Get-Date) -lt $deadline) {
        if (-not (Test-Path -LiteralPath $maintenanceRequest)) {
          $newPid = [int]((Get-Content -LiteralPath $pidFile -Raw).Trim())
          if ($newPid -ne $expectedPid -and (Get-Process -Id $newPid -ErrorAction SilentlyContinue)) {
            $handoffOk = $true
            break
          }
        }
        Start-Sleep -Milliseconds 500
      }
    }
    catch {
      Write-Warning "administrator watcher handoff failed: $($_.Exception.Message)"
    }
    finally {
      Remove-Item -LiteralPath $maintenanceRequest -Force -ErrorAction SilentlyContinue
      Remove-Item -LiteralPath "$maintenanceRequest.$PID.tmp" -Force -ErrorAction SilentlyContinue
    }
  }
}
if (-not $handoffOk) {
  try {
    Restore-LegacyWriterOwnership -Reason "watcher ownership handoff failed"
  }
  catch {
    throw "watcher ownership handoff failed; dedicated-writer rollback also failed: $($_.Exception.Message)"
  }
  throw "watcher ownership handoff failed; legacy writer ownership was restored"
}
Write-Host "verified watcher restart with dedicated refresh-task ownership"

if ($DisableLegacyPipeline) {
  $legacy = Get-ScheduledTask -TaskName "RDAgent-Daily-Pipeline" -ErrorAction SilentlyContinue
  if ($legacy) {
    if (-not $legacy.Settings.Enabled) {
      Write-Host "obsolete RDAgent-Daily-Pipeline is already disabled"
    }
    else {
      try {
        Disable-ScheduledTask -TaskName "RDAgent-Daily-Pipeline" | Out-Null
        Write-Host "disabled obsolete RDAgent-Daily-Pipeline"
      }
      catch {
        if ($_.Exception.Message -match "Access is denied|0x80070005") {
          Write-Warning "administrator-owned RDAgent-Daily-Pipeline could not be disabled"
        }
        else {
          throw
        }
      }
    }
  }
}
