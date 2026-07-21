# PC resident watcher (Plan B + web button).
# Watches the shared folder for predict_request.json (written by the NAS web button),
# runs the prediction, writes predict_status.json, deletes the request.
# Uses a UNC path so it does NOT depend on the Z: drive mapping (works in any shell,
# including elevated PowerShell where mapped drives are not visible).
#
# Start (keep the window open):
#   powershell -ExecutionPolicy Bypass -File scripts\watch_predict_pc.ps1
# Ctrl+C to stop.

$ErrorActionPreference = "Continue"
$proj = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "watch_predict_pc_docker.ps1")
. (Join-Path $PSScriptRoot "rdagent_mine_supervisor.ps1")
$watchPidFile = Join-Path $proj "data\watch_predict_pc.pid"
try { $Host.UI.RawUI.WindowTitle = "quantinvest watch_predict_pc" } catch {}

# ===== 单例守卫: 生命周期独占锁消除并发启动竞态；不得杀父进程后留下挖矿孤儿 =====
$watcherCommandPattern = '(?i)(?:^|\s)-File\s+"?[^"\r\n]*[\\/]watch_predict_pc\.ps1"?(?:\s|$)'
$watchInstanceLockPath = Join-Path $proj "data\watch_predict_pc.instance.lock"
try {
  New-Item -ItemType Directory -Force -Path (Split-Path $watchInstanceLockPath -Parent) | Out-Null
  $script:watchInstanceLock = [System.IO.File]::Open(
    $watchInstanceLockPath,
    [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite,
    [System.IO.FileShare]::None
  )
} catch {
  Write-Host "[singleton] watcher 已持有实例锁，本实例退出" -ForegroundColor Yellow
  exit 0
}
# 兼容正在运行、尚未使用实例锁的旧版本 watcher。并发的新版本输家会先因锁退出。
Start-Sleep -Milliseconds 750
try {
  $existingWatchers = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -match $watcherCommandPattern
  })
} catch {
  Write-Host "[singleton] 无法确认 watcher 单例，本实例安全退出: $($_.Exception.Message)" -ForegroundColor Yellow
  exit 76
}
if ($existingWatchers.Count -gt 0) {
  $existingIds = ($existingWatchers | ForEach-Object { $_.ProcessId }) -join ","
  Write-Host "[singleton] watcher 已运行 PID=$existingIds，本实例退出" -ForegroundColor Yellow
  exit 0
}
try {
  New-Item -ItemType Directory -Force -Path (Split-Path $watchPidFile -Parent) | Out-Null
  $PID | Out-File -FilePath $watchPidFile -Encoding ascii -Force
} catch {}

# Shared dir = NAS qlib data 'csv_tmp'. Prefer explicit SHARED_DIR, then UNC, then mapped Z: fallbacks.
$sharedCandidates = @()
if ($env:SHARED_DIR) { $sharedCandidates += $env:SHARED_DIR }
$sharedCandidates += "\/app/qlib_data\csv_tmp"
$sharedCandidates += "Z:\obsidian\vaults\claude\qlib\data\csv_tmp"
$sharedCandidates += "Z:\claude\qlib\data\csv_tmp"
$sharedCandidates += "Z:\qlib\data\csv_tmp"
$shared = $null
foreach ($cand in $sharedCandidates) {
  try {
    if ($cand -and (Test-Path $cand)) {
      $shared = $cand
      break
    }
  } catch {}
}
if (-not $shared) {
  $shared = $sharedCandidates[0]
}
$qlibData = (Split-Path $shared -Parent) + "\cn_data"
$rdagentWorkspaceNasCandidates = @()
if ($env:RDAGENT_WORKSPACE_NAS_DIR) { $rdagentWorkspaceNasCandidates += $env:RDAGENT_WORKSPACE_NAS_DIR }
$rdagentWorkspaceNasCandidates += "\/app/shared\claude\rdagent_workspace"
$rdagentWorkspaceNasCandidates += "Z:\claude\rdagent_workspace"
$rdagentWorkspaceNasRoot = $null
foreach ($cand in $rdagentWorkspaceNasCandidates) {
  try {
    if ($cand -and (Test-Path $cand)) {
      $rdagentWorkspaceNasRoot = $cand
      break
    }
  } catch {}
}
if (-not $rdagentWorkspaceNasRoot) {
  $rdagentWorkspaceNasRoot = $rdagentWorkspaceNasCandidates[0]
}
$nasQuantinvestCandidates = @()
if ($env:QUANTINVEST_NAS_DIR) { $nasQuantinvestCandidates += $env:QUANTINVEST_NAS_DIR }
$nasQuantinvestCandidates += "\/app"
$nasQuantinvestCandidates += "Z:\quantinvest"
$nasQuantinvestRoot = $null
foreach ($cand in $nasQuantinvestCandidates) {
  try {
    if ($cand -and (Test-Path $cand)) {
      $nasQuantinvestRoot = $cand
      break
    }
  } catch {}
}

$reqFile    = Join-Path $shared "predict_request.json"
$statusFile = Join-Path $shared "predict_status.json"
$rdReqFile    = Join-Path $shared "rdagent_request.json"
$rdStatusFile = Join-Path $shared "rdagent_status.json"
$script:rdStatusRequestId = ""
$script:rdStatusRequestedAt = ""
$script:rdStatusAttemptId = ""
$taReqFile    = Join-Path $shared "ta_request.json"     # TradingAgents 分析请求
$factorReqFile = Join-Path $shared "factor_request.json" # 因子值抽取请求 (Phase 4 K线叠加)
$inclReqFile   = Join-Path $shared "inclusion_request.json"  # 指数纳入重算请求 (网页按钮)
$inclStatusFile = Join-Path $shared "inclusion_status.json"
$refreshReqFile = Join-Path $shared "refresh_request.json"   # 通用页面刷新请求 (rsrs/ipo/repo/runup)
$refreshStatusFile = Join-Path $shared "refresh_status.json"
$thesisReqFile = Join-Path $shared "thesis_request.json"     # 瓶颈链分析请求 (网页写theme, 跑 export_thesis.py)
$thesisStatusFile = Join-Path $shared "thesis_status.json"
$predA158ReqFile = Join-Path $shared "predict_a158_request.json"      # Alpha158预测请求 (网页写model, 跑 predict_next_day.py RDAGENT_ALPHA158=1)
$predA158StatusFile = Join-Path $shared "predict_a158_status.json"    # Alpha158预测进度 (PC回写)
$poolReqFile = Join-Path $shared "pool_predict_request.json"          # 分池买入清单一键全跑请求 (网页写universe, 所有模型按IR降序跑 predict_next_day RDAGENT_UNIVERSE=)
$poolStatusFile = Join-Path $shared "pool_predict_status.json"        # 分池预测进度 (PC回写)
$arenaReqFile = Join-Path $shared "alpha158_arena_request.json"       # Alpha158擂台回测请求 (网页写model, 跑 run_model.py RDAGENT_ALPHA158=1)
$arenaStatusFile = Join-Path $shared "alpha158_arena_status.json"     # 擂台进度 (PC回写)
$uarenaReqFile = Join-Path $shared "universe_arena_request.json"      # 股票池回测请求 (网页写 universe+model)
$uarenaStatusFile = Join-Path $shared "universe_arena_status.json"    # 股票池回测进度
$barenaReqFile = Join-Path $shared "batch_arena_request.json"         # 批次擂台回测请求 (网页写 batch+universe+model, 非A158)
$barenaStatusFile = Join-Path $shared "batch_arena_status.json"       # 批次擂台进度
$fcompReqFile = Join-Path $shared "fund_compare_request.json"         # 🧬批次vs基线 次日清单对比请求 (网页写 batch+baseline+model)
$fcompStatusFile = Join-Path $shared "fund_compare_status.json"       # 对比进度 (PC回写)
$batchPredReqFile = Join-Path $shared "batch_predict_request.json"    # 用某OHLCV批次因子+指定池(真路B)全模型预测次日清单 (网页写 batch+universe)
$batchPredStatusFile = Join-Path $shared "batch_predict_status.json"  # 批次预测进度 (PC回写)
$transferAutoFile = Join-Path $shared "transfer_events_auto.json"     # 协转/定增解禁增量自动刷新节流记录
$placementAutoFile = Join-Path $proj "data\placement_events_auto.json" # 本机节流，NAS中断时也不会高频重试
$earningsTimesAutoFile = Join-Path $shared "earnings_times_auto.json" # 巨潮业绩公告时间全市场自动刷新节流记录
$earningsEventTimesAutoFile = Join-Path $shared "earnings_event_times_auto.json" # 滚动业绩回测事件级巨潮时间补漏节流记录
$earningsAnnouncementsLockFile = Join-Path $shared "cninfo_earnings_announcements.lock"
$transferDocumentsLockFile = Join-Path $shared "cninfo_transfer.lock"
$placementDocumentsLockFile = Join-Path $shared "placement_documents.lock"
$rollingEarningsStatusFile = Join-Path $shared "rolling_earnings_backtest_status.json"
$rollingEarningsLockFile = Join-Path $shared "rolling_earnings_backtest.lock"
$dedicatedRefreshTasksMarker = Join-Path $proj "data\dedicated_refresh_tasks.enabled"
$autoRefreshRetryBaseMinutes = 15
$autoRefreshRetryCapMinutes = 360

$env:QLIB_DATA_PATH      = $qlibData
$env:PARQUET_DIR         = Join-Path $shared "tushare_daily"
$env:PREDICT_DATA_DIR    = $shared
$env:SHARED_DIR          = $shared
$env:STOCK_META_DB       = Join-Path $proj "data\stock_meta.db"
$env:QLIB_KERNELS        = "8"
$env:PREDICT_TRAIN_START = "2020-01-01"

function Write-Status($state, $msg) {
  $obj = @{ state = $state; msg = $msg; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
  ($obj | ConvertTo-Json -Compress) | Out-File -FilePath $statusFile -Encoding utf8
}
function Write-RdStatus($state, $msg) {
  $obj = @{ state = $state; msg = $msg; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
  if ($script:rdStatusRequestId) { $obj.request_id = $script:rdStatusRequestId }
  if ($script:rdStatusRequestedAt) { $obj.requested_at = $script:rdStatusRequestedAt }
  if ($script:rdStatusAttemptId) { $obj.attempt_id = $script:rdStatusAttemptId }
  Write-JsonAtomic $rdStatusFile $obj
}

function Test-RdagentMiningProcess {
  try {
    $wrapper = Get-CimInstance Win32_Process -Filter "Name='rdagent.exe'" -ErrorAction Stop |
      Where-Object { $_.CommandLine -match '(?i)(?:^|\s)fin_factor(?:\s|$)' } |
      Select-Object -First 1
    if ($wrapper) { return $true }
    $pythonMain = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop |
      Where-Object { $_.CommandLine -match '(?i)(?:^|\s)fin_factor(?:\s|$)' } |
      Select-Object -First 1
    return $null -ne $pythonMain
  } catch {
    # 查询命令行失败时保守处理，避免权限瞬变导致重复烧 LLM。
    return $null -ne (Get-Process -Name "rdagent" -ErrorAction SilentlyContinue | Select-Object -First 1)
  }
}

# JSON request values are eventually interpolated into bash commands and output names.
# Keep their accepted language deliberately small; do not try to escape arbitrary input.
$allowedRdagentModels = @("lgb", "xgb", "catboost", "ols", "ridge", "lasso", "dlinear", "patchtst", "timesnet", "itransformer")
$allowedRdagentUniverses = @("csi300", "csi500", "csi1000")

function Test-SafeRequestLabel {
  param(
    [AllowNull()][string]$Value,
    [switch]$AllowEmpty,
    [int]$MaxLength = 160
  )
  if ($null -eq $Value -or $Value.Length -eq 0) { return [bool]$AllowEmpty }
  if ($Value.Length -gt $MaxLength) { return $false }
  # Unicode letters/numbers plus ASCII space and . _ : -. Quotes, shell metacharacters,
  # line breaks and both path separators are consequently rejected.
  return [regex]::IsMatch(
    $Value,
    '\A[\p{L}\p{N} ._:\-]+\z',
    [System.Text.RegularExpressions.RegexOptions]::CultureInvariant
  )
}

function Test-AllowedRdagentModel {
  param(
    [AllowNull()][string]$Value,
    [switch]$AllowAll,
    [switch]$AllowEmpty
  )
  if ($null -eq $Value -or $Value.Length -eq 0) { return [bool]$AllowEmpty }
  $normalized = $Value.ToLowerInvariant()
  if ($AllowAll -and $normalized -eq "all") { return $true }
  return ($normalized -in $allowedRdagentModels)
}

function Test-AllowedRdagentUniverse {
  param(
    [AllowNull()][string]$Value,
    [switch]$AllowAll,
    [switch]$AllowAllUniverses
  )
  if ($null -eq $Value) { return $false }
  $normalized = $Value.ToLowerInvariant()
  if ($AllowAll -and $normalized -eq "all") { return $true }
  if ($AllowAllUniverses -and $normalized -eq "allunivs") { return $true }
  return ($normalized -in $allowedRdagentUniverses)
}

function Get-RdagentWorkspaceId {
  param([AllowNull()][string]$Value)
  if (-not $Value -or $Value.Length -gt 160) { return $null }
  $match = [regex]::Match(
    $Value,
    '\A(?:D:/rdagent_workspace|Z:/claude/rdagent_workspace)/(?<id>[0-9a-f]{32})\z',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
      [System.Text.RegularExpressions.RegexOptions]::CultureInvariant
  )
  if (-not $match.Success) { return $null }
  return $match.Groups["id"].Value.ToLowerInvariant()
}

function Test-SafeWorkspacePath {
  param([AllowNull()][string]$Value)
  return $null -ne (Get-RdagentWorkspaceId $Value)
}

function Get-PersistentRdagentWorkspacePath {
  param([AllowNull()][string]$Value)
  $workspaceId = Get-RdagentWorkspaceId $Value
  if (-not $workspaceId) { throw "Invalid RD-Agent workspace path" }
  return "Z:/claude/rdagent_workspace/$workspaceId"
}

function Publish-RdagentWorkspace {
  param(
    [Parameter(Mandatory = $true)][string]$Value,
    [Parameter(Mandatory = $true)][string]$NasRoot
  )
  $workspaceId = Get-RdagentWorkspaceId $Value
  if (-not $workspaceId) { throw "Invalid RD-Agent workspace path" }
  $persistentPath = Get-PersistentRdagentWorkspacePath $Value
  $destination = Join-Path $NasRoot $workspaceId

  if ($Value.StartsWith("Z:/", [System.StringComparison]::OrdinalIgnoreCase)) {
    if (-not (Test-Path -LiteralPath $destination -PathType Container)) {
      throw "RD-Agent Z workspace is missing: $destination"
    }
    return $persistentPath
  }

  $source = [System.IO.Path]::GetFullPath($Value.Replace('/', '\'))
  $expectedRoot = [System.IO.Path]::GetFullPath("D:\rdagent_workspace").TrimEnd('\') + '\'
  if (-not $source.StartsWith($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "RD-Agent source escaped D workspace root"
  }
  foreach ($relative in @("mlruns", "ret.pkl", "qlib_res.csv", "combined_factors_df.parquet")) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $relative))) {
      throw "RD-Agent source workspace is incomplete: missing $relative"
    }
  }

  New-Item -ItemType Directory -Force -Path $NasRoot | Out-Null
  robocopy $source $destination /E /MT:8 /R:2 /W:2 /COPY:DAT /DCOPY:DAT /NFL /NDL /NJH /NJS /NP | Out-Null
  $copyExit = $LASTEXITCODE
  if ($copyExit -ge 8) {
    throw "RD-Agent workspace copy to NAS failed: robocopy exit $copyExit"
  }
  foreach ($relative in @("ret.pkl", "qlib_res.csv", "combined_factors_df.parquet")) {
    $sourceFile = Get-Item -LiteralPath (Join-Path $source $relative)
    $destinationFile = Get-Item -LiteralPath (Join-Path $destination $relative) -ErrorAction Stop
    if ($sourceFile.Length -ne $destinationFile.Length) {
      throw "RD-Agent workspace copy verification failed: $relative"
    }
  }
  $configs = @(Get-ChildItem -LiteralPath (Join-Path $destination "mlruns") -Recurse -File -Filter "config")
  if ($configs.Count -ne 1) {
    throw "RD-Agent persisted workspace must contain exactly one evaluated config; found $($configs.Count)"
  }
  return $persistentPath
}

function Reject-WatcherRequest {
  param(
    [string]$RequestFile,
    [string]$StatusFile,
    [string]$Reason
  )
  $obj = @{ state = "error"; msg = "非法请求: $Reason"; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
  try { ($obj | ConvertTo-Json -Compress) | Out-File -FilePath $StatusFile -Encoding utf8 } catch {}
  Remove-Item $RequestFile -Force -ErrorAction SilentlyContinue
  Write-Host "[watch] rejected request $([System.IO.Path]::GetFileName($RequestFile)): $Reason" -ForegroundColor Red
}

function Refresh-Csi300MembersCache() {
  $script = Join-Path $proj "scripts\refresh_csi300_members_cache.py"
  if (-not (Test-Path $script)) {
    Write-Host "[members] skip: $script not found" -ForegroundColor Yellow
    return
  }
  Write-Host "[members] refresh CSI300 members cache before advisor pro..." -ForegroundColor Cyan
  & "D:\anaconda3\python.exe" $script
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[members] refresh failed exit $LASTEXITCODE; advisor pro will continue with existing cache" -ForegroundColor Yellow
  }
}

function Test-RdagentMiningPreflight {
  param(
    [string]$Universe,
    [string]$RdagentRoot = "C:\rdagent",
    [string]$QlibRoot = "C:\qlib_data\cn_data"
  )
  $expected = switch ($Universe) {
    "csi300" { 300 }
    "csi500" { 500 }
    "csi1000" { 1000 }
    default { return [pscustomobject]@{ Ok = $false; Message = "unsupported universe: $Universe" } }
  }

  $templateRoot = Join-Path $RdagentRoot "rdagent\scenarios\qlib\experiment\factor_template"
  foreach ($name in @("conf_baseline.yaml", "conf_combined_factors.yaml", "conf_combined_factors_sota_model.yaml")) {
    $path = Join-Path $templateRoot $name
    if (-not (Test-Path -LiteralPath $path)) {
      return [pscustomobject]@{ Ok = $false; Message = "missing RD-Agent template: $path" }
    }
    $provider = Select-String -LiteralPath $path -Pattern '^\s*provider_uri\s*:' | Select-Object -First 1
    if (-not $provider -or $provider.Line -notmatch '/root/qlib_data/cn_data') {
      return [pscustomobject]@{ Ok = $false; Message = "$name uses a host path that Docker cannot read" }
    }
  }

  $calendarPath = Join-Path $QlibRoot "calendars\day.txt"
  $instrumentPath = Join-Path $QlibRoot "instruments\$Universe.txt"
  if (-not (Test-Path -LiteralPath $calendarPath) -or -not (Test-Path -LiteralPath $instrumentPath)) {
    return [pscustomobject]@{ Ok = $false; Message = "missing Qlib calendar or universe file for $Universe" }
  }
  $lastDate = (Get-Content -LiteralPath $calendarPath -Tail 1).Trim()
  if ($lastDate -notmatch '^\d{4}-\d{2}-\d{2}$') {
    return [pscustomobject]@{ Ok = $false; Message = "invalid Qlib calendar tail: $lastDate" }
  }

  $activeRows = 0
  $activeCodes = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($line in Get-Content -LiteralPath $instrumentPath) {
    $parts = $line -split "`t"
    if ($parts.Count -lt 3) { continue }
    if ($parts[1] -le $lastDate -and $parts[2] -ge $lastDate) {
      $activeRows += 1
      [void]$activeCodes.Add($parts[0])
    }
  }
  if ($activeRows -ne $expected -or $activeCodes.Count -ne $expected) {
    return [pscustomobject]@{
      Ok = $false
      Message = "$Universe point-in-time membership is invalid on ${lastDate}: rows=$activeRows unique=$($activeCodes.Count), expected=$expected"
    }
  }
  return [pscustomobject]@{ Ok = $true; Message = "$Universe preflight OK on $lastDate ($expected members)" }
}

function Test-RdagentPredictionPreflight {
  param(
    [string]$Universe,
    [string]$QlibRoot = "C:\qlib_data\cn_data",
    [string]$SourceQlibRoot = $qlibData,
    [string]$MarketDataRoot = (Join-Path $shared "tushare_daily"),
    [int]$MaxCalendarAgeDays = 14
  )
  $expected = switch ($Universe) {
    "csi300" { 300 }
    "csi500" { 500 }
    "csi1000" { 1000 }
    default { return [pscustomobject]@{ Ok = $false; Message = "unsupported prediction universe: $Universe" } }
  }
  $calendarPath = Join-Path $QlibRoot "calendars\day.txt"
  $sourceCalendarPath = Join-Path $SourceQlibRoot "calendars\day.txt"
  $instrumentPath = Join-Path $QlibRoot "instruments\$Universe.txt"
  foreach ($required in @($calendarPath, $sourceCalendarPath, $instrumentPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
      return [pscustomobject]@{ Ok = $false; Message = "prediction preflight missing file: $required" }
    }
  }
  $lastDate = [string](Get-Content -LiteralPath $calendarPath -Encoding UTF8 | Where-Object { $_.Trim() } | Select-Object -Last 1)
  $sourceLastDate = [string](Get-Content -LiteralPath $sourceCalendarPath -Encoding UTF8 | Where-Object { $_.Trim() } | Select-Object -Last 1)
  $lastDate = $lastDate.Trim()
  $sourceLastDate = $sourceLastDate.Trim()
  if ($lastDate -notmatch '^\d{4}-\d{2}-\d{2}$' -or $sourceLastDate -notmatch '^\d{4}-\d{2}-\d{2}$') {
    return [pscustomobject]@{ Ok = $false; Message = "invalid prediction calendar tail: local=$lastDate source=$sourceLastDate" }
  }
  if ($lastDate -ne $sourceLastDate) {
    return [pscustomobject]@{ Ok = $false; Message = "local Qlib calendar $lastDate != synced source $sourceLastDate" }
  }
  if (-not (Test-Path -LiteralPath $MarketDataRoot -PathType Container)) {
    return [pscustomobject]@{ Ok = $false; Message = "latest-market-data directory unavailable: $MarketDataRoot; prediction blocked" }
  }
  $latestMarketFile = Get-ChildItem -LiteralPath $MarketDataRoot -File -Filter "*.parquet" -ErrorAction SilentlyContinue |
    Where-Object { $_.Length -gt 0 -and $_.BaseName -match '^\d{8}' } |
    Sort-Object { $_.BaseName.Substring(0, 8) } -Descending |
    Select-Object -First 1
  if (-not $latestMarketFile) {
    return [pscustomobject]@{ Ok = $false; Message = "no dated market parquet in $MarketDataRoot; prediction blocked" }
  }
  $marketDataDate = $latestMarketFile.BaseName.Substring(0, 8)
  $marketDataDate = "$($marketDataDate.Substring(0,4))-$($marketDataDate.Substring(4,2))-$($marketDataDate.Substring(6,2))"
  if ($lastDate -ne $marketDataDate) {
    return [pscustomobject]@{
      Ok = $false
      Message = "Qlib calendar is behind latest market data: calendar=$lastDate market=$marketDataDate; rebuild Qlib calendar/features and $Universe constituents before prediction"
    }
  }
  try { $marketDate = [datetime]::ParseExact($lastDate, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture) }
  catch { return [pscustomobject]@{ Ok = $false; Message = "invalid prediction market date: $lastDate" } }
  $ageDays = [int]((Get-Date).Date - $marketDate.Date).TotalDays
  if ($ageDays -lt 0 -or $ageDays -gt $MaxCalendarAgeDays) {
    return [pscustomobject]@{ Ok = $false; Message = "Qlib calendar stale/future: tail=$lastDate age=${ageDays}d allowed=0..${MaxCalendarAgeDays}d" }
  }

  $activeRows = 0
  $malformedRows = 0
  $activeCodes = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($line in Get-Content -LiteralPath $instrumentPath -Encoding UTF8) {
    if (-not $line.Trim()) { continue }
    $parts = $line -split "`t"
    if ($parts.Count -lt 3 -or $parts[1] -notmatch '^\d{4}-\d{2}-\d{2}$' -or $parts[2] -notmatch '^\d{4}-\d{2}-\d{2}$') {
      $malformedRows += 1
      continue
    }
    if ($parts[1] -le $lastDate -and $parts[2] -ge $lastDate) {
      $activeRows += 1
      [void]$activeCodes.Add($parts[0].Trim().ToLowerInvariant())
    }
  }
  if ($malformedRows -gt 0) {
    return [pscustomobject]@{ Ok = $false; Message = "$Universe membership has $malformedRows malformed row(s)" }
  }
  if ($activeRows -ne $expected -or $activeCodes.Count -ne $expected) {
    return [pscustomobject]@{
      Ok = $false
      Message = "$Universe PIT membership invalid on ${lastDate}: rows=$activeRows unique=$($activeCodes.Count), expected=$expected"
    }
  }
  return [pscustomobject]@{
    Ok = $true
    Message = "$Universe prediction preflight OK on $lastDate ($expected members, calendar age ${ageDays}d)"
    MarketDate = $lastDate
    ExpectedCount = $expected
    FreshnessBasis = "latest_market_parquet+synced_source_calendar+wall_clock_guard"
  }
}

function Test-RdagentPredictionArtifact {
  param(
    [string]$Path,
    [string]$Universe,
    [string]$Model,
    [string]$MarketDate,
    [datetime]$RunStartedUtc
  )
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    return [pscustomobject]@{ Ok = $false; Message = "prediction artifact missing: $Path" }
  }
  $item = Get-Item -LiteralPath $Path
  if ($item.LastWriteTimeUtc -lt $RunStartedUtc.AddSeconds(-2)) {
    return [pscustomobject]@{ Ok = $false; Message = "prediction artifact was not generated by this run: $Path" }
  }
  try { $payload = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
  catch { return [pscustomobject]@{ Ok = $false; Message = "prediction artifact is invalid JSON: $Path" } }
  if ([string]$payload.universe -ne $Universe -or [string]$payload.model -ne $Model -or [string]$payload.as_of -ne $MarketDate) {
    return [pscustomobject]@{
      Ok = $false
      Message = "prediction artifact identity mismatch: got $($payload.universe)/$($payload.model)/$($payload.as_of), expected $Universe/$Model/$MarketDate"
    }
  }
  $expected = switch ($Universe) { "csi300" { 300 }; "csi500" { 500 }; "csi1000" { 1000 }; default { 0 } }
  $minimum = [math]::Ceiling($expected * 0.90)
  $hits = @($payload.hits)
  $uniqueHits = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($hit in $hits) { [void]$uniqueHits.Add([string]$hit.code) }
  if ([int]$payload.n_universe -lt $minimum -or $hits.Count -lt 50 -or $uniqueHits.Count -ne $hits.Count) {
    return [pscustomobject]@{
      Ok = $false
      Message = "prediction artifact coverage invalid: n_universe=$($payload.n_universe) hits=$($hits.Count) unique_hits=$($uniqueHits.Count), required>=$minimum/50"
    }
  }
  return [pscustomobject]@{ Ok = $true; Message = "prediction artifact verified: $Path" }
}

function Test-RdagentScoreArtifact {
  param(
    [string]$Path,
    [string]$Model,
    [string]$MarketDate,
    [int]$ExpectedCount,
    [datetime]$RunStartedUtc
  )
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    return [pscustomobject]@{ Ok = $false; Message = "score artifact missing: $Path" }
  }
  $item = Get-Item -LiteralPath $Path
  if ($item.LastWriteTimeUtc -lt $RunStartedUtc.AddSeconds(-2)) {
    return [pscustomobject]@{ Ok = $false; Message = "score artifact was not generated by this run: $Path" }
  }
  try { $payload = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
  catch { return [pscustomobject]@{ Ok = $false; Message = "score artifact is invalid JSON: $Path" } }
  $minimum = [math]::Ceiling($ExpectedCount * 0.90)
  $scoreCount = if ($payload.scores) { @($payload.scores.PSObject.Properties).Count } else { 0 }
  if ([string]$payload.model -ne $Model -or [string]$payload.as_of -ne $MarketDate -or
      [int]$payload.n -lt $minimum -or $scoreCount -lt $minimum) {
    return [pscustomobject]@{
      Ok = $false
      Message = "score artifact identity/coverage mismatch: model=$($payload.model) as_of=$($payload.as_of) n=$($payload.n) scores=$scoreCount"
    }
  }
  return [pscustomobject]@{ Ok = $true; Message = "score artifact verified: $Path" }
}

function Get-RdagentGatewayFailureInfo {
  param($ErrorRecord, [string]$Stage)
  $httpStatus = $null
  try {
    if ($ErrorRecord.Exception.Response -and $ErrorRecord.Exception.Response.StatusCode) {
      $httpStatus = [int]$ErrorRecord.Exception.Response.StatusCode
    }
  } catch {}

  $webStatus = $null
  $exception = $ErrorRecord.Exception
  if ($exception -is [System.Net.WebException]) {
    $webStatus = $exception.Status
  } elseif ($exception.InnerException -is [System.Net.WebException]) {
    $webStatus = $exception.InnerException.Status
  }

  $failureKind = "request"
  if ($httpStatus -in @(401, 403)) {
    $failureKind = "auth"
  } elseif ($httpStatus -eq 429) {
    $failureKind = "rate_limit"
  } elseif ($null -ne $httpStatus -and $httpStatus -ge 500) {
    $failureKind = "server"
  } elseif ($webStatus -eq [System.Net.WebExceptionStatus]::Timeout) {
    $failureKind = "timeout"
  } elseif ($webStatus -in @(
      [System.Net.WebExceptionStatus]::ConnectFailure,
      [System.Net.WebExceptionStatus]::ConnectionClosed,
      [System.Net.WebExceptionStatus]::NameResolutionFailure,
      [System.Net.WebExceptionStatus]::ReceiveFailure,
      [System.Net.WebExceptionStatus]::SendFailure
    )) {
    $failureKind = "transport"
  }
  $retryable = $failureKind -in @("rate_limit", "server", "timeout", "transport")
  return [pscustomobject]@{
    Stage = $Stage
    FailureKind = $failureKind
    HttpStatus = $httpStatus
    Retryable = $retryable
  }
}

function Invoke-RdagentModelsProbe {
  param(
    [string]$BaseUrl,
    [string]$ApiKey,
    [string]$Model,
    [int]$Attempts = 2
  )
  $lastFailure = $null
  for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    try {
      $response = Invoke-RestMethod -Uri "$BaseUrl/models" -Headers @{ Authorization = "Bearer $ApiKey" } -TimeoutSec 12
      $models = @($response.data | ForEach-Object { [string]$_.id })
      if ($models -notcontains $Model) {
        return [pscustomobject]@{ Ok = $false; Stage = "models"; FailureKind = "configuration"; HttpStatus = $null; Retryable = $false }
      }
      return [pscustomobject]@{ Ok = $true; Stage = "models"; FailureKind = "none"; HttpStatus = $null; Retryable = $false }
    } catch {
      $lastFailure = Get-RdagentGatewayFailureInfo -ErrorRecord $_ -Stage "models"
      if (-not $lastFailure.Retryable -or $attempt -ge $Attempts) { break }
      Start-Sleep -Seconds 2
    }
  }
  return [pscustomobject]@{
    Ok = $false
    Stage = "models"
    FailureKind = $lastFailure.FailureKind
    HttpStatus = $lastFailure.HttpStatus
    Retryable = $lastFailure.Retryable
  }
}

function Invoke-RdagentMarkerProbe {
  param(
    [string]$BaseUrl,
    [string]$ApiKey,
    [string]$Model,
    [int]$TimeoutSeconds = 45,
    [string]$Stage = "primary_chat"
  )
  $probeMarker = "QI_GATEWAY_READY_7F4A"
  $bodyData = @{
    model = $Model
    messages = @(
      @{ role = "system"; content = "Return exactly the marker supplied by the user and nothing else." },
      @{ role = "user"; content = $probeMarker }
    )
    stream = $false
  }
  if ($Model.Trim().ToLowerInvariant() -in @("k3", "kimi-k3")) {
    # Kimi K3 always reasons and only accepts its fixed sampling parameters.
    $bodyData.reasoning_effort = "max"
    $bodyData.max_completion_tokens = 1024
  } else {
    $bodyData.max_tokens = 32
    $bodyData.temperature = 0
  }
  $body = $bodyData | ConvertTo-Json -Depth 5
  try {
    $chat = Invoke-RestMethod -Method Post -Uri "$BaseUrl/chat/completions" -Headers @{ Authorization = "Bearer $ApiKey" } -ContentType "application/json" -Body $body -TimeoutSec $TimeoutSeconds
  } catch {
    $failure = Get-RdagentGatewayFailureInfo -ErrorRecord $_ -Stage $Stage
    return [pscustomobject]@{ Ok = $false; Stage = $Stage; FailureKind = $failure.FailureKind; HttpStatus = $failure.HttpStatus; Retryable = $failure.Retryable }
  }
  if (@($chat.choices).Count -lt 1) {
    return [pscustomobject]@{ Ok = $false; Stage = $Stage; FailureKind = "response"; HttpStatus = $null; Retryable = $false }
  }
  $probeContent = [string]$chat.choices[0].message.content
  if ($probeContent.Trim() -ne $probeMarker) {
    return [pscustomobject]@{ Ok = $false; Stage = $Stage; FailureKind = "response"; HttpStatus = $null; Retryable = $false }
  }
  return [pscustomobject]@{ Ok = $true; Stage = $Stage; FailureKind = "none"; HttpStatus = $null; Retryable = $false }
}

function Test-RdagentModelGateway {
  param([string]$EnvPath = "C:\rdagent\.env")
  if (-not (Test-Path -LiteralPath $EnvPath)) {
    return [pscustomobject]@{ Ok = $false; IsLocal = $false; Stage = "configuration"; FailureKind = "configuration"; RestartRecommended = $false; Message = "missing RD-Agent .env" }
  }
  $envValues = @{}
  foreach ($line in Get-Content -LiteralPath $EnvPath -Encoding UTF8) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
      $envValues[$matches[1]] = ([string]$matches[2]).Trim()
    }
  }
  $base = ([string]$envValues.CHAT_OPENAI_BASE_URL).TrimEnd('/')
  $key = [string]$envValues.CHAT_OPENAI_API_KEY
  $model = ([string]$envValues.CHAT_MODEL) -replace '^openai/', ''
  $isLocal = $base -match '^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)'
  if (-not $base -or -not $key -or -not $model) {
    return [pscustomobject]@{ Ok = $false; IsLocal = $isLocal; Stage = "configuration"; FailureKind = "configuration"; RestartRecommended = $false; Message = "model gateway configuration is incomplete" }
  }

  $modelsProbe = Invoke-RdagentModelsProbe -BaseUrl $base -ApiKey $key -Model $model
  if (-not $modelsProbe.Ok -and $isLocal) {
    $restartRecommended = $isLocal -and $modelsProbe.FailureKind -in @("transport", "timeout", "server")
    return [pscustomobject]@{
      Ok = $false
      IsLocal = $isLocal
      Stage = "models"
      FailureKind = $modelsProbe.FailureKind
      HttpStatus = $modelsProbe.HttpStatus
      RestartRecommended = $restartRecommended
      Message = "model gateway models probe failed ($($modelsProbe.FailureKind))"
    }
  }
  # Remote OpenAI-compatible gateways do not all guarantee a stable /models
  # endpoint. The marker chat is authoritative, and configured fallbacks must
  # remain usable even if the primary provider's model listing is unavailable.

  $primaryProbe = $null
  for ($primaryAttempt = 1; $primaryAttempt -le 2; $primaryAttempt++) {
    $primaryProbe = Invoke-RdagentMarkerProbe -BaseUrl $base -ApiKey $key -Model $model -Stage "primary_chat"
    if ($primaryProbe.Ok) {
      return [pscustomobject]@{
        Ok = $true
        IsLocal = $isLocal
        Stage = "complete"
        FailureKind = "none"
        RestartRecommended = $false
        Degraded = $false
        Message = "model gateway and primary chat OK ($model)"
        FallbacksTested = 0
      }
    }
    if (-not $primaryProbe.Retryable -or $primaryAttempt -ge 2) { break }
    Start-Sleep -Seconds 5
  }

  $fallbackModels = @(
    ([string]$envValues.CHAT_FALLBACK_MODELS).Split(',') |
      ForEach-Object { $_.Trim() } |
      Where-Object { $_ }
  )
  $fallbackFailures = @()
  for ($index = 0; $index -lt $fallbackModels.Count; $index++) {
    $number = $index + 1
    $fallbackModel = $fallbackModels[$index] -replace '^openai/', ''
    $keySource = [string]$envValues["CHAT_FALLBACK_${number}_API_KEY_ENV"]
    $baseSource = [string]$envValues["CHAT_FALLBACK_${number}_BASE_URL_ENV"]
    $fallbackKey = if ($keySource) { [string]$envValues[$keySource] } else { $key }
    $fallbackBase = if ($baseSource) { ([string]$envValues[$baseSource]).TrimEnd('/') } else { $base }
    if (-not $fallbackKey -or -not $fallbackBase -or -not $fallbackModel) {
      $fallbackFailures += "${fallbackModel}:configuration"
      continue
    }
    $fallbackTimeout = 45
    try {
      $configuredTimeout = [int]$envValues["CHAT_FALLBACK_${number}_TIMEOUT"]
      if ($configuredTimeout -ge 10 -and $configuredTimeout -le 180) { $fallbackTimeout = $configuredTimeout }
    } catch {}
    $fallbackProbe = Invoke-RdagentMarkerProbe -BaseUrl $fallbackBase -ApiKey $fallbackKey -Model $fallbackModel -TimeoutSeconds $fallbackTimeout -Stage "fallback_chat"
    if ($fallbackProbe.Ok) {
      return [pscustomobject]@{
        Ok = $true
        IsLocal = $isLocal
        Stage = "complete"
        FailureKind = "none"
        RestartRecommended = $false
        Degraded = $true
        Message = "primary chat unavailable ($($primaryProbe.FailureKind)); fallback chat OK ($fallbackModel)"
        FallbacksTested = $number
        FallbackModel = $fallbackModel
      }
    }
    $fallbackFailures += "${fallbackModel}:$($fallbackProbe.FailureKind)"
  }

  $restartRecommended = $false
  if ($isLocal -and $primaryProbe.FailureKind -in @("transport", "timeout")) {
    $modelsRecheck = Invoke-RdagentModelsProbe -BaseUrl $base -ApiKey $key -Model $model
    $restartRecommended = -not $modelsRecheck.Ok -and $modelsRecheck.FailureKind -in @("transport", "timeout", "server")
  }
  return [pscustomobject]@{
    Ok = $false
    IsLocal = $isLocal
    Stage = "chat_chain"
    FailureKind = $primaryProbe.FailureKind
    HttpStatus = $primaryProbe.HttpStatus
    RestartRecommended = $restartRecommended
    Degraded = $false
    Message = "primary and fallback chat probes failed; fallbacks tested $($fallbackModels.Count)"
    FallbacksTested = $fallbackModels.Count
    FallbackFailures = $fallbackFailures
  }
}

function Wait-RdagentModelGatewayReady {
  param(
    [string]$EnvPath = "C:\rdagent\.env",
    [int]$TimeoutSeconds = 30,
    [int]$PollMilliseconds = 1000
  )
  if (-not (Test-Path -LiteralPath $EnvPath)) { return $false }
  $envValues = @{}
  foreach ($line in Get-Content -LiteralPath $EnvPath -Encoding UTF8) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
      $envValues[$matches[1]] = ([string]$matches[2]).Trim()
    }
  }
  $base = ([string]$envValues.CHAT_OPENAI_BASE_URL).TrimEnd('/')
  $key = [string]$envValues.CHAT_OPENAI_API_KEY
  $model = ([string]$envValues.CHAT_MODEL) -replace '^openai/', ''
  if (-not $base -or -not $key -or -not $model) { return $false }
  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  do {
    try {
      $response = Invoke-RestMethod -Uri "$base/models" -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 3
      $models = @($response.data | ForEach-Object { [string]$_.id })
      if ($models -contains $model) { return $true }
    } catch {}
    if ([DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds $PollMilliseconds }
  } while ([DateTime]::UtcNow -lt $deadline)
  return $false
}

function Write-InclStatus($state, $msg, [hashtable]$details = $null) {
  $obj = @{ state = $state; msg = $msg; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
  if ($details) {
    foreach ($key in $details.Keys) { $obj[$key] = $details[$key] }
  }
  ($obj | ConvertTo-Json -Compress) | Out-File -FilePath $inclStatusFile -Encoding utf8
}
function Write-RefreshStatus($state, $msg, $kind) {
  $obj = @{ state = $state; msg = $msg; kind = $kind; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
  ($obj | ConvertTo-Json -Compress) | Out-File -FilePath $refreshStatusFile -Encoding utf8
}

function Write-JsonAtomic($path, $payload) {
  $parent = Split-Path -Parent $path
  if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
  $tempPath = Join-Path $parent ("." + (Split-Path -Leaf $path) + "." + $PID + "." + [guid]::NewGuid().ToString("N") + ".tmp")
  try {
    $json = $payload | ConvertTo-Json -Depth 10 -Compress
    [System.IO.File]::WriteAllText($tempPath, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempPath -Destination $path -Force -ErrorAction Stop
  } finally {
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
  }
}

function Publish-FileAtomic($source, $destination) {
  if (-not (Test-Path -LiteralPath $source)) { return $false }
  $parent = Split-Path -Parent $destination
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  $tempPath = Join-Path $parent ("." + (Split-Path -Leaf $destination) + "." + $PID + "." + [guid]::NewGuid().ToString("N") + ".tmp")
  try {
    Copy-Item -LiteralPath $source -Destination $tempPath -Force -ErrorAction Stop
    Move-Item -LiteralPath $tempPath -Destination $destination -Force -ErrorAction Stop
    return $true
  } catch {
    Write-Host "[watch] atomic publish failed: $source -> $destination ($($_.Exception.Message))" -ForegroundColor Red
    return $false
  } finally {
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
  }
}

function Try-RecoverStaleProcessLockFile($path) {
  $guardPath = "$path.reclaim"
  $guardStream = $null
  $guardLocked = $false
  try {
    $parent = Split-Path -Parent $path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $guardStream = [System.IO.File]::Open(
      $guardPath,
      [System.IO.FileMode]::OpenOrCreate,
      [System.IO.FileAccess]::ReadWrite,
      [System.IO.FileShare]::ReadWrite
    )
    if ($guardStream.Length -lt 1) {
      $guardStream.SetLength(1)
      $guardStream.Flush($true)
    }
    $guardStream.Lock(0, 1)
    $guardLocked = $true

    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $true }
    $owner = $null
    try {
      $owner = Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    } catch {}
    $ageSeconds = 0.0
    try { $ageSeconds = ((Get-Date) - (Get-Item -LiteralPath $path -ErrorAction Stop).LastWriteTime).TotalSeconds } catch {}
    $ownerPid = 0
    $pidValid = $false
    if ($owner) {
      $pidValid = [int]::TryParse([string]$owner.pid, [ref]$ownerPid)
    }
    $stale = $false
    if (-not $owner -or -not $pidValid) {
      # Match scripts/process_lock.py: tolerate a creator that has made the file
      # but has not finished its JSON write yet.
      $stale = ($ageSeconds -gt 60)
    } else {
      $sameHost = ([string]$owner.host).Equals(
        [Environment]::MachineName,
        [System.StringComparison]::OrdinalIgnoreCase
      )
      if ($sameHost) {
        $stale = $null -eq (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue)
      } else {
        $stale = ($ageSeconds -gt (12 * 3600))
      }
    }
    if (-not $stale) { return $false }
    Remove-Item -LiteralPath $path -Force -ErrorAction Stop
    Write-Host "[watch] recovered stale process lock: $path" -ForegroundColor Yellow
    return $true
  } catch [System.IO.IOException] {
    # Python process_lock.py uses an OS byte-range lock on this same guard.
    return $false
  } catch {
    Write-Host "[watch] stale process lock recovery failed: $path ($($_.Exception.Message))" -ForegroundColor Yellow
    return $false
  } finally {
    if ($guardStream) {
      if ($guardLocked) {
        try { $guardStream.Unlock(0, 1) } catch {}
      }
      $guardStream.Dispose()
    }
  }
}

function Try-AcquireProcessLockFile($path, $reason) {
  $owner = [ordered]@{
    pid = [int]$PID
    host = [Environment]::MachineName
    token = [guid]::NewGuid().ToString("N")
    reason = [string]$reason
    created_at = (Get-Date).ToString("s")
  }
  for ($attempt = 0; $attempt -lt 2; $attempt++) {
    $stream = $null
    try {
      $parent = Split-Path -Parent $path
      if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
      $stream = [System.IO.File]::Open(
        $path,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
      )
      $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes(($owner | ConvertTo-Json -Compress))
      $stream.Write($bytes, 0, $bytes.Length)
      $stream.Flush()
      return [pscustomobject]$owner
    } catch [System.IO.IOException] {
      if ($attempt -eq 0 -and (Try-RecoverStaleProcessLockFile $path)) {
        continue
      }
      return $null
    } catch {
      Write-Host "[watch] process lock acquire failed: $path ($($_.Exception.Message))" -ForegroundColor Red
      return $null
    } finally {
      if ($stream) { $stream.Dispose() }
    }
  }
  return $null
}

function Release-ProcessLockFile($path, $owner) {
  if (-not $owner) { return }
  try {
    $current = Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    if ([string]$current.token -eq [string]$owner.token) {
      Remove-Item -LiteralPath $path -Force -ErrorAction Stop
    }
  } catch {
    Write-Host "[watch] process lock release failed: $path ($($_.Exception.Message))" -ForegroundColor Yellow
  }
}

function Get-FileSnapshotFingerprint($path) {
  try {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return "missing" }
    return "sha256:" + (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
  } catch {
    Write-Host "[watch] file fingerprint failed: $path ($($_.Exception.Message))" -ForegroundColor Red
    return $null
  }
}

function Read-AutoRefreshState($path) {
  try {
    if (Test-Path -LiteralPath $path) {
      return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
    }
  } catch {}
  return $null
}

function Get-AutoRefreshRetryMinutes([int]$failureCount) {
  $power = [Math]::Min([Math]::Max($failureCount - 1, 0), 10)
  $delay = [int]($autoRefreshRetryBaseMinutes * [Math]::Pow(2, $power))
  return [Math]::Min($autoRefreshRetryCapMinutes, $delay)
}

function Set-AutoRefreshState($path, $slot, $state, $reason) {
  $now = Get-Date
  $previous = Read-AutoRefreshState $path
  $sameSlot = ($previous -and [string]$previous.last_attempt_slot -eq [string]$slot)
  $failureCount = if ($sameSlot) { [int]$previous.failure_count } else { 0 }
  $obj = @{
    state = $state
    reason = $reason
    failure_count = $failureCount
    last_attempt = $now.ToString("yyyy-MM-ddTHH:mm:ss")
    last_attempt_slot = $slot
  }
  if ($previous) {
    if ($previous.last_success) { $obj.last_success = [string]$previous.last_success }
    if ($previous.last_success_slot) { $obj.last_success_slot = [string]$previous.last_success_slot }
    if ($previous.last_run) { $obj.last_run = [string]$previous.last_run }
    if ($previous.last_slot) { $obj.last_slot = [string]$previous.last_slot }
  }
  if ($state -eq "done") {
    $obj.failure_count = 0
    $obj.last_success = $obj.last_attempt
    $obj.last_success_slot = $slot
    # Preserve the legacy fields while older deployments are still reading them.
    $obj.last_run = $obj.last_attempt
    $obj.last_slot = $slot
  } elseif ($state -eq "error") {
    $obj.failure_count = $failureCount + 1
    $retryMinutes = Get-AutoRefreshRetryMinutes $obj.failure_count
    $obj.next_retry_at = $now.AddMinutes($retryMinutes).ToString("yyyy-MM-ddTHH:mm:ss")
  } elseif ($previous -and $previous.next_retry_at) {
    $obj.next_retry_at = [string]$previous.next_retry_at
  }
  Write-JsonAtomic $path $obj
  return [pscustomobject]$obj
}

function Test-AutoRefreshRetryReady($state) {
  if (-not $state -or -not $state.next_retry_at) { return $true }
  try { return ((Get-Date) -ge [datetime]::Parse([string]$state.next_retry_at)) } catch { return $true }
}
function Invoke-CrossMarketRefresh($kind) {
  Write-Host "[watch] 刷新 跨市场存储映射 ..." -ForegroundColor Cyan
  Write-RefreshStatus "running" "重算 跨市场存储映射 (~1分钟)" $kind
  & "D:\anaconda3\python.exe" "$proj\scripts\export_cross_market_storage.py" --output-dir $shared 2>&1 | Out-Null
  $okc = ($LASTEXITCODE -eq 0)
  foreach ($o in @("cross_market_storage.json", "cross_market_storage_status.json")) {
    $outPath = Get-DataOutput $o
    if (Test-Path $outPath) { Copy-Item $outPath (Join-Path $shared $o) -Force } else { $okc = $false }
  }
  if ($okc) {
    Write-RefreshStatus "done" "跨市场存储映射已更新" $kind
    Write-Host "[watch] 跨市场存储映射完成" -ForegroundColor Green
  } else {
    Write-RefreshStatus "error" "跨市场存储映射刷新失败, 检查 export_cross_market_storage.py" $kind
  }
  return $okc
}
function Invoke-TopRiskRefresh($kind) {
  Write-Host "[watch] 刷新 市场与板块见顶风险 ETF 数据 ..." -ForegroundColor Cyan
  Write-RefreshStatus "running" "重算宽基ETF份额/日线缓存" $kind
  if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
  $ok = $true
  & "D:\anaconda3\python.exe" "$proj\scripts\backtest_etf_flow_signal.py" --refresh 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { $ok = $false }
  Write-RefreshStatus "running" "重算板块ETF份额/日线缓存" $kind
  & "D:\anaconda3\python.exe" "$proj\scripts\backtest_sector_etf_flow_signal.py" --refresh 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { $ok = $false }
  Write-RefreshStatus "running" "重算汇金持仓ETF份额/申赎代理与严格回测" $kind
  & "D:\anaconda3\python.exe" "$proj\scripts\backtest_huijin_etf_flow.py" --refresh 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { $ok = $false }

  $localCache = Join-Path $proj "data\etf_flow_cache"
  if ($ok -and (Test-Path $localCache)) {
    try {
      $sharedCache = Join-Path $shared "etf_flow_cache"
      New-Item -ItemType Directory -Force -Path $sharedCache | Out-Null
      Copy-Item -Path (Join-Path $localCache "*") -Destination $sharedCache -Recurse -Force
      $nasData = $null
      if ($nasQuantinvestRoot) {
        $nasData = Join-Path $nasQuantinvestRoot "data"
        $nasCache = Join-Path $nasData "etf_flow_cache"
        New-Item -ItemType Directory -Force -Path $nasCache | Out-Null
        Copy-Item -Path (Join-Path $localCache "*") -Destination $nasCache -Recurse -Force
      }
      foreach ($name in @("etf_flow_top_signal.json", "sector_etf_flow_signal.json", "huijin_etf_flow.json")) {
        $src = Join-Path $proj "data\$name"
        if (Test-Path $src) {
          if ($nasData) { Copy-Item $src (Join-Path $nasData $name) -Force }
          Copy-Item $src (Join-Path $shared $name) -Force
        }
      }
    } catch {
      $ok = $false
      Write-Host "[watch] top_risk copy failed: $($_.Exception.Message)" -ForegroundColor Red
    }
  }

  if ($ok) {
    Write-RefreshStatus "done" "见顶风险及汇金ETF资金代理已更新" $kind
    Write-Host "[watch] 见顶风险及汇金ETF资金代理完成" -ForegroundColor Green
  } else {
    Write-RefreshStatus "error" "ETF数据刷新失败, 检查 broad / sector / huijin 三个回测脚本" $kind
  }
  return $ok
}
function Invoke-MoneyOutflowRefresh($kind) {
  Write-Host "[watch] 刷新 资金流出验证 ..." -ForegroundColor Cyan
  Write-RefreshStatus "running" "重算资金流出个股/板块排行" $kind
  if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
  $end = Get-Date -Format "yyyy-MM-dd"
  & "D:\anaconda3\python.exe" "$proj\scripts\backtest_money_outflow_signal.py" --start 2026-01-01 --end $end --sample-every 1 --sleep 0.05 2>&1 | Out-Null
  $ok = ($LASTEXITCODE -eq 0 -and (Test-Path "$proj\data\money_outflow_signal.json"))
  if ($ok) {
    try {
      Copy-Item "$proj\data\money_outflow_signal.json" (Join-Path $shared "money_outflow_signal.json") -Force
      if ($nasQuantinvestRoot) {
        $nasData = Join-Path $nasQuantinvestRoot "data"
        New-Item -ItemType Directory -Force -Path $nasData | Out-Null
        Copy-Item "$proj\data\money_outflow_signal.json" (Join-Path $nasData "money_outflow_signal.json") -Force
      }
      Write-RefreshStatus "done" "资金流出验证已更新" $kind
      Write-Host "[watch] 资金流出验证完成" -ForegroundColor Green
    } catch {
      $ok = $false
      Write-RefreshStatus "error" "资金流出结果复制失败" $kind
      Write-Host "[watch] money_outflow copy failed: $($_.Exception.Message)" -ForegroundColor Red
    }
  } else {
    Write-RefreshStatus "error" "资金流出验证刷新失败, 检查 backtest_money_outflow_signal.py" $kind
  }
  return $ok
}
function Get-DataOutput($name) {
  $sharedPath = Join-Path $shared $name
  if (Test-Path $sharedPath) { return $sharedPath }
  return (Join-Path (Join-Path $proj "data") $name)
}
function Invoke-TransferEventsIncremental($reason, $autoSlot = "") {
  if ($autoSlot) { [void](Set-AutoRefreshState $transferAutoFile $autoSlot "running" $reason) }
  Write-Host "[watch] 增量刷新 询价转让/协转解禁 ($reason) ..." -ForegroundColor Cyan
  Write-RefreshStatus "running" "增量同步询价转让/协转解禁" "transfer_events"
  $transferLock = Try-AcquireProcessLockFile $transferDocumentsLockFile "watcher-transfer-documents"
  if (-not $transferLock) {
    if ($autoSlot) { [void](Set-AutoRefreshState $transferAutoFile $autoSlot "error" "$reason lock-busy") }
    Write-RefreshStatus "error" "询价转让写锁忙，共享数据保持不变" "transfer_events"
    return $false
  }
  try {
  & "D:\anaconda3\python.exe" "$proj\scripts\export_transfer_events.py" --incremental --incremental-overlap-days 2 2>&1 | Out-Null
  $exportExit = $LASTEXITCODE
  $transferPath = "$proj\data\cninfo_transfer.json"
  $overlayPath = "$proj\data\transfer_terms_overlay.json"
  if ($exportExit -ne 0 -or -not (Test-Path $transferPath)) {
    if ($autoSlot) { [void](Set-AutoRefreshState $transferAutoFile $autoSlot "error" "$reason export-exit-$exportExit") }
    Write-RefreshStatus "error" "询价转让/协转解禁增量刷新失败" "transfer_events"
    Write-Host "[watch] export_transfer_events incremental exit $exportExit" -ForegroundColor Red
    return $false
  }
  Write-RefreshStatus "running" "解析转让价格与比例" "transfer_events"
  & "D:\anaconda3\python.exe" "$proj\scripts\enrich_transfer_terms.py" --source $transferPath --output $overlayPath --limit 30 2>&1 | Out-Null
  $enrichExit = $LASTEXITCODE
  if ($enrichExit -ne 0 -or -not (Test-Path $overlayPath)) {
    if ($autoSlot) { [void](Set-AutoRefreshState $transferAutoFile $autoSlot "error" "$reason enrich-exit-$enrichExit") }
    Write-RefreshStatus "error" "转让公告已更新，但价格与比例解析失败" "transfer_events"
    Write-Host "[watch] enrich_transfer_terms exit $enrichExit" -ForegroundColor Red
    return $false
  }
  if (-not (Publish-PlacementFileSet `
      $transferPath $overlayPath $shared `
      "cninfo_transfer.json" "transfer" `
      "transfer_terms_overlay.json" "overlay")) {
    if ($autoSlot) { [void](Set-AutoRefreshState $transferAutoFile $autoSlot "error" "$reason grouped-publish-failed") }
    Write-RefreshStatus "error" "转让公告与价格比例成组发布失败，共享端已回滚" "transfer_events"
    return $false
  }
  if ($autoSlot) {
    [void](Set-AutoRefreshState $transferAutoFile $autoSlot "done" $reason)
  }
  Write-RefreshStatus "done" "询价转让/协转解禁及价格比例已更新" "transfer_events"
  return $true
  } finally {
    Release-ProcessLockFile $transferDocumentsLockFile $transferLock
  }
}
function Invoke-TransferEventsAutoIfDue {
  $now = Get-Date
  if ($now.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) { return }
  $minutes = ($now.Hour * 60) + $now.Minute
  $slot = ""
  if ($minutes -ge (6 * 60) -and $minutes -lt (8 * 60 + 30)) {
    $slot = $now.ToString("yyyy-MM-dd") + "-preopen"
  } elseif ($minutes -ge (18 * 60 + 30)) {
    $slot = $now.ToString("yyyy-MM-dd") + "-afterclose"
  }
  if (-not $slot) { return }
  $state = Read-AutoRefreshState $transferAutoFile
  $lastSuccessSlot = ""
  if ($state) {
    $lastSuccessSlot = if ($state.last_success_slot) { [string]$state.last_success_slot } else { [string]$state.last_slot }
  }
  if ($lastSuccessSlot -ne $slot -and (Test-AutoRefreshRetryReady $state)) {
    [void](Invoke-TransferEventsIncremental "scheduled-$slot" $slot)
  }
}

function Test-PlacementJson($path, $kind) {
  if (-not (Test-Path $path)) { return $false }
  try {
    $payload = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $payload.items -or -not $payload.updated) { return $false }
    $items = @($payload.items)
    if ($items.Count -eq 0) { return $false }
    if ($kind -in @("lifecycle", "transfer") -and @($payload.errors).Count -gt 0) { return $false }
    if ($kind -eq "lifecycle" -and [int]$payload.count -ne $items.Count) { return $false }
    if ($kind -eq "overlay" -and [int](($payload.stats).errors) -gt 0) { return $false }
    return $true
  } catch {
    return $false
  }
}

function Publish-PlacementFileSet(
  $assetSource,
  $lifecycleSource,
  $destinationRoot,
  $firstName = "asset_injection.json",
  $firstKind = "asset",
  $secondName = "cninfo_placement.json",
  $secondKind = "lifecycle"
) {
  $transaction = $PID.ToString() + "." + [guid]::NewGuid().ToString("N")
  $entries = @(
    [pscustomobject]@{
      Source = $assetSource
      Destination = Join-Path $destinationRoot $firstName
      Kind = $firstKind
      Stage = $null
      Backup = $null
      HadOriginal = $false
      Published = $false
    },
    [pscustomobject]@{
      Source = $lifecycleSource
      Destination = Join-Path $destinationRoot $secondName
      Kind = $secondKind
      Stage = $null
      Backup = $null
      HadOriginal = $false
      Published = $false
    }
  )
  $cleanupBackups = $false
  try {
    New-Item -ItemType Directory -Force -Path $destinationRoot -ErrorAction Stop | Out-Null

    # Stage and validate the complete pair before changing either published file.
    foreach ($entry in $entries) {
      $leaf = Split-Path -Leaf $entry.Destination
      $entry.Stage = Join-Path $destinationRoot ("." + $leaf + "." + $transaction + ".stage")
      $entry.Backup = Join-Path $destinationRoot ("." + $leaf + "." + $transaction + ".backup")
      Copy-Item -LiteralPath $entry.Source -Destination $entry.Stage -Force -ErrorAction Stop
      if (-not (Test-PlacementJson $entry.Stage $entry.Kind)) {
        throw "invalid staged placement output: $($entry.Kind)"
      }
    }

    # Keep a recoverable snapshot of the whole old pair before the first rename.
    foreach ($entry in $entries) {
      $entry.HadOriginal = Test-Path -LiteralPath $entry.Destination -PathType Leaf
      if ($entry.HadOriginal) {
        Copy-Item -LiteralPath $entry.Destination -Destination $entry.Backup -Force -ErrorAction Stop
      }
    }

    # Same-directory renames make each file replacement atomic.  If the second
    # replacement fails, the catch block restores the first from its backup.
    foreach ($entry in $entries) {
      Move-Item -LiteralPath $entry.Stage -Destination $entry.Destination -Force -ErrorAction Stop
      $entry.Published = $true
    }
    $cleanupBackups = $true
    return $true
  } catch {
    $publishError = $_.Exception.Message
    $rollbackOk = $true
    for ($index = $entries.Count - 1; $index -ge 0; $index--) {
      $entry = $entries[$index]
      if (-not $entry.Published) { continue }
      try {
        if ($entry.HadOriginal) {
          if (-not (Test-Path -LiteralPath $entry.Backup -PathType Leaf)) {
            throw "missing rollback backup: $($entry.Backup)"
          }
          Move-Item -LiteralPath $entry.Backup -Destination $entry.Destination -Force -ErrorAction Stop
        } else {
          Remove-Item -LiteralPath $entry.Destination -Force -ErrorAction Stop
        }
      } catch {
        $rollbackOk = $false
        Write-Host "[watch] placement rollback failed: $($entry.Destination) ($($_.Exception.Message)); backup=$($entry.Backup)" -ForegroundColor Red
      }
    }
    $cleanupBackups = $rollbackOk
    Write-Host "[watch] placement grouped publish failed: $publishError; rollback_ok=$rollbackOk" -ForegroundColor Red
    return $false
  } finally {
    foreach ($entry in $entries) {
      if ($entry.Stage) {
        Remove-Item -LiteralPath $entry.Stage -Force -ErrorAction SilentlyContinue
      }
      if ($cleanupBackups -and $entry.Backup) {
        Remove-Item -LiteralPath $entry.Backup -Force -ErrorAction SilentlyContinue
      }
    }
  }
}

function Invoke-PlacementEventsRefresh($reason, $autoSlot = "") {
  if ($autoSlot) { [void](Set-AutoRefreshState $placementAutoFile $autoSlot "running" $reason) }
  Write-Host "[watch] 刷新定增生命周期 ($reason) ..." -ForegroundColor Cyan
  Write-RefreshStatus "running" "同步定增样本与公告生命周期" "placement_events"
  $placementLock = Try-AcquireProcessLockFile $placementDocumentsLockFile "watcher-placement-documents"
  if (-not $placementLock) {
    if ($autoSlot) { [void](Set-AutoRefreshState $placementAutoFile $autoSlot "error" "$reason lock-busy") }
    Write-RefreshStatus "error" "定增写锁忙，共享数据保持不变" "placement_events"
    return $false
  }
  try {

  $assetPath = Join-Path $proj "data\asset_injection.json"
  $placementPath = Join-Path $proj "data\cninfo_placement.json"
  $assetStale = -not (Test-Path $assetPath)
  if (-not $assetStale) {
    try { $assetStale = (Get-Item $assetPath).LastWriteTime -lt (Get-Date).AddHours(-18) } catch { $assetStale = $true }
  }
  $refreshAssets = (-not $autoSlot) -or $autoSlot.EndsWith("-afterclose") -or $assetStale
  if ($refreshAssets) {
    & "D:\anaconda3\python.exe" "$proj\scripts\export_asset_injection.py" 2>&1 | Out-Null
    $assetExit = $LASTEXITCODE
    if ($assetExit -ne 0 -or -not (Test-PlacementJson $assetPath "asset")) {
      if ($autoSlot) { [void](Set-AutoRefreshState $placementAutoFile $autoSlot "error" "$reason asset-export-exit-$assetExit") }
      Write-RefreshStatus "error" "定增样本刷新失败，共享数据保留上一版" "placement_events"
      Write-Host "[watch] export_asset_injection exit $assetExit" -ForegroundColor Red
      return $false
    }
  } elseif (-not (Test-PlacementJson $assetPath "asset")) {
    if ($autoSlot) { [void](Set-AutoRefreshState $placementAutoFile $autoSlot "error" "$reason invalid-local-asset") }
    Write-RefreshStatus "error" "本地定增样本无效，共享数据保留上一版" "placement_events"
    return $false
  }

  $placementCandidate = Join-Path $proj "data\.cninfo_placement.$PID.tmp.json"
  Remove-Item $placementCandidate -Force -ErrorAction SilentlyContinue
  & "D:\anaconda3\python.exe" "$proj\scripts\export_placement_events.py" --data-dir "$proj\data" --output $placementCandidate --timeout 20 --sleep 0.2 2>&1 | Out-Null
  $placementExit = $LASTEXITCODE
  if ($placementExit -ne 0 -or -not (Test-PlacementJson $placementCandidate "lifecycle")) {
    Remove-Item $placementCandidate -Force -ErrorAction SilentlyContinue
    if ($autoSlot) { [void](Set-AutoRefreshState $placementAutoFile $autoSlot "error" "$reason lifecycle-export-exit-$placementExit") }
    Write-RefreshStatus "error" "定增生命周期刷新不完整，共享数据保留上一版" "placement_events"
    Write-Host "[watch] export_placement_events exit $placementExit" -ForegroundColor Red
    return $false
  }
  try {
    Move-Item $placementCandidate $placementPath -Force -ErrorAction Stop
  } catch {
    Remove-Item $placementCandidate -Force -ErrorAction SilentlyContinue
    if ($autoSlot) { [void](Set-AutoRefreshState $placementAutoFile $autoSlot "error" "$reason local-publish-failed") }
    Write-RefreshStatus "error" "定增生命周期本地发布失败，共享数据保留上一版" "placement_events"
    return $false
  }

  if (-not (Publish-PlacementFileSet $assetPath $placementPath $shared)) {
    if ($autoSlot) { [void](Set-AutoRefreshState $placementAutoFile $autoSlot "error" "$reason shared-publish-failed") }
    Write-RefreshStatus "error" "定增数据同步失败，共享端上一版生命周期仍可用" "placement_events"
    return $false
  }

  if ($autoSlot) { [void](Set-AutoRefreshState $placementAutoFile $autoSlot "done" $reason) }
  Write-RefreshStatus "done" "定增样本及公告生命周期已自动更新" "placement_events"
  return $true
  } finally {
    Release-ProcessLockFile $placementDocumentsLockFile $placementLock
  }
}

function Invoke-PlacementEventsAutoIfDue {
  $now = Get-Date
  if ($now.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) { return }
  $minutes = ($now.Hour * 60) + $now.Minute
  $slot = ""
  if ($minutes -ge (6 * 60) -and $minutes -lt (8 * 60 + 30)) {
    $slot = $now.ToString("yyyy-MM-dd") + "-preopen"
  } elseif ($minutes -ge (18 * 60 + 30)) {
    $slot = $now.ToString("yyyy-MM-dd") + "-afterclose"
  }
  if (-not $slot) { return }

  $state = Read-AutoRefreshState $placementAutoFile
  $lastSuccessSlot = ""
  if ($state) {
    $lastSuccessSlot = if ($state.last_success_slot) { [string]$state.last_success_slot } else { [string]$state.last_slot }
  }
  if ($lastSuccessSlot -ne $slot -and (Test-AutoRefreshRetryReady $state)) {
    [void](Invoke-PlacementEventsRefresh "scheduled-$slot" $slot)
  }
}

function Invoke-EarningsTimesIncremental($reason, $autoSlot = "") {
  if ($autoSlot) { [void](Set-AutoRefreshState $earningsTimesAutoFile $autoSlot "running" $reason) }
  Write-Host "[watch] 增量刷新 巨潮业绩公告时间 ($reason) ..." -ForegroundColor Cyan
  Write-RefreshStatus "running" "同步巨潮业绩公告时间" "earnings_times"
  $localPath = Join-Path $proj "data\cninfo_earnings_announcements.json"
  $sharedPath = Join-Path $shared "cninfo_earnings_announcements.json"

  # The shared file is canonical because event backfill updates it in place.  Hold
  # the common mutex only long enough to seed a stable local baseline; the network
  # exporter must run outside the lock so a slow CNINFO request cannot deadlock all
  # announcement writers.
  $seedLock = Try-AcquireProcessLockFile $earningsAnnouncementsLockFile "watcher-earnings-seed"
  if (-not $seedLock) {
    if ($autoSlot) { [void](Set-AutoRefreshState $earningsTimesAutoFile $autoSlot "error" "$reason lock-busy-before-export") }
    Write-RefreshStatus "error" "巨潮业绩公告时间写锁忙，共享快照保持不变" "earnings_times"
    Write-Host "[watch] earnings announcement lock busy before export" -ForegroundColor Yellow
    return $false
  }
  $sharedBaseline = $null
  $seedReady = $false
  try {
    $sharedBaseline = Get-FileSnapshotFingerprint $sharedPath
    if ($null -ne $sharedBaseline) {
      $seedReady = ($sharedBaseline -eq "missing" -or (Publish-FileAtomic $sharedPath $localPath))
    }
  } finally {
    Release-ProcessLockFile $earningsAnnouncementsLockFile $seedLock
  }
  if (-not $seedReady) {
    if ($autoSlot) { [void](Set-AutoRefreshState $earningsTimesAutoFile $autoSlot "error" "$reason shared-seed-failed") }
    Write-RefreshStatus "error" "巨潮业绩公告时间基线同步失败，共享快照保持不变" "earnings_times"
    return $false
  }

  $beforeWrite = if (Test-Path -LiteralPath $localPath) { (Get-Item -LiteralPath $localPath).LastWriteTimeUtc } else { [datetime]::MinValue }
  & "D:\anaconda3\python.exe" "$proj\scripts\export_earnings_announcement_times.py" 2>&1 | Out-Null
  $exportExit = $LASTEXITCODE
  $freshLocal = ($exportExit -eq 0 -and (Test-Path -LiteralPath $localPath) -and (Get-Item -LiteralPath $localPath).LastWriteTimeUtc -gt $beforeWrite)
  if (-not $freshLocal) {
    if ($autoSlot) { [void](Set-AutoRefreshState $earningsTimesAutoFile $autoSlot "error" "$reason export-exit-$exportExit") }
    Write-RefreshStatus "error" "巨潮业绩公告时间刷新失败，共享快照保持不变" "earnings_times"
    Write-Host "[watch] export_earnings_announcement_times exit $exportExit freshLocal=$freshLocal" -ForegroundColor Red
    return $false
  }

  # Backfill may have completed while the exporter was running.  Reacquire the
  # same mutex and publish only if the canonical snapshot still matches our seed.
  $publishLock = Try-AcquireProcessLockFile $earningsAnnouncementsLockFile "watcher-earnings-publish"
  if (-not $publishLock) {
    if ($autoSlot) { [void](Set-AutoRefreshState $earningsTimesAutoFile $autoSlot "error" "$reason lock-busy-before-publish") }
    Write-RefreshStatus "error" "巨潮业绩公告时间写锁忙，本次本地结果未发布" "earnings_times"
    Write-Host "[watch] earnings announcement lock busy before publish" -ForegroundColor Yellow
    return $false
  }
  $published = $false
  $publishFailure = "publish-failed"
  try {
    $sharedCurrent = Get-FileSnapshotFingerprint $sharedPath
    if ($null -eq $sharedCurrent) {
      $publishFailure = "shared-fingerprint-failed"
    } elseif ($sharedCurrent -ne $sharedBaseline) {
      $publishFailure = "shared-changed-during-export"
      Write-Host "[watch] shared earnings announcements changed during export; keeping newer shared snapshot" -ForegroundColor Yellow
    } else {
      $published = Publish-FileAtomic $localPath $sharedPath
    }
  } finally {
    Release-ProcessLockFile $earningsAnnouncementsLockFile $publishLock
  }
  if ($published) {
    if ($autoSlot) { [void](Set-AutoRefreshState $earningsTimesAutoFile $autoSlot "done" $reason) }
    Write-RefreshStatus "done" "巨潮业绩公告时间已更新" "earnings_times"
    return $true
  }
  if ($autoSlot) { [void](Set-AutoRefreshState $earningsTimesAutoFile $autoSlot "error" "$reason $publishFailure") }
  Write-RefreshStatus "error" "巨潮业绩公告时间共享快照已变化或发布失败，本次结果未覆盖" "earnings_times"
  return $false
}

function Invoke-EarningsTimesAutoIfDue {
  $due = $true
  $state = Read-AutoRefreshState $earningsTimesAutoFile
  try {
    if ($state) {
      $lastText = if ($state.last_success) { [string]$state.last_success } else { [string]$state.last_run }
      $last = [datetime]::Parse($lastText)
      if (((Get-Date) - $last).TotalHours -lt 24) { $due = $false }
    }
  } catch {}
  if ($due -and -not (Test-AutoRefreshRetryReady $state)) { $due = $false }
  if ($due) { [void](Invoke-EarningsTimesIncremental "daily-auto" "earnings-daily") }
}

function Invoke-RollingEarningsBacktest($reason, [int]$waitSeconds = 900) {
  $scriptPath = Join-Path $proj "scripts\backtest_rolling_earnings.py"
  $announcementPath = Join-Path $shared "cninfo_earnings_announcements.json"
  $outputPath = Join-Path $shared "rolling_earnings_backtest_top50.json"
  $beforeWrite = if (Test-Path -LiteralPath $outputPath) { (Get-Item -LiteralPath $outputPath).LastWriteTimeUtc } else { [datetime]::MinValue }
  & "D:\anaconda3\python.exe" $scriptPath --topn 50 --announcement-cache $announcementPath --out $outputPath `
    --lock-file $rollingEarningsLockFile --status-file $rollingEarningsStatusFile --lock-wait-seconds $waitSeconds --reason $reason 2>&1 | Out-Null
  $exitCode = $LASTEXITCODE
  $freshOutput = ($exitCode -eq 0 -and (Test-Path -LiteralPath $outputPath) -and (Get-Item -LiteralPath $outputPath).LastWriteTimeUtc -gt $beforeWrite)
  $statusDone = $false
  try {
    $status = Get-Content -LiteralPath $rollingEarningsStatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $statusDone = ([string]$status.state -eq "done" -and [string]$status.reason -eq [string]$reason)
  } catch {}
  if (-not $freshOutput -or -not $statusDone) {
    Write-Host "[watch] rolling earnings backtest failed exit=$exitCode fresh=$freshOutput statusDone=$statusDone" -ForegroundColor Red
    return $false
  }
  return $true
}

function Start-RollingEarningsBacktestAuto {
  $scriptPath = Join-Path $proj "scripts\backtest_rolling_earnings.py"
  $stdoutPath = Join-Path $proj "data\rolling_earnings_auto.out.log"
  $stderrPath = Join-Path $proj "data\rolling_earnings_auto.err.log"
  $announcementPath = Join-Path $shared "cninfo_earnings_announcements.json"
  $outputPath = Join-Path $shared "rolling_earnings_backtest_top50.json"
  try {
    if (Test-Path -LiteralPath $rollingEarningsStatusFile) {
      $active = Get-Content -LiteralPath $rollingEarningsStatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
      if ([string]$active.state -eq "running" -and [int]$active.pid -gt 0 -and (Get-Process -Id ([int]$active.pid) -ErrorAction SilentlyContinue)) {
        Write-Host "[watch] rolling earnings backtest already running PID=$($active.pid)" -ForegroundColor DarkCyan
        return [int]$active.pid
      }
    }
  } catch {}
  try {
    $process = Start-Process `
      -FilePath "D:\anaconda3\python.exe" `
      -ArgumentList @($scriptPath, "--topn", "50", "--announcement-cache", $announcementPath, "--out", $outputPath, "--lock-file", $rollingEarningsLockFile, "--status-file", $rollingEarningsStatusFile, "--lock-wait-seconds", "0", "--reason", "auto-event-backfill") `
      -WorkingDirectory $proj `
      -WindowStyle Hidden `
      -RedirectStandardOutput $stdoutPath `
      -RedirectStandardError $stderrPath `
      -PassThru
    return $process.Id
  } catch {
    Write-Host "[watch] failed to start rolling earnings backtest: $($_.Exception.Message)" -ForegroundColor Red
    return 0
  }
}

function Invoke-EarningsEventTimesBackfill($reason) {
  Write-Host "[watch] 事件级补漏 巨潮业绩公告时间 ($reason) ..." -ForegroundColor Cyan
  Write-RefreshStatus "running" "事件级补漏巨潮业绩公告时间" "earnings_event_times"
  $annPath = Get-DataOutput "cninfo_earnings_announcements.json"
  $beforeItems = 0
  try {
    if (Test-Path $annPath) {
      $beforePayload = Get-Content $annPath -Raw -Encoding UTF8 | ConvertFrom-Json
      $beforeItems = @($beforePayload.items).Count
    }
  } catch {}
  $bfWorkers = 5
  $bfLimit = 100
  $bfSleep = 0.8
  $bfMax403 = 3
  try {
    if (Test-Path $earningsEventTimesAutoFile) {
      $prev = Get-Content $earningsEventTimesAutoFile -Raw | ConvertFrom-Json
      if ([bool]$prev.aborted) {
        $bfWorkers = 1; $bfLimit = 20; $bfSleep = 1.2; $bfMax403 = 1
      } elseif ([int]$prev.added -ge 200) {
        $bfWorkers = 10; $bfLimit = 200; $bfSleep = 0.6; $bfMax403 = 3
      } elseif ([int]$prev.added -ge 50) {
        $bfWorkers = 5; $bfLimit = 100; $bfSleep = 0.8; $bfMax403 = 3
      }
    }
  } catch {}
  Write-Host "[watch] event backfill params: workers=$bfWorkers limit=$bfLimit sleep=$bfSleep max403=$bfMax403" -ForegroundColor DarkCyan
  & "D:\anaconda3\python.exe" "$proj\scripts\backfill_earnings_event_times.py" --data-dir $shared --workers $bfWorkers --max-pages 2 --sleep $bfSleep --limit $bfLimit --max-403 $bfMax403 --lock-file $earningsAnnouncementsLockFile --lock-wait-seconds 0 2>&1 | Out-Null
  $exitCode = $LASTEXITCODE
  $annPath = Join-Path $shared "cninfo_earnings_announcements.json"
  $statusPath = Join-Path $shared "cninfo_earnings_event_backfill_status.json"
  $afterItems = $beforeItems
  $aborted = $false
  try {
    if (Test-Path $annPath) {
      $afterPayload = Get-Content $annPath -Raw -Encoding UTF8 | ConvertFrom-Json
      $afterItems = @($afterPayload.items).Count
    }
    if (Test-Path $statusPath) {
      $st = Get-Content $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
      $aborted = [bool]$st.aborted
    }
  } catch {}
  $added = [int]($afterItems - $beforeItems)
  (@{ last_run = (Get-Date -Format "yyyy-MM-dd HH:mm:ss"); reason = $reason; added = $added; aborted = $aborted; exit_code = $exitCode; workers = $bfWorkers; limit = $bfLimit; sleep = $bfSleep; max_403 = $bfMax403 } | ConvertTo-Json -Compress) | Out-File -FilePath $earningsEventTimesAutoFile -Encoding utf8
  if ($exitCode -ne 0) {
    Write-RefreshStatus "error" "事件级巨潮时间补漏脚本失败" "earnings_event_times"
    return $false
  }
  if ($added -gt 0) {
    $backtestPid = Start-RollingEarningsBacktestAuto
    if ($backtestPid) {
      Write-Host "[watch] 事件级补漏新增 $added 条, 滚动业绩回测已转后台 PID=$backtestPid" -ForegroundColor Cyan
    }
  }
  if ($aborted) {
    Write-RefreshStatus "done" "事件级补漏遇到巨潮403已自动暂停" "earnings_event_times"
  } else {
    $suffix = if ($added -gt 0 -and $backtestPid) { ", 滚动回测后台重算中" } else { "" }
    Write-RefreshStatus "done" "事件级巨潮时间补漏完成, 新增 $added 条$suffix" "earnings_event_times"
  }
  return $true
}

function Invoke-EarningsEventTimesAutoIfDue {
  $due = $true
  try {
    if (Test-Path $earningsEventTimesAutoFile) {
      $s = Get-Content $earningsEventTimesAutoFile -Raw | ConvertFrom-Json
      $last = [datetime]::Parse([string]$s.last_run)
      $waitHours = 4
      if ([bool]$s.aborted) { $waitHours = 12 }
      if (((Get-Date) - $last).TotalHours -lt $waitHours) { $due = $false }
    }
  } catch {}
  if ($due) { [void](Invoke-EarningsEventTimesBackfill "auto-backfill") }
}

# 持锁进程是否仍是“活着的 watcher”。只看 PID 在不在会被 PID 回收坑 (旧 watcher 死后,
# 系统把同一 PID 分给别的程序如 lenovo_tool, 误判“锁还有效” -> 谁也抢不到锁, 全部死锁)。
# 所以额外要求: 该 PID 确实是命令行里带 watch_predict 的 powershell/pwsh 进程。
function Test-WatcherAlive([int]$procId) {
  if ($procId -le 0) { return $false }
  try {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction Stop
    if (-not $p) { return $false }
    if ($p.Name -ne 'powershell.exe' -and $p.Name -ne 'pwsh.exe') { return $false }
    return ([string]$p.CommandLine -match 'watch_predict')
  } catch { return $false }
}

# watcher 首次建锁 / 抢占了死掉的持锁者 -> 说明此刻没有任何活着的实例在处理请求,
# 那么任何卡在 "running" 的状态文件都是上次被中断(进程被杀/PC睡眠)留下的孤儿,
# 复位成 error, 避免网页永远显示"处理中"。(只在拿到锁的瞬间调, 不会误伤自己正在跑的任务)
function Reset-StaleRunning {
  $files = @(
    $statusFile, $rdStatusFile, $inclStatusFile, $refreshStatusFile, $thesisStatusFile,
    $predA158StatusFile, $poolStatusFile, $arenaStatusFile, $uarenaStatusFile,
    $barenaStatusFile, $fcompStatusFile, $batchPredStatusFile
  )
  foreach ($sf in $files) {
    try {
      if (Test-Path $sf) {
        $s = Get-Content $sf -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($s.state -eq 'running') {
          (@{ state = 'error'; msg = '上次任务被中断(watcher 重启), 状态已自动复位'; updated_at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') } | ConvertTo-Json -Compress) | Out-File -FilePath $sf -Encoding utf8
          Write-Host "[watch] reset stale running status: $([System.IO.Path]::GetFileName($sf))" -ForegroundColor DarkYellow
        }
      }
    } catch {}
  }
}

if (-not (Test-Path $shared)) {
  Write-Host "[watch] shared dir not reachable: $shared" -ForegroundColor Red
  Write-Host "        tried: $($sharedCandidates -join ' | ')"
  Write-Host "        check NAS login/mapped Z: drive, or set `$env:SHARED_DIR."
  exit 1
}
if (-not (Test-Path $env:STOCK_META_DB)) {
  Write-Host "[watch] missing stock_meta.db at $($env:STOCK_META_DB) (build it, see run_predict_pc.ps1)" -ForegroundColor Yellow
  exit 1
}

Write-Host "[watch] watching $reqFile  (every 15s, Ctrl+C to stop)" -ForegroundColor Cyan
Write-Status "idle" "waiting"
Set-Location $proj
$lockFile = Join-Path $shared "watcher.lock"

while ($true) {
  # 全局锁: 多个 watcher 实例时只有持锁者处理请求, 避免重复处理同一请求/打架; 持锁进程死了则抢占
  $haveLock = $false
  try {
    $fs = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $bb = [System.Text.Encoding]::ASCII.GetBytes("$PID"); $fs.Write($bb, 0, $bb.Length); $fs.Close(); $haveLock = $true
    Reset-StaleRunning
  } catch {
    try {
      $owner = 0; [void][int]::TryParse(((Get-Content $lockFile -Raw -ErrorAction Stop) -replace '\s', ''), [ref]$owner)
      if ($owner -eq $PID) { $haveLock = $true }
      elseif (-not (Test-WatcherAlive $owner)) {
        # 持锁者不是活着的 watcher (已死 / 或 PID 被别的程序回收) -> 抢占
        Set-Content -Path $lockFile -Value "$PID" -Encoding ascii -Force; $haveLock = $true
        Reset-StaleRunning
      }
    } catch {}
  }
  if (-not $haveLock) { Start-Sleep -Seconds 15; continue }
  try {
  if (Test-Path $reqFile) {
    $retrain = $false; $update = $false
    try { $r = (Get-Content $reqFile -Raw | ConvertFrom-Json); $retrain = [bool]$r.retrain; $update = [bool]$r.update } catch {}
    $pargs = @()
    if ($update) {
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) {
        $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim()
      }
      if ($env:TUSHARE_TOKEN) { $pargs += "--update" }
      else { Write-Host "[watch] update requested but no TUSHARE_TOKEN (data\.tushare_token), skipping update" -ForegroundColor Yellow }
    }
    if ($retrain) { $pargs += "--train" }
    Write-Host "[watch] request (update=$update retrain=$retrain), running: predict_qlib.py $pargs" -ForegroundColor Yellow
    Write-Status "running" "predicting"
    try {
      python scripts\predict_qlib.py @pargs
      if ($LASTEXITCODE -eq 0) {
        Write-Status "done" "done"; Write-Host "[watch] done" -ForegroundColor Green
      } else {
        Write-Status "error" "exit $LASTEXITCODE"; Write-Host "[watch] failed (exit $LASTEXITCODE)" -ForegroundColor Red
      }
    } catch {
      Write-Status "error" $_.Exception.Message
    }
    Remove-Item $reqFile -Force -ErrorAction SilentlyContinue
  }

  if (Test-Path $rdReqFile) {
    # request flags: mine(因子挖掘) / retrain / batch(因子批次标签) / loop_n(挖掘轮数)
    $rdRetrain = $false   # web 默认快速预测(复用缓存); 缓存不存在时 predict_next_day 自动回退重训
    $rdBatch = ""; $rdMine = $false; $rdLoopN = 5; $rdModelEval = $false; $rdModel = "lgb"; $rdRunAll = $false; $rdGenReports = $false
    $rdFund = $false   # 基本面增强挖矿路(RDAGENT_FUNDAMENTAL=1, prompt含基本面/情绪维度); 不传=老OHLCV路, 零干扰
    $rdUniverse = "csi300"   # 挖矿股票池(默认csi300; csi500/csi1000时挖前set_mine_universe、挖后恢复csi300)
    $rdStrat = $false; $rdHold = 5; $rdTopN = 1; $rdCost = 0.002   # 单票·周频策略回测
    $rdRegimeAdv = $false   # 策略顾问: regime择时 当前推荐+战绩
    $rdRegimeAdvPro = $false # 策略顾问Pro: 增强版(regime+正交选股)
    $rdRequestId = ""; $rdRequestedAt = ""
    $rdReadError = ""
    try {
      $rr = (Get-Content $rdReqFile -Raw | ConvertFrom-Json)
      if ($null -ne $rr.request_id)  { $rdRequestId = [string]$rr.request_id }
      if ($null -ne $rr.requested_at){ $rdRequestedAt = [string]$rr.requested_at }
      if ($null -ne $rr.retrain)    { $rdRetrain = [bool]$rr.retrain }
      if ($null -ne $rr.batch)      { $rdBatch = [string]$rr.batch }
      if ($null -ne $rr.mine)       { $rdMine = [bool]$rr.mine }
      if ($null -ne $rr.fund)       { $rdFund = [bool]$rr.fund }
      if ($null -ne $rr.universe)   { $rdUniverse = [string]$rr.universe }
      if ($null -ne $rr.loop_n)     { $rdLoopN = [int]$rr.loop_n }
      if ($null -ne $rr.model_eval) { $rdModelEval = [bool]$rr.model_eval }
      if ($null -ne $rr.model)      { $rdModel = [string]$rr.model }
      if ($null -ne $rr.run_all)    { $rdRunAll = [bool]$rr.run_all }
      if ($null -ne $rr.gen_reports){ $rdGenReports = [bool]$rr.gen_reports }
      if ($null -ne $rr.strategy_bt){ $rdStrat = [bool]$rr.strategy_bt }
      if ($null -ne $rr.hold_days)  { $rdHold = [int]$rr.hold_days }
      if ($null -ne $rr.topn)       { $rdTopN = [int]$rr.topn }
      if ($null -ne $rr.rt_cost)    { $rdCost = [double]$rr.rt_cost }
      if ($null -ne $rr.regime_adv) { $rdRegimeAdv = [bool]$rr.regime_adv }
      if ($null -ne $rr.regime_adv_pro) { $rdRegimeAdvPro = [bool]$rr.regime_adv_pro }
    } catch { $rdReadError = "JSON无法解析或字段类型错误" }

    $script:rdStatusRequestId = $rdRequestId
    $script:rdStatusRequestedAt = $rdRequestedAt
    $script:rdStatusAttemptId = ""

    $rdValidationError = $rdReadError
    if (-not $rdValidationError -and -not (Test-SafeRequestLabel $rdBatch -AllowEmpty)) {
      $rdValidationError = "batch 只允许Unicode字母数字、空格和 ._:-"
    } elseif (-not $rdValidationError -and -not (Test-AllowedRdagentModel $rdModel -AllowEmpty)) {
      $rdValidationError = "model 不在允许列表"
    } elseif (-not $rdValidationError -and -not (Test-AllowedRdagentUniverse $rdUniverse)) {
      $rdValidationError = "universe 不在允许列表"
    } elseif (-not $rdValidationError -and ($rdLoopN -lt 1 -or $rdLoopN -gt 50)) {
      $rdValidationError = "loop_n 必须在1到50之间"
    } elseif (-not $rdValidationError -and ($rdHold -lt 1 -or $rdHold -gt 252)) {
      $rdValidationError = "hold_days 必须在1到252之间"
    } elseif (-not $rdValidationError -and ($rdTopN -lt 1 -or $rdTopN -gt 1000)) {
      $rdValidationError = "topn 必须在1到1000之间"
    } elseif (-not $rdValidationError -and ([double]::IsNaN($rdCost) -or [double]::IsInfinity($rdCost) -or $rdCost -lt 0 -or $rdCost -gt 1)) {
      $rdValidationError = "rt_cost 必须是0到1之间的有限数"
    }
    if ($rdValidationError) {
      Reject-WatcherRequest $rdReqFile $rdStatusFile $rdValidationError
      continue
    }
    $rdModel = if ($rdModel) { $rdModel.ToLowerInvariant() } else { "lgb" }
    $rdUniverse = $rdUniverse.ToLowerInvariant()

    # ===== 策略顾问Pro: 增强版(regime+正交选股), 写 regime_advisor_pro.json =====
    if ($rdRegimeAdvPro) {
      Write-Host "[watch] 策略顾问Pro: 拉最新数据 + 重算增强版..." -ForegroundColor Cyan
      Write-RdStatus "running" "策略顾问Pro: 同步行情 + 重算 regime + 正交选股篮子 (~1-2分钟)"
      robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      Write-RdStatus "running" "策略顾问Pro: 刷新沪深300最新成分缓存"
      Refresh-Csi300MembersCache
      & "D:\anaconda3\python.exe" "C:\rdagent\regime_advisor_pro.py"
      $advExit = $LASTEXITCODE
      if ($advExit -eq 0 -and (Test-Path "C:\rdagent\regime_advisor_pro.json")) {
        Copy-Item "C:\rdagent\regime_advisor_pro.json" (Join-Path $shared "regime_advisor_pro.json") -Force
        # 连带重算组合清单(吸收最新Pro篮子作主300腿), 拷NAS
        Write-RdStatus "running" "策略顾问Pro已更新, 重算组合清单(主300腿)..."
        & "D:\anaconda3\python.exe" "$proj\scripts\export_combo_holdings.py"
        if (Test-Path "$proj\data\combo_holdings.json") { Copy-Item "$proj\data\combo_holdings.json" (Join-Path $shared "combo_holdings.json") -Force }
        Write-RdStatus "done" "策略顾问Pro + 组合清单(主300)已更新"
        Write-Host "[watch] 策略顾问Pro + combo完成" -ForegroundColor Green
      } else {
        Write-RdStatus "error" "策略顾问Pro失败 exit $advExit (检查 regime_advisor_pro.py)"
        Write-Host "[watch] 策略顾问Pro失败 exit $advExit" -ForegroundColor Red
      }
      Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
      continue
    }

    # ===== 策略顾问: regime择时 当前推荐篮子+战绩, 拉最新数据重算, 写 regime_advisor.json =====
    if ($rdRegimeAdv) {
      Write-Host "[watch] 策略顾问: 拉最新数据 + 重算 regime/篮子..." -ForegroundColor Cyan
      Write-RdStatus "running" "策略顾问: 同步行情 + 拉最新成分/估值 + 重算当前 regime (~1分钟)"
      robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      & "D:\anaconda3\python.exe" "C:\rdagent\regime_advisor.py"
      $advExit = $LASTEXITCODE
      if ($advExit -eq 0 -and (Test-Path "C:\rdagent\regime_advisor.json")) {
        Copy-Item "C:\rdagent\regime_advisor.json" (Join-Path $shared "regime_advisor.json") -Force
        if (Test-Path "C:\rdagent\regime_advisor_history.json") { Copy-Item "C:\rdagent\regime_advisor_history.json" (Join-Path $shared "regime_advisor_history.json") -Force }
        Write-RdStatus "done" "策略顾问已更新: 见 策略顾问 页"
        Write-Host "[watch] 策略顾问完成" -ForegroundColor Green
      } else {
        Write-RdStatus "error" "策略顾问失败 exit $advExit (检查 regime_advisor.py)"
        Write-Host "[watch] 策略顾问失败 exit $advExit" -ForegroundColor Red
      }
      Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
      continue
    }

    # ===== 单票·周频策略回测: top-N 单票, 每 HOLD 天换仓, 结果写 strategy_result.json =====
    if ($rdStrat) {
      Write-Host "[watch] 策略回测: $rdModel batch='$rdBatch' hold=$rdHold topN=$rdTopN..." -ForegroundColor Cyan
      Write-RdStatus "running" "策略回测: $rdModel [batch=$rdBatch] top$rdTopN/$rdHold日换仓, 训练+模拟中 (~几分钟)"
      wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && RDAGENT_MODEL='$rdModel' RDAGENT_FACTOR_BATCH='$rdBatch' HOLD_DAYS=$rdHold TOPN=$rdTopN RT_COST=$rdCost python backtest_top1_weekly.py"
      $stExit = $LASTEXITCODE
      if ($stExit -eq 0 -and (Test-Path "C:\rdagent\strategy_result.json")) {
        Copy-Item "C:\rdagent\strategy_result.json" (Join-Path $shared "strategy_result.json") -Force
        Write-RdStatus "done" "策略回测完成: $rdModel top$rdTopN/$rdHold日, 见 单票策略 页"
        Write-Host "[watch] 策略回测完成" -ForegroundColor Green
      } else {
        Write-RdStatus "error" "策略回测失败 exit $stExit (检查 backtest_top1_weekly.py)"
        Write-Host "[watch] 策略回测失败 exit $stExit" -ForegroundColor Red
      }
      Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
      continue
    }

    # ===== 一键全跑: 所有模型 训练+回测 + 各出买入清单 (供对比). 同步一次数据后循环。 =====
    if ($rdRunAll) {
      $models = @("lgb","xgb","catboost","ols","ridge","lasso")
      Write-Host "[watch] RUN ALL on batch '$rdBatch'..." -ForegroundColor Cyan
      Write-RdStatus "running" "一键全跑: 同步数据 (robocopy Z->C)"
      robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      Write-RdStatus "running" "一键全跑: 重建 csi300 universe"
      Push-Location "C:\rdagent"; python build_csi300.py; Pop-Location
      $n = $models.Count; $i = 0; $failed = @()
      foreach ($m in $models) {
        $i++
        Write-RdStatus "running" "一键全跑 ($i/$n): $m 训练+回测"
        wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_MODEL='$m' RDAGENT_FACTOR_BATCH='$rdBatch' python run_model.py > /mnt/c/rdagent/run_model_${m}.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
        # 训练+回测失败 -> 记录并跳过该模型 (不要闷头继续, 否则最后假报 done). 见 run_model.py 回测边界等。
        if ($LASTEXITCODE -ne 0) {
          $failed += $m
          Write-Host "[watch] run all: $m 训练+回测失败 exit $LASTEXITCODE, 跳过该模型" -ForegroundColor Red
          Write-RdStatus "running" "一键全跑 ($i/$n): $m 训练+回测失败(exit $LASTEXITCODE), 跳过"
          continue
        }
        if (Test-Path "C:\rdagent\model_results.json") { Copy-Item "C:\rdagent\model_results.json" (Join-Path $shared "model_results.json") -Force }
        if (Test-Path "C:\rdagent\model_runs_history.json") { Copy-Item "C:\rdagent\model_runs_history.json" (Join-Path $shared "model_runs_history.json") -Force }
        if (Test-Path "C:\rdagent\model_curves.json") { Copy-Item "C:\rdagent\model_curves.json" (Join-Path $shared "model_curves.json") -Force }
        Write-RdStatus "running" "一键全跑 ($i/$n): $m 预测买入清单"
        wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_RETRAIN=1 RDAGENT_MODEL='$m' RDAGENT_FACTOR_BATCH='$rdBatch' python predict_next_day.py > /mnt/c/rdagent/predict_next_day_${m}.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
        if ($LASTEXITCODE -eq 0) {
          Push-Location "C:\rdagent"
          python post_process.py
          $env:RDAGENT_TAG_BUYLIST = "1"; $env:RDAGENT_MODEL = $m; $env:RDAGENT_FACTOR_BATCH = $rdBatch
          python export_rdagent.py
          Remove-Item Env:\RDAGENT_TAG_BUYLIST -ErrorAction SilentlyContinue
          Pop-Location
        } else {
          Write-Host "[watch] run all: $m 预测买入清单失败 exit $LASTEXITCODE (回测结果已出, 仅清单缺)" -ForegroundColor Yellow
        }
      }
      # gen_reports: 一键全跑完, 汇总xgb+catboost清单, 缺失/久远研报自动排队生成(alphagen_listener接力)
      if ($rdGenReports) {
        Write-RdStatus "running" "一键全跑完: 汇总xgb+catboost清单, 触发研报+行业瓶颈链生成"
        Write-Host "[watch] gen_reports: 汇总xgb+catboost + 触发研报..." -ForegroundColor Cyan
        try { & "D:\anaconda3\python.exe" "C:\rdagent\agg_buylist_gen_reports.py" $rdBatch 2>&1 | ForEach-Object { Write-Host "  $_" } } catch { Write-Host "[watch] gen_reports失败: $_" -ForegroundColor Red }
        try { & "D:\anaconda3\python.exe" "C:\rdagent\gen_thesis_for_buylist.py" $rdBatch 2>&1 | ForEach-Object { Write-Host "  $_" } } catch { Write-Host "[watch] gen_thesis失败: $_" -ForegroundColor Red }
      }
      $proMsg = ""
      if ($failed.Count -lt $n) {
        Write-RdStatus "running" "一键全跑完: 重算策略顾问Pro篮子"
        Write-Host "[watch] run all: refresh advisor pro..." -ForegroundColor Cyan
        Refresh-Csi300MembersCache
        & "D:\anaconda3\python.exe" "C:\rdagent\regime_advisor_pro.py"
        $advExit = $LASTEXITCODE
        if ($advExit -eq 0 -and (Test-Path "C:\rdagent\regime_advisor_pro.json")) {
          Copy-Item "C:\rdagent\regime_advisor_pro.json" (Join-Path $shared "regime_advisor_pro.json") -Force
          & "D:\anaconda3\python.exe" "$proj\scripts\export_combo_holdings.py"
          if (Test-Path "$proj\data\combo_holdings.json") {
            Copy-Item "$proj\data\combo_holdings.json" (Join-Path $shared "combo_holdings.json") -Force
          }
          $proMsg = ", 顾问Pro已更新"
        } else {
          $proMsg = ", 顾问Pro刷新失败(exit $advExit)"
          Write-Host "[watch] run all: advisor pro failed exit $advExit" -ForegroundColor Yellow
        }
      }
      # 收尾按成败分级: 全成功=done; 部分成功=done但点明失败项; 全失败=error (不再假报完成)
      if ($failed.Count -eq 0) {
        Write-RdStatus "done" "一键全跑完成: $n 个模型已回测+出清单$(if($rdGenReports){', 研报已排队'})$proMsg"
        Write-Host "[watch] run all done" -ForegroundColor Green
      } elseif ($failed.Count -lt $n) {
        $okN = $n - $failed.Count
        Write-RdStatus "done" "一键全跑部分完成: $okN/$n 成功, 失败=$($failed -join ',')$proMsg (见 watcher 窗口日志)"
        Write-Host "[watch] run all partial: failed=$($failed -join ',')" -ForegroundColor Yellow
      } else {
        Write-RdStatus "error" "一键全跑失败: 全部 $n 个模型训练+回测失败, 检查 run_model.py / 数据 (无结果产出)"
        Write-Host "[watch] run all FAILED: 全部模型失败" -ForegroundColor Red
      }
      Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
      continue
    }

    # ===== 模型实验室: 训练指定模型 + 回测, 结果写 model_results.json (供网页对比) =====
    if ($rdModelEval) {
      Write-Host "[watch] model eval: $rdModel on batch '$rdBatch'..." -ForegroundColor Cyan
      Write-RdStatus "running" "model eval: $rdModel [batch=$rdBatch] 训练+回测中 (~几分钟)"
      wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_MODEL='$rdModel' RDAGENT_FACTOR_BATCH='$rdBatch' python run_model.py ) 9>/mnt/c/rdagent/.gpu_train.lock"
      $meExit = $LASTEXITCODE
      if (Test-Path "C:\rdagent\model_results.json") { Copy-Item "C:\rdagent\model_results.json" (Join-Path $shared "model_results.json") -Force }
      if (Test-Path "C:\rdagent\model_runs_history.json") { Copy-Item "C:\rdagent\model_runs_history.json" (Join-Path $shared "model_runs_history.json") -Force }
      if (Test-Path "C:\rdagent\model_curves.json") { Copy-Item "C:\rdagent\model_curves.json" (Join-Path $shared "model_curves.json") -Force }
      if ($meExit -eq 0) { Write-RdStatus "done" "model eval 完成: $rdModel [batch=$rdBatch]"; Write-Host "[watch] model eval done" -ForegroundColor Green }
      else { Write-RdStatus "error" "model eval $rdModel 失败 exit $meExit" }
      Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
      continue
    }

    # ===== 因子挖掘 (RD-Agent fin_factor 演化循环, 几小时, 烧 LLM). 产出新批次但不动全局 SOTA 指针 =====
    if ($rdMine) {
      $trackName = if ($rdFund) { "基本面增强路" } else { "OHLCV老路" }
      if (Test-RdagentMiningProcess) {
        Write-RdStatus "running" "mine[$trackName]: 已有 fin_factor 进程，等待其结束；未重复启动"
        Write-Host "[watch] mine deferred: an existing rdagent process is still running" -ForegroundColor Yellow
        Start-Sleep -Seconds 15
        continue
      }
      Write-Host "[watch] RD-Agent MINE [$trackName] (loop_n=$rdLoopN): 因子发现 (~几小时)..." -ForegroundColor Magenta
      if (-not (Ensure-DockerReady -WriteStatus { param($State, $Message) Write-RdStatus $State $Message })) {
        Write-RdStatus "error" "Docker 未就绪, 无法挖掘 (已尝试启动 Docker Desktop; 请确认登录用户可访问 docker-users/daemon)"
        Write-Host "[watch] mine aborted: Docker 未运行" -ForegroundColor Red
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      Write-RdStatus "running" "mine: 同步数据 (robocopy Z->C)"
      robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
      $mineSyncExit = $LASTEXITCODE
      if ($mineSyncExit -ge 8) {
        Write-RdStatus "error" "mine[$trackName] 终止: Qlib 数据同步失败 (robocopy exit $mineSyncExit)"
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      Write-RdStatus "running" "mine: 重建 csi300 universe"
      $csi300BuildLog = "C:\rdagent\_build_csi300.log"
      $csi300BuildExit = 1
      "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] watcher mine rebuild start" |
        Out-File -FilePath $csi300BuildLog -Encoding utf8
      for ($buildAttempt = 1; $buildAttempt -le 2; $buildAttempt++) {
        "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] attempt $buildAttempt/2" |
          Out-File -FilePath $csi300BuildLog -Encoding utf8 -Append
        Push-Location "C:\rdagent"
        try {
          & "D:\anaconda3\python.exe" -u "C:\rdagent\build_csi300.py" 2>&1 |
            Out-File -FilePath $csi300BuildLog -Encoding utf8 -Append
          $csi300BuildExit = $LASTEXITCODE
        } finally {
          Pop-Location
        }
        if ($csi300BuildExit -eq 0) { break }
        if ($buildAttempt -lt 2) {
          Write-RdStatus "running" "mine: csi300 重建失败, 5秒后自动重试 (1/2)"
          Start-Sleep -Seconds 5
        }
      }
      if ($csi300BuildExit -ne 0) {
        Write-RdStatus "error" "mine[$trackName] 终止: 沪深300历史成分重建失败 (exit $csi300BuildExit); 检查 $csi300BuildLog"
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      Write-RdStatus "running" "mine[$trackName]: 检查模型网关"
      $gateway = Test-RdagentModelGateway
      if (-not $gateway.Ok -and $gateway.RestartRecommended) {
        Write-Host "[watch] local model gateway models endpoint is unavailable; restarting once" -ForegroundColor Yellow
        Write-RdStatus "running" "mine[$trackName]: 本地模型网关不可达，重启并等待就绪 (最多30秒)"
        # TODO: 原脚本在此处关闭作者本机工具，已移除
        # TODO: 原脚本在此处启动作者本机工具，已移除；如需联动请改为自己的程序路径
        if (Wait-RdagentModelGatewayReady -TimeoutSeconds 30) {
          $gateway = Test-RdagentModelGateway
        } else {
          $gateway = [pscustomobject]@{
            Ok = $false
            Stage = "models"
            FailureKind = "startup_timeout"
            RestartRecommended = $false
            Message = "local model gateway did not become ready within 30 seconds"
          }
        }
      }
      if (-not $gateway.Ok) {
        Write-RdStatus "error" "mine[$trackName] 模型网关预检失败: $($gateway.Message)"
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      Write-RdStatus "running" "mine[$trackName]: 检查并同步RD-Agent量价源数据"
      & "D:\anaconda3\python.exe" -u "C:\rdagent\refresh_rdagent_daily_pv.py" 2>&1 | Out-File -FilePath "C:\rdagent\_daily_pv_refresh.log" -Encoding utf8
      if ($LASTEXITCODE -ne 0) {
        Write-RdStatus "error" "mine[$trackName] 量价源数据刷新失败，未启动昂贵挖掘；检查 C:\rdagent\_daily_pv_refresh.log"
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      $preflight = Test-RdagentMiningPreflight -Universe $rdUniverse
      if (-not $preflight.Ok) {
        Write-RdStatus "error" "mine[$trackName] 预检失败: $($preflight.Message)"
        Write-Host "[watch] mine preflight failed: $($preflight.Message)" -ForegroundColor Red
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      Write-Host "[watch] $($preflight.Message)" -ForegroundColor Green
      if (Test-RdagentMiningProcess) {
        Write-RdStatus "running" "mine[$trackName]: 预检期间发现已有 fin_factor，等待其结束；未重复启动"
        Write-Host "[watch] mine deferred after preflight: an existing rdagent process appeared" -ForegroundColor Yellow
        Start-Sleep -Seconds 15
        continue
      }
      # fin_factor (Windows anaconda); 日志写到已知目录以便解析 SOTA
      # 路线×股票池 独立 trace 前缀, 互不覆盖, 留痕可按组合对比。
      # 命名: <minefund|mine>_<universe>_<ts>; 历史脚本据此解析 路线/池 (老的无universe默认csi300)
      $routePrefix = if ($rdFund) { "minefund" } else { "mine" }
      $prefix = "${routePrefix}_$rdUniverse"
      $runStamp = Get-Date -Format yyyyMMdd_HHmmss
      $logPath = "C:\rdagent\log\${prefix}_$runStamp"
      $env:LOG_TRACE_PATH = $logPath
      $mineLog = "C:\rdagent\daily_logs\${prefix}_$runStamp.log"
      if (-not (Test-Path "C:\rdagent\daily_logs")) { New-Item -ItemType Directory -Force "C:\rdagent\daily_logs" | Out-Null }
      $script:rdStatusAttemptId = [guid]::NewGuid().ToString("N")
      $progressLease = "$mineLog.$($script:rdStatusAttemptId).running"
      $mineExit = 1
      $mineLaunchError = ""
      $mineSupervisedAbort = $false
      $mineProcess = $null
      $mineStdoutLog = "$mineLog.stdout.log"
      $progressPublisher = $null
      $mineLocationPushed = $false
      $mineUniverseSwitched = $false
      try {
        New-Item -ItemType File -Path $progressLease -Force -ErrorAction Stop | Out-Null
        Write-RdStatus "running" "mine[$trackName]: rdagent fin_factor loop_n=$rdLoopN (~几小时)"
        Push-Location "C:\rdagent"
        $mineLocationPushed = $true
        $env:CONDA_DEFAULT_ENV = "base"   # RD-Agent 因子代码在本地 conda 环境跑, 读这个变量 (base 有 qlib)
        # 进程级 UTF-8: rich 的 spinner 字符 ⠋(U+280B) 往 GBK 控制台写会崩 UnicodeEncodeError 整个进程挂。
        $env:PYTHONIOENCODING = "utf-8"
        $env:PYTHONUTF8 = "1"
        if ($rdFund) { $env:RDAGENT_FUNDAMENTAL = "1" }
        if ($rdUniverse -ne "csi300") {
          $mineUniverseSwitched = $true
          Write-RdStatus "running" "mine[$trackName]: 切换挖矿股票池 -> $rdUniverse"
          & "D:\anaconda3\python.exe" "C:\rdagent\set_mine_universe.py" $rdUniverse | Out-Null
        }
        # 发布器只获得本 attempt 的日志、租约和身份，不能读取“全局最新日志”。
        if ($script:rdStatusRequestId) { $env:RDAGENT_PROGRESS_REQUEST_ID = $script:rdStatusRequestId }
        if ($script:rdStatusRequestedAt) { $env:RDAGENT_PROGRESS_REQUESTED_AT = $script:rdStatusRequestedAt }
        $env:RDAGENT_PROGRESS_ATTEMPT_ID = $script:rdStatusAttemptId
        $env:RDAGENT_PROGRESS_LOG_PATH = $mineLog
        $env:RDAGENT_PROGRESS_STDOUT_LOG_PATH = $mineStdoutLog
        $env:RDAGENT_PROGRESS_LEASE_PATH = $progressLease
        $env:RDAGENT_PROGRESS_OWNER_PID = [string]$PID
        try {
          $progressPublisher = Start-Process "D:\anaconda3\python.exe" -ArgumentList 'C:\rdagent\_mine_progress_pub.py' -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
        } finally {
          Remove-Item Env:\RDAGENT_PROGRESS_REQUEST_ID -ErrorAction SilentlyContinue
          Remove-Item Env:\RDAGENT_PROGRESS_REQUESTED_AT -ErrorAction SilentlyContinue
          Remove-Item Env:\RDAGENT_PROGRESS_ATTEMPT_ID -ErrorAction SilentlyContinue
          Remove-Item Env:\RDAGENT_PROGRESS_LOG_PATH -ErrorAction SilentlyContinue
          Remove-Item Env:\RDAGENT_PROGRESS_STDOUT_LOG_PATH -ErrorAction SilentlyContinue
          Remove-Item Env:\RDAGENT_PROGRESS_LEASE_PATH -ErrorAction SilentlyContinue
          Remove-Item Env:\RDAGENT_PROGRESS_OWNER_PID -ErrorAction SilentlyContinue
        }
        $mineStartedAtUtc = (Get-Date).ToUniversalTime()
        $mineProcess = Start-Process -FilePath "D:\anaconda3\Scripts\rdagent.exe" `
          -ArgumentList @("fin_factor", "--loop-n", [string]$rdLoopN) `
          -WorkingDirectory "C:\rdagent" -RedirectStandardError $mineLog `
          -RedirectStandardOutput $mineStdoutLog -WindowStyle Hidden -PassThru -ErrorAction Stop
        $pipeFailureSeenAtUtc = $null
        while ($true) {
          $mineProcess.Refresh()
          if ($mineProcess.HasExited) { break }

          $logState = Get-RdagentMiningLogState -Path $mineLog
          if ($logState.PipeFailure -and -not $logState.RecoveredAfterFailure) {
            if ($null -eq $pipeFailureSeenAtUtc) { $pipeFailureSeenAtUtc = (Get-Date).ToUniversalTime() }
            $nowUtc = (Get-Date).ToUniversalTime()
            $latestWriteUtc = Get-RdagentMiningLatestWriteUtc -Paths @($mineLog, $mineStdoutLog)
            $failureAge = ($nowUtc - $pipeFailureSeenAtUtc).TotalSeconds
            $quietAge = if ($latestWriteUtc -eq [datetime]::MinValue) { 0 } else { ($nowUtc - $latestWriteUtc).TotalSeconds }
            if ($failureAge -ge 180 -and $quietAge -ge 180) {
              $identityOk = Test-RdagentMiningAttemptIdentity `
                -ProcessId $mineProcess.Id -WatcherProcessId $PID -LoopN $rdLoopN `
                -StartedAtUtc $mineStartedAtUtc -LeasePath $progressLease -StatusPath $rdStatusFile `
                -RequestId $script:rdStatusRequestId -AttemptId $script:rdStatusAttemptId
              if ($identityOk) {
                $marker = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] [watch-supervisor] abnormal_exit=worker_pipe_assertion_stall pid=$($mineProcess.Id)"
                $marker | Out-File -LiteralPath $mineLog -Encoding utf8 -Append
                Write-RdStatus "running" "mine[$trackName]: 检测到Windows多进程管道崩溃且静默3分钟，已终止本次进程树；保留已完成回测"
                if (Stop-RdagentMiningAttempt -ProcessId $mineProcess.Id) {
                  $mineSupervisedAbort = $true
                  break
                }
              }
            }
          } else {
            $pipeFailureSeenAtUtc = $null
          }
          Start-Sleep -Seconds 15
        }
        if ($mineSupervisedAbort) {
          try { $mineProcess.WaitForExit(30000) } catch {}
          $mineExit = 74
        } else {
          $mineProcess.WaitForExit()
          $mineExit = $mineProcess.ExitCode
        }
      } catch {
        $mineLaunchError = $_.Exception.Message
      } finally {
        Remove-Item -LiteralPath $progressLease -Force -ErrorAction SilentlyContinue
        if ($progressPublisher) {
          for ($waitPublisher = 0; $waitPublisher -lt 40; $waitPublisher++) {
            if (-not (Get-Process -Id $progressPublisher.Id -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 500
          }
          if (Get-Process -Id $progressPublisher.Id -ErrorAction SilentlyContinue) {
            Stop-Process -Id $progressPublisher.Id -Force -ErrorAction SilentlyContinue
          }
        }
        if ($mineUniverseSwitched) {
          & "D:\anaconda3\python.exe" "C:\rdagent\set_mine_universe.py" csi300 | Out-Null
        }
        if ($mineLocationPushed) { Pop-Location }
        Remove-Item Env:\LOG_TRACE_PATH -ErrorAction SilentlyContinue
        Remove-Item Env:\RDAGENT_FUNDAMENTAL -ErrorAction SilentlyContinue
      }
      if ($mineLaunchError) {
        Write-RdStatus "error" "mine[$trackName] 启动失败: $mineLaunchError"
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      # Every route gets a deterministic same-universe orthogonal screen. This separates
      # "no SOTA winner" from "no candidate with incremental signal".
      # Legacy global screen sampled factors from every historical workspace and
      # could not gate the current winner. Keep it only as an explicit diagnostic.
      if ($env:RDAGENT_LEGACY_GLOBAL_SCREEN -eq "1" -and $rdUniverse -in @("csi300", "csi1000")) {
        Write-RdStatus "running" "mine[$trackName]: 跑 $rdUniverse 正交增量检验(resid_ic vs base, 几分钟)"
        $env:RDAGENT_SCREEN_UNIVERSE = $rdUniverse
        $screenLog = if ($rdFund) { "C:\rdagent\_fund_resid.log" } else { "C:\rdagent\_ohlcv_resid.log" }
        & "D:\anaconda3\python.exe" "C:\rdagent\factor_rdagent_screen.py" 60 2>&1 | Out-File -FilePath $screenLog -Encoding utf8
        $screenExit = $LASTEXITCODE
        Remove-Item Env:\RDAGENT_SCREEN_UNIVERSE -ErrorAction SilentlyContinue
        if (Test-Path "C:\rdagent\rdagent_screen.json") {
          $screenName = if ($rdFund) { "fund_resid_screen.json" } elseif ($rdUniverse -eq "csi300") { "rdagent_screen.json" } else { "rdagent_screen_$rdUniverse.json" }
          Copy-Item "C:\rdagent\rdagent_screen.json" (Join-Path $shared $screenName) -Force
          Write-Host "[watch] mine[$trackName]: 正交增量榜 -> $screenName" -ForegroundColor Cyan
          $traceName = Split-Path $logPath -Leaf
          if (-not (Test-Path "C:\rdagent\final")) { New-Item -ItemType Directory -Force "C:\rdagent\final" | Out-Null }
          $archivePrefix = if ($rdFund) { "fund_resid" } else { "ohlcv_resid" }
          Copy-Item "C:\rdagent\rdagent_screen.json" "C:\rdagent\final\${archivePrefix}_$traceName.json" -Force
        } elseif ($screenExit -ne 0) {
          Write-Host "[watch] mine[$trackName]: orthogonal screen failed exit $screenExit" -ForegroundColor Yellow
        }
      }
      if ($rdFund) {
        # 刷新基本面挖掘历史留痕(时间/因子/通过数/结果), 自动拷到 NAS
        & "D:\anaconda3\python.exe" "C:\rdagent\build_fund_mine_history.py" 2>&1 | Out-File -FilePath "C:\rdagent\_fund_hist.log" -Encoding utf8
      }
      # 统一挖矿历史(全 路线×股票池), 供6个挖矿页各按组合过滤留痕对比
      & "D:\anaconda3\python.exe" "C:\rdagent\build_all_mine_history.py" 2>&1 | Out-File -FilePath "C:\rdagent\_all_hist.log" -Encoding utf8
      # fin_factor 即使非零退出(常见: LLM 限流耗尽重试), 已完成的 loop 仍有成果在 trace 里,
      # 所以不直接放弃, 尝试从 session 抢救最优 SOTA。
      if ($mineExit -ne 0) {
        Write-Host "[watch] mine: fin_factor exit $mineExit — 尝试从已完成的 loop 抢救 SOTA" -ForegroundColor Yellow
        Write-RdStatus "running" "mine: fin_factor exit $mineExit, 尝试抢救已完成 loop 的成果"
      }
      Write-RdStatus "running" "mine: 解析新 SOTA workspace"
      $newWs = (& python "C:\rdagent\resolve_sota_ws.py" $logPath | Select-Object -Last 1)
      if (-not $newWs) {
        # A started experiment and a feedback prompt can both happen before a usable
        # result is persisted. Require an actual portfolio metric for success.
        $mineLogPaths = @(@($mineLog, $mineStdoutLog) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
        $hasMineLogs = $mineLogPaths.Count -gt 0
        $ran = $hasMineLogs -and (Select-String -LiteralPath $mineLogPaths -Pattern 'Experiment execution|Start Loop\s+\d+\s*,\s*Step\s+2\s*:\s*running|Combined Results:' -Quiet)
        $evaluated = $hasMineLogs -and (Select-String -LiteralPath $mineLogPaths -Pattern 'Combined Results:|1day\.excess_return_with_cost\.annualized_return' -Quiet)
        $gbk = $hasMineLogs -and (Select-String -LiteralPath $mineLogPaths -Pattern 'UnicodeEncodeError' -Quiet)
        $dataPathError = $hasMineLogs -and (Select-String -LiteralPath $mineLogPaths -Pattern 'does not contain data for day|/C:/qlib_data/cn_data|No result file found|Failed to run this experiment' -Quiet)
        $tokenRejected = $hasMineLogs -and (Select-String -LiteralPath $mineLogPaths -Pattern 'Invalid token|token_rejected' -Quiet)
        $apiTimeout = $hasMineLogs -and (Select-String -LiteralPath $mineLogPaths -Pattern 'APITimeout|Request timed out|Failed to create chat completion after' -Quiet)
        if ($dataPathError) {
          Write-RdStatus "error" "mine[$trackName] Qlib容器数据路径错误, 本轮没有产生回测指标"
        } elseif ($tokenRejected) {
          $saved = if ($rdFund -and (Test-Path (Join-Path $shared "fund_resid_screen.json"))) { "；已保留本次完成的增量检验结果" } else { "" }
          Write-RdStatus "error" "mine[$trackName] 模型 API 凭据无效，任务未完成$saved；本次请求已结束，更新凭据后请重新提交"
        } elseif ($mineExit -ne 0 -and $apiTimeout) {
          Write-RdStatus "error" "mine[$trackName] 模型 API 超时, 任务未完整跑完 (exit $mineExit)"
        } elseif ($mineExit -ne 0) {
          Write-RdStatus "error" "mine[$trackName] 任务中断 (exit $mineExit, 日志 $mineLog)"
        } elseif ($evaluated -and -not $gbk) {
          # No-winner runs are persisted with their completed backtest details.
          Write-RdStatus "done" "mine[$trackName] 跑通: 本轮正常无赢家，已记账（含有效回测明细；fin_factor exit $mineExit）"
          Write-Host "[watch] mine[$trackName]: 跑通无winner（已记账，含有效回测明细）" -ForegroundColor Cyan
        } elseif ($ran -and -not $gbk) {
          Write-RdStatus "error" "mine[$trackName] 回测已启动但没有产生任何指标 (exit $mineExit, 日志 $mineLog)"
        } else {
          Write-RdStatus "error" "mine[$trackName] 真崩: 回测未跑成$(if($gbk){'(GBK编码崩)'}else{''}) (exit $mineExit, 日志 $mineLog)"
        }
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      $newWs = ([string]$newWs).Trim()
      if (-not (Test-SafeWorkspacePath $newWs)) {
        Reject-WatcherRequest $rdReqFile $rdStatusFile "resolve_sota_ws.py 返回了非法workspace路径"
        continue
      }
      # Preserve every LLM-accepted research workspace before selecting one for
      # the expensive production gate.  The trace's final continuation SOTA is
      # not itself authority to replace the production champion.
      $researchTraceName = Split-Path $logPath -Leaf
      if (-not (Test-Path "C:\rdagent\final")) { New-Item -ItemType Directory -Force "C:\rdagent\final" | Out-Null }
      $researchManifestPath = "C:\rdagent\final\research_candidates_$researchTraceName.json"
      & "D:\anaconda3\python.exe" "C:\rdagent\resolve_sota_ws.py" `
        --accepted-manifest $researchManifestPath $logPath
      $researchManifestExit = $LASTEXITCODE
      if ($researchManifestExit -ne 0 -or -not (Test-Path -LiteralPath $researchManifestPath)) {
        Write-RdStatus "error" "mine[$trackName] 无法保存accepted/Pareto研究候选(exit $researchManifestExit), 生产发布已阻止"
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      Write-Host "[watch] mine: 研究延续 workspace = $newWs; accepted/Pareto 已留档" -ForegroundColor Green
      # CSI300 OHLCV runs evaluate the bounded Pareto queue, not merely the LLM's
      # final continuation workspace.  The worker isolates candidate failures,
      # uses stable candidate IDs for cross-trace dedupe, and tournaments every
      # completed three-seed batch on one OOS/cost contract.
      if ($rdUniverse -eq "csi300" -and -not $rdFund) {
        # Keep the first production-grade pass bounded.  Remaining Pareto
        # candidates stay in the resumable queue and can be raised explicitly
        # with RDAGENT_PARETO_MAX_CANDIDATES after reviewing runtime/capacity.
        $paretoMax = 2
        $requestedParetoMax = 0
        if ([int]::TryParse([string]$env:RDAGENT_PARETO_MAX_CANDIDATES, [ref]$requestedParetoMax)) {
          $paretoMax = [math]::Max(1, [math]::Min(8, $requestedParetoMax))
        }
        Write-RdStatus "running" "mine[$trackName]: 逐一评估Pareto候选(上限$paretoMax, exact+FDR+同窗3-seed)"
        $paretoOk = $false
        $paretoError = ""
        try {
          $paretoOutput = & (Join-Path $proj "scripts\evaluate_rdagent_pareto_queue.ps1") `
            -ResearchManifest $researchManifestPath `
            -Universe $rdUniverse `
            -SharedRoot $shared `
            -WorkspaceNasRoot $rdagentWorkspaceNasRoot `
            -MaxCandidates $paretoMax
          $paretoOk = $true
          if ($paretoOutput) { Write-Host ($paretoOutput -join [Environment]::NewLine) }
        } catch {
          $paretoError = $_.Exception.Message
        }
        if (-not $paretoOk) {
          Write-RdStatus "error" "mine[$trackName] Pareto候选队列执行失败($paretoError); 各候选状态已留档, 生产指针未越门变更"
        } else {
          Write-RdStatus "done" "mine[$trackName] 完成: accepted/Pareto已留档并逐一评估(上限$paretoMax); 生产联合门决策已留痕"
        }
        & "D:\anaconda3\python.exe" "C:\rdagent\build_all_mine_history.py" 2>&1 | Out-File -FilePath "C:\rdagent\_all_hist.log" -Encoding utf8
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      if ($rdUniverse -eq "csi500") {
        # There is no independent CSI 500 residual evaluator yet.  Keep the
        # completed research/history, but never turn an FDR-only result into a
        # production batch without the same exact-workspace publication gate.
        Write-RdStatus "done" "mine[$trackName] 研究完成: csi500独立正交/衰减评估器尚未配置, 生产发布禁用(未生成批次)"
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      # Screen only the factor that actually won this request.  This is a hard
      # publication gate: a global historical candidate must never authorize a
      # different workspace's batch.
      if ($rdUniverse -in @("csi300", "csi1000")) {
        Write-RdStatus "running" "mine[$trackName]: 对本轮获胜 workspace 做正交/衰减硬门禁"
        $env:RDAGENT_SCREEN_UNIVERSE = $rdUniverse
        $env:RDAGENT_SCREEN_EXACT_WORKSPACE = $newWs
        $exactScreenLog = if ($rdFund) { "C:\rdagent\_fund_winner_resid.log" } else { "C:\rdagent\_ohlcv_winner_resid.log" }
        try {
          & "D:\anaconda3\python.exe" "C:\rdagent\factor_rdagent_screen.py" 60 2>&1 | Out-File -FilePath $exactScreenLog -Encoding utf8
          $exactScreenExit = $LASTEXITCODE
        } finally {
          Remove-Item Env:\RDAGENT_SCREEN_UNIVERSE -ErrorAction SilentlyContinue
          Remove-Item Env:\RDAGENT_SCREEN_EXACT_WORKSPACE -ErrorAction SilentlyContinue
        }
        if ($exactScreenExit -ne 0 -or -not (Test-Path "C:\rdagent\rdagent_screen.json")) {
          Write-RdStatus "error" "mine[$trackName] 本轮获胜因子正交门禁执行失败(exit $exactScreenExit)"
          Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
          continue
        }
        try {
          $winnerScreen = Get-Content -LiteralPath "C:\rdagent\rdagent_screen.json" -Raw | ConvertFrom-Json
          if ($winnerScreen.scope -ne "exact_workspace" -or
              [string]$winnerScreen.workspace -ne $newWs -or
              [string]$winnerScreen.universe -ne $rdUniverse) {
            throw "screen scope/workspace/universe mismatch"
          }
          $winnerFactorRows = @($winnerScreen.factors)
          $winnerPassedRows = @($winnerFactorRows | Where-Object { $_.pass -eq $true })
          $winnerFactorNames = @($winnerFactorRows | ForEach-Object { [string]$_.factor })
          if ([int]$winnerScreen.screened -ne $winnerFactorRows.Count -or
              [int]$winnerScreen.n_pass -ne $winnerPassedRows.Count -or
              @($winnerFactorNames | Sort-Object -Unique).Count -ne $winnerFactorNames.Count) {
            throw "screen factor rows/counts are inconsistent"
          }
          $winnerScreenName = if ($rdFund) { "fund_resid_screen.json" } elseif ($rdUniverse -eq "csi300") { "rdagent_screen.json" } else { "rdagent_screen_$rdUniverse.json" }
          Copy-Item "C:\rdagent\rdagent_screen.json" (Join-Path $shared $winnerScreenName) -Force
          if (-not (Test-Path "C:\rdagent\final")) { New-Item -ItemType Directory -Force "C:\rdagent\final" | Out-Null }
          $winnerArchivePrefix = if ($rdFund) { "fund_winner_resid" } else { "ohlcv_winner_resid" }
          $winnerTraceName = Split-Path $logPath -Leaf
          Copy-Item "C:\rdagent\rdagent_screen.json" "C:\rdagent\final\${winnerArchivePrefix}_$winnerTraceName.json" -Force
          & "D:\anaconda3\python.exe" "C:\rdagent\build_all_mine_history.py" 2>&1 | Out-File -FilePath "C:\rdagent\_all_hist.log" -Encoding utf8
          if ([int]$winnerScreen.n_pass -lt 1) {
            Write-RdStatus "done" "mine[$trackName] 完成: 本轮winner未通过正交/衰减门禁, 不生成生产批次(正常无赢家)"
            Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
            continue
          }
        } catch {
          Write-RdStatus "error" "mine[$trackName] 本轮正交门禁产物无效: $($_.Exception.Message)"
          Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
          continue
        }
      }
      # 在新 workspace 上评估因子 -> 归档成新批次 (RDAGENT_SOTA_WS_OVERRIDE 不改全局指针/canonical)
      Write-RdStatus "running" "mine: factor_analysis on 新 workspace"
      $batchFilesBefore = @(
        Get-ChildItem "C:\rdagent\final\batches\*.json" -ErrorAction SilentlyContinue |
          ForEach-Object { $_.FullName }
      )
      if ($rdUniverse -in @("csi300", "csi1000")) {
        # factor_analysis validates this artifact again and publishes only the
        # per-factor intersection: selection-split FDR PASS AND exact-screen PASS.
        wsl -e env "RDAGENT_SOTA_WS_OVERRIDE=$newWs" `
          "RDAGENT_FACTOR_EXACT_SCREEN_PATH=C:/rdagent/rdagent_screen.json" `
          "RDAGENT_FACTOR_EXACT_SCREEN_UNIVERSE=$rdUniverse" `
          bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && python factor_analysis.py"
      } else {
        wsl -e env "RDAGENT_SOTA_WS_OVERRIDE=$newWs" bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && python factor_analysis.py"
      }
      $faExit = $LASTEXITCODE
      if ($faExit -eq 3) {
        Write-RdStatus "done" "mine[$trackName] 完成: FDR有效因子与本轮正交/衰减过关因子无交集, 不生成生产批次(正常无赢家)"
        Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
        continue
      } elseif ($faExit -eq 0) {
        $createdBatches = @(
          Get-ChildItem "C:\rdagent\final\batches\*.json" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notin $batchFilesBefore }
        )
        if ($createdBatches.Count -ne 1) {
          Write-RdStatus "error" "factor_analysis 产物异常: 预期1个新批次, 实际$($createdBatches.Count)个"
          Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
          continue
        }
        $newBatchPath = $createdBatches[0]
        $newBatch = $newBatchPath.BaseName
        try {
          Write-RdStatus "running" "mine: 将获胜 workspace 持久化到 Z 盘"
          $persistentWs = Publish-RdagentWorkspace -Value $newWs -NasRoot $rdagentWorkspaceNasRoot
          $manifest = Get-Content -LiteralPath $newBatchPath.FullName -Raw | ConvertFrom-Json
          $manifest.workspace = $persistentWs
          $manifestJson = $manifest | ConvertTo-Json -Depth 12
          [System.IO.File]::WriteAllText(
            $newBatchPath.FullName,
            $manifestJson,
            [System.Text.UTF8Encoding]::new($false)
          )
          Write-Host "[watch] mine: workspace 已持久化 = $persistentWs" -ForegroundColor Green
        } catch {
          Remove-Item -LiteralPath $newBatchPath.FullName -Force -ErrorAction SilentlyContinue
          Write-RdStatus "error" "新因子已筛出, 但 workspace 持久化到 Z 盘失败: $($_.Exception.Message)"
          Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
          continue
        }
        Push-Location "C:\rdagent"; python export_rdagent.py; $exportExit = $LASTEXITCODE; Pop-Location
        if ($exportExit -ne 0) {
          Write-RdStatus "error" "新批次 $newBatch 已持久化, 但网页批次索引导出失败(exit $exportExit)"
          Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
          continue
        }
        # 自动给新批次跑一次 lgb 回测 -> 生成净值曲线 (回测对比页可直接看, 挖一批=自动一条曲线)
        if ($newBatch -and -not (Test-SafeRequestLabel $newBatch)) {
          Reject-WatcherRequest $rdReqFile $rdStatusFile "新批次名不符合安全字符白名单"
          continue
        }
        if ($newBatch -and $rdFund) {   # 标记此批次出自基本面增强路, 供对比/页面区分
          Set-Content -Path "C:\rdagent\final\batches\$newBatch.fundtrack" -Value (Get-Date -Format s) -Encoding utf8
        }
        if ($newBatch) {
          Write-RdStatus "running" "mine: 给新批次 $newBatch 跑 lgb 回测曲线 (~几分钟)"
          wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; SEEDS=0,1,2 RDAGENT_UNIVERSE='$rdUniverse' RDAGENT_MODEL=lgb RDAGENT_FACTOR_BATCH='$newBatch' python run_model.py ) 9>/mnt/c/rdagent/.gpu_train.lock"
          $modelExit = $LASTEXITCODE
          if ($modelExit -ne 0) {
            Write-RdStatus "error" "新批次 $newBatch 已生成并持久化, 但 lgb 回测失败(exit $modelExit); 未发布旧曲线"
            Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
            continue
          }
          if (Test-Path "C:\rdagent\model_curves.json")  { Copy-Item "C:\rdagent\model_curves.json"  (Join-Path $shared "model_curves.json")  -Force }
          if (Test-Path "C:\rdagent\model_results.json") { Copy-Item "C:\rdagent\model_results.json" (Join-Path $shared "model_results.json") -Force }
        }
        # Refresh the incumbent on the exact same OOS window/cost/execution
        # contract, then run a deterministic production tournament.  A missing,
        # stale or unstable metric fails closed while preserving the research batch.
        $productionPromoted = $false
        $productionEligibleShadow = $false
        $promotionBlocked = $false
        $promotionResearchOnly = $rdUniverse -ne "csi300"
        $autoPromotionCommit = ([string]$env:RDAGENT_AUTO_SOTA_PROMOTION).Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")
        $incumbentBatch = ""
        $championStatePath = "C:\rdagent\final\production_champion.json"
        if (-not $promotionResearchOnly -and (Test-Path -LiteralPath $championStatePath)) {
          try {
            $championState = Get-Content -LiteralPath $championStatePath -Raw | ConvertFrom-Json
            $incumbentBatch = [string]$championState.champion.label
            if (-not (Test-SafeRequestLabel $incumbentBatch)) { throw "invalid champion label" }
          } catch {
            Write-Host "[watch] production champion state invalid: $($_.Exception.Message)" -ForegroundColor Yellow
            $promotionBlocked = $true
          }
        }
        if (-not $promotionResearchOnly -and -not $promotionBlocked) {
          $incumbentDisplay = if ($incumbentBatch) { $incumbentBatch } else { "default" }
          Write-RdStatus "running" "mine: 同窗重跑生产冠军 $incumbentDisplay (3 seeds)"
          wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; SEEDS=0,1,2 RDAGENT_UNIVERSE='csi300' RDAGENT_MODEL=lgb RDAGENT_FACTOR_BATCH='$incumbentBatch' python run_model.py ) 9>/mnt/c/rdagent/.gpu_train.lock"
          $incumbentExit = $LASTEXITCODE
          if ($incumbentExit -ne 0) {
            Write-Host "[watch] incumbent refresh failed exit $incumbentExit; production pointer unchanged" -ForegroundColor Yellow
            $promotionBlocked = $true
          }
        }
        if (-not $promotionResearchOnly -and -not $promotionBlocked) {
          $promotionDecisionPath = "C:\rdagent\final\promotion_$newBatch.json"
          $promotionArgs = @(
            "C:\rdagent\promote_production_champion.py",
            "--candidate-batch", $newBatch,
            "--decision-output", $promotionDecisionPath
          )
          if ($autoPromotionCommit) { $promotionArgs += "--commit" }
          & "D:\anaconda3\python.exe" @promotionArgs
          $promotionExit = $LASTEXITCODE
          if ($promotionExit -eq 0) {
            if ($autoPromotionCommit) {
              $productionPromoted = $true
              Write-Host "[watch] production champion promoted -> $newBatch" -ForegroundColor Green
              Copy-Item -LiteralPath $championStatePath -Destination (Join-Path $shared "production_champion.json") -Force
            } else {
              $productionEligibleShadow = $true
              Write-Host "[watch] batch $newBatch 通过生产联合门(shadow); set RDAGENT_AUTO_SOTA_PROMOTION=1 to commit future winners" -ForegroundColor Cyan
            }
          } elseif ($promotionExit -eq 3) {
            Write-Host "[watch] batch $newBatch 未通过生产联合门, production pointer unchanged" -ForegroundColor Cyan
          } else {
            Write-Host "[watch] production tournament invalid exit $promotionExit; pointer unchanged" -ForegroundColor Yellow
            $promotionBlocked = $true
          }
          if (Test-Path "C:\rdagent\model_results.json") { Copy-Item "C:\rdagent\model_results.json" (Join-Path $shared "model_results.json") -Force }
        }
        $promotionTail = if ($productionPromoted) { "; 已晋级生产冠军" } elseif ($productionEligibleShadow) { "; 通过生产联合门(shadow)，生产指针未动" } elseif ($promotionResearchOnly) { "; $rdUniverse 仅保留研究批次，不改csi300生产指针" } elseif ($promotionBlocked) { "; 生产晋级门输入/复验失败，生产指针未动" } else { "; 未通过生产联合门，生产指针未动" }
        $doneMsg = if ($mineExit -eq 0) { "mine[$trackName] 完成: 新研究批次 $newBatch + lgb回测已出$promotionTail" } `
                   else { "mine[$trackName] 部分完成(fin_factor exit $mineExit, 已抢救): 新研究批次 $newBatch + 回测已出$promotionTail" }
        Write-RdStatus "done" $doneMsg
        Write-Host "[watch] mine done (exit=$mineExit), 新批次=$newBatch 回测曲线已出" -ForegroundColor Green
      } else {
        Write-RdStatus "error" "factor_analysis exit $faExit"
      }
      Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
      continue
    }

    $rdMode = if ($rdRetrain) { "1" } else { "0" }
    Write-Host "[watch] RD-Agent request (retrain=$rdRetrain batch='$rdBatch'): sync data + predict..." -ForegroundColor Yellow
    Write-RdStatus "running" "sync data (robocopy Z->C)"
    # 1) Windows robocopy 同步 Z->C (快; WSL rsync 走 /mnt/z 网络盘太慢). 源用 UNC, 不依赖盘符。
    robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
      Write-RdStatus "error" "robocopy failed $LASTEXITCODE"
      Write-Host "[watch] RD-Agent robocopy failed $LASTEXITCODE" -ForegroundColor Red
    }
    else {
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) {
        $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim()
      }
      # 1.5) 重建 csi300 universe (Windows python). 否则成分股 end_date 过时 -> 最新交易日股池缩水
      Write-RdStatus "running" "rebuild csi300 universe"
      Push-Location "C:\rdagent"; python build_csi300.py; $buildUniverseExit = $LASTEXITCODE; Pop-Location
      $predictionPreflight = Test-RdagentPredictionPreflight -Universe $rdUniverse
      if ($buildUniverseExit -ne 0 -or -not $predictionPreflight.Ok) {
        $failure = if ($buildUniverseExit -ne 0) { "build_csi300 exit $buildUniverseExit" } else { $predictionPreflight.Message }
        Write-RdStatus "error" "prediction preflight failed: $failure"
        Write-Host "[watch] RD-Agent prediction preflight failed: $failure" -ForegroundColor Red
      } else {
      Write-Host "[watch] $($predictionPreflight.Message); freshness_basis=$($predictionPreflight.FreshnessBasis)" -ForegroundColor Green
      # 2) predict_next_day 在 WSL(miniconda rdagent env, 有 qlib) 跑, 用 /mnt 路径
      #    RDAGENT_RETRAIN=1 全量重训(~15min); =0 复用缓存模型只预测(快)
      $stepMsg = if ($rdRetrain) { "predict (WSL full retrain)" } else { "predict (WSL no-retrain, cached model)" }
      if ($rdBatch) { $stepMsg += " [batch=$rdBatch]" }
      if ($rdModel -and $rdModel -ne "lgb") { $stepMsg += " [model=$rdModel]" }
      Write-RdStatus "running" $stepMsg
      wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_EXPECTED_MARKET_DATE_BASIS=latest_market_parquet RDAGENT_EXPECTED_MARKET_DATE='$($predictionPreflight.MarketDate)' RDAGENT_UNIVERSE='$rdUniverse' RDAGENT_RETRAIN=$rdMode RDAGENT_FACTOR_BATCH='$rdBatch' RDAGENT_MODEL='$rdModel' python predict_next_day.py > /mnt/c/rdagent/predict_next_day.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
      if ($LASTEXITCODE -ne 0) {
        Write-RdStatus "error" "predict_next_day exit $LASTEXITCODE"
        Write-Host "[watch] RD-Agent predict failed $LASTEXITCODE" -ForegroundColor Red
      } else {
        # 3) post_process + export 在 Windows python 跑 (用 C:/ 路径 + tushare)
        Write-RdStatus "running" "post-process + export (Windows)"
        if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) {
          $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim()
        }
        Push-Location "C:\rdagent"
        python post_process.py
        $pp = $LASTEXITCODE
        # 给本次预测的买入清单打上 模型+批次 标签 (供网页对比)
        $env:RDAGENT_TAG_BUYLIST = "1"; $env:RDAGENT_MODEL = $rdModel; $env:RDAGENT_FACTOR_BATCH = $rdBatch
        python export_rdagent.py
        Remove-Item Env:\RDAGENT_TAG_BUYLIST -ErrorAction SilentlyContinue
        Pop-Location
        if ($pp -eq 0) { Write-RdStatus "done" "done"; Write-Host "[watch] RD-Agent done" -ForegroundColor Green }
        else { Write-RdStatus "error" "post_process exit $pp"; Write-Host "[watch] RD-Agent post_process failed $pp" -ForegroundColor Red }
      }
      }
    }
    Remove-Item $rdReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== TradingAgents 多智能体分析 (对选中股票深度分析, LLM 重型, 每只几分钟) =====
  if (Test-Path $taReqFile) {
    Write-Host "[watch] TradingAgents 分析请求..." -ForegroundColor Cyan
    $env:SHARED_DIR = $shared
    if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
    Push-Location "Z:\claude\tradingagents"
    & "D:\anaconda3\python.exe" run_tradingagents.py
    Pop-Location
    Remove-Item $taReqFile -Force -ErrorAction SilentlyContinue
    Write-Host "[watch] TradingAgents 分析结束" -ForegroundColor Green
  }

  # ===== 因子值抽取 (单股某因子时间序列, 供网页叠加到 K 线; 挖掘因子读 parquet, Alpha158 用 qlib 现算) =====
  if (Test-Path $factorReqFile) {
    Write-Host "[watch] 因子抽取请求..." -ForegroundColor Cyan
    $env:SHARED_DIR = $shared
    & "D:\anaconda3\python.exe" "C:\rdagent\extract_factor.py"
    Remove-Item $factorReqFile -Force -ErrorAction SilentlyContinue
    Write-Host "[watch] 因子抽取结束" -ForegroundColor Green
  }

  # ===== 指数纳入重算: 研究表 + 实盘清单, 产物拷回 csv_tmp 供网页读 (app 从 csv_tmp 读, 非 /app/data) =====
  if (Test-Path $inclReqFile) {
    Write-Host "[watch] 指数纳入重算请求..." -ForegroundColor Cyan
    Write-InclStatus "running" "拉各指数成分 + 算纳入前后收益 (~2-5分钟)"
    if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
    $okR = $false; $okP = $false
    $researchOutput = "$proj\data\index_inclusion.json"
    $researchBefore = if (Test-Path $researchOutput) { (Get-Item $researchOutput).LastWriteTimeUtc } else { [datetime]::MinValue }
    try {
      & "D:\anaconda3\python.exe" scripts\export_index_inclusion.py
      if ($LASTEXITCODE -eq 0 -and (Test-Path $researchOutput) -and (Get-Item $researchOutput).LastWriteTimeUtc -gt $researchBefore) {
        Copy-Item $researchOutput (Join-Path $shared "index_inclusion.json") -Force; $okR = $true
      }
    } catch { Write-Host "[watch] 纳入研究表失败: $_" -ForegroundColor Red }
    Write-InclStatus "running" "研究表完成=$okR, 算实盘清单..."
    $liveOutput = "$proj\data\index_inclusion_pro.json"
    $liveBefore = if (Test-Path $liveOutput) { (Get-Item $liveOutput).LastWriteTimeUtc } else { [datetime]::MinValue }
    try {
      & "D:\anaconda3\python.exe" scripts\export_index_inclusion_pro.py
      if ($LASTEXITCODE -eq 0 -and (Test-Path $liveOutput) -and (Get-Item $liveOutput).LastWriteTimeUtc -gt $liveBefore) {
        Copy-Item $liveOutput (Join-Path $shared "index_inclusion_pro.json") -Force; $okP = $true
      }
    } catch { Write-Host "[watch] 纳入实盘清单失败: $_" -ForegroundColor Red }
    if ($okR -or $okP) {
      $tail = if (-not $okR) { " (研究表失败)" } elseif (-not $okP) { " (实盘清单失败)" } else { "" }
      Write-InclStatus "done" ("纳入数据已更新" + $tail) @{ research_ok = $okR; live_ok = $okP }
      Write-Host "[watch] 指数纳入完成 (研究=$okR 实盘=$okP)" -ForegroundColor Green
    } else {
      Write-InclStatus "error" "纳入重算失败, 检查 export_index_inclusion(_pro).py" @{ research_ok = $okR; live_ok = $okP }
      Write-Host "[watch] 指数纳入全失败" -ForegroundColor Red
    }
    Remove-Item $inclReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== 通用页面刷新: rsrs/ipo/repo/runup, 跑 C:\rdagent 导出脚本拷回 csv_tmp =====
  if (Test-Path $refreshReqFile) {
    $lastLen = -1
    for ($i = 0; $i -lt 10; $i++) {
      try { $curLen = (Get-Item $refreshReqFile -ErrorAction Stop).Length } catch { $curLen = -1 }
      if ($curLen -gt 0 -and $curLen -eq $lastLen) { break }
      $lastLen = $curLen
      Start-Sleep -Milliseconds 200
    }
    $kind = ""
    $refreshRaw = ""
    try {
      $refreshRaw = Get-Content $refreshReqFile -Raw
      $kind = [string](($refreshRaw | ConvertFrom-Json).kind)
    } catch {
      try {
        $m = [regex]::Match([string]$refreshRaw, '"kind"\s*:\s*"([^"]+)"')
        if ($m.Success) { $kind = $m.Groups[1].Value }
      } catch {}
    }
    $kind = ([regex]::Replace([string]$kind, "[^A-Za-z0-9_\-]", "")).Trim().ToLowerInvariant()
    if ($kind -eq "cross_market" -or $kind.Contains("cross_market") -or ($kind -like "*cross*" -and $kind -like "*market*")) {
      [void](Invoke-CrossMarketRefresh "cross_market")
      Remove-Item $refreshReqFile -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 2
      continue
    }
    if ($kind -eq "top_risk" -or $kind.Contains("top_risk")) {
      [void](Invoke-TopRiskRefresh "top_risk")
      Remove-Item $refreshReqFile -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 2
      continue
    }
    if ($kind -eq "money_outflow" -or $kind.Contains("money_outflow")) {
      [void](Invoke-MoneyOutflowRefresh "money_outflow")
      Remove-Item $refreshReqFile -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 2
      continue
    }
    if ($kind -eq "earnings_times") {
      Write-Host "[watch] 刷新 巨潮业绩公告时间 ..." -ForegroundColor Cyan
      Write-RefreshStatus "running" "同步巨潮业绩公告时间" $kind
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      $ok = Invoke-EarningsTimesIncremental "manual-button"
      if ($ok) {
        Write-RefreshStatus "running" "重跑滚动业绩回测" $kind
        $ok = Invoke-RollingEarningsBacktest "manual-earnings-times"
      }
      Remove-Item $refreshReqFile -ErrorAction SilentlyContinue
      if ($ok) { Write-RefreshStatus "done" "巨潮业绩公告时间和滚动业绩回测已更新" $kind; Write-Host "[watch] 巨潮业绩公告时间完成" -ForegroundColor Green }
      else { Write-RefreshStatus "error" "巨潮业绩公告时间刷新失败" $kind; Write-Host "[watch] 巨潮业绩公告时间失败" -ForegroundColor Red }
      Start-Sleep -Seconds 2
      continue
    }
    $map = @{
      rsrs  = @{ s = @("export_rsrs.py"); o = @("rsrs.json") }
      ipo   = @{ s = @("export_ipo.py"); o = @("ipo.json") }
      repo  = @{ s = @("export_repo.py"); o = @("repo.json") }
      runup = @{ s = @("pull_forecast_upcoming.py", "export_runup.py", "export_forecast_browse.py"); o = @("runup.json", "forecast_browse.json") }
      backfill = @{ s = @("verify_and_backfill_qlib.py"); o = @("qlib_coverage.json") }   # 行情数据自检+滞后则全量重建(可能十几分钟)
      avoid = @{ s = @("export_fundamentals.py"); o = @("margin_avoid.json", "fundamentals.json") }   # 毛利率避雷(季频财报数据)手动刷新
    }
    $qproj = @{ industry = @("export_industry.py", "industry.json", "行业基本面"); quality = @("export_quality.py", "quality.json", "质量选股"); sell = @("export_sell_signals.py", "sell_alerts.json", "卖出提醒"); intraday_t = @("export_intraday_t.py", "intraday_t.json", "超短线做T"); hotavoid = @("export_hot_avoid.py", @("hot_avoid.json", "hot_avoid_history.json"), "热榜避雷"); snowball = @("export_snowball.py", @("snowball_avoid.json", "snowball_history.json"), "雪球避雷"); transfer_events = @("export_transfer_events.py", "cninfo_transfer.json", "询价转让/协转解禁"); earnings_times = @("export_earnings_announcement_times.py", "cninfo_earnings_announcements.json", "巨潮业绩公告时间"); cross_market = @("export_cross_market_storage.py", @("cross_market_storage.json", "cross_market_storage_status.json"), "跨市场存储映射") }
    if ($kind -eq "cross_market" -or $kind.Contains("cross_market") -or ($kind -like "*cross*" -and $kind -like "*market*")) {
      [void](Invoke-CrossMarketRefresh $kind)
    }
    elseif ($qproj.ContainsKey($kind)) {
      # quantinvest 自己的脚本(非C:\rdagent)
      $sc = $qproj[$kind][0]; $oj = $qproj[$kind][1]; $lbl = $qproj[$kind][2]
      Write-Host "[watch] 刷新 $lbl ..." -ForegroundColor Cyan
      Write-RefreshStatus "running" "重算 $lbl (~1分钟)" $kind
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      if ($kind -eq "transfer_events") {
        $ec = if (Invoke-TransferEventsIncremental "manual-button") { 0 } else { 1 }
        $okc = ($ec -eq 0)
      } elseif ($kind -eq "earnings_times") {
        $okc = Invoke-EarningsTimesIncremental "manual-qproj"
        if ($okc) {
          Write-RefreshStatus "running" "重跑滚动业绩回测 (~2分钟)" $kind
          $okc = Invoke-RollingEarningsBacktest "manual-qproj-earnings-times"
        }
        $ec = if ($okc) { 0 } else { 1 }
      } else {
        & "D:\anaconda3\python.exe" "$proj\scripts\$sc" 2>&1 | Out-Null
        $ec = $LASTEXITCODE
        $ojs = if ($oj -is [array]) { $oj } else { @($oj) }   # 支持多输出文件(如热榜避雷的清单+历史)
        $okc = $true
        foreach ($o in $ojs) {
          $outPath = Get-DataOutput $o
          if (Test-Path $outPath) { Copy-Item $outPath (Join-Path $shared $o) -Force } else { $okc = $false }
        }
      }
      if ($ec -eq 0 -and $okc) {
        Write-RefreshStatus "done" "$lbl 已更新" $kind; Write-Host "[watch] $lbl 完成" -ForegroundColor Green
      } else { Write-RefreshStatus "error" "$lbl 刷新失败, 检查 $sc" $kind }
    }
    elseif ($map.ContainsKey($kind)) {
      Write-Host "[watch] 刷新 $kind ..." -ForegroundColor Cyan
      Write-RefreshStatus "running" "重算 $kind (~1-2分钟)" $kind
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      $ok = $true
      Push-Location "C:\rdagent"
      foreach ($sc in $map[$kind].s) {
        & "D:\anaconda3\python.exe" "C:\rdagent\$sc" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $ok = $false; Write-Host "[watch] $sc exit $LASTEXITCODE" -ForegroundColor Red }
      }
      Pop-Location
      foreach ($oj in $map[$kind].o) {
        if (Test-Path "C:\rdagent\$oj") { Copy-Item "C:\rdagent\$oj" (Join-Path $shared $oj) -Force } else { $ok = $false }
      }
      if ($kind -eq "runup") {
        Write-RefreshStatus "running" "同步询价转让/协转解禁 (~1分钟)" $kind
        if (-not (Invoke-TransferEventsIncremental "runup-refresh")) { $ok = $false }
        Write-RefreshStatus "running" "同步巨潮业绩公告时间 (~1分钟)" $kind
        if (Invoke-EarningsTimesIncremental "runup-refresh") {
          Write-RefreshStatus "running" "重跑滚动业绩回测 (~2分钟)" $kind
          if (-not (Invoke-RollingEarningsBacktest "manual-runup")) {
            $ok = $false
          }
        } else {
          $ok = $false
        }
      }
      if ($ok) { Write-RefreshStatus "done" "$kind 已更新" $kind; Write-Host "[watch] 刷新 $kind 完成" -ForegroundColor Green }
      else { Write-RefreshStatus "error" "$kind 刷新失败, 检查脚本/日志" $kind; Write-Host "[watch] 刷新 $kind 失败" -ForegroundColor Red }
    }
    elseif ($kind -eq "chipmap") {
      # 海力士映射: 信号(export_korea_semi.py, C:\rdagent)+ 当日分时(export_hynix_intraday.py, quantinvest, 走本地代理推NAS)
      Write-Host "[watch] 刷新 chipmap (信号+分时)..." -ForegroundColor Cyan
      Write-RefreshStatus "running" "重算 海力士映射+分时 (~1分钟)" $kind
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      $ok = $true
      Push-Location "C:\rdagent"
      & "D:\anaconda3\python.exe" "C:\rdagent\export_korea_semi.py" 2>&1 | Out-Null
      if ($LASTEXITCODE -ne 0) { $ok = $false; Write-Host "[watch] export_korea_semi exit $LASTEXITCODE" -ForegroundColor Red }
      Pop-Location
      if (Test-Path "C:\rdagent\korea_semi.json") { Copy-Item "C:\rdagent\korea_semi.json" (Join-Path $shared "korea_semi.json") -Force } else { $ok = $false }
      & "D:\anaconda3\python.exe" "$proj\scripts\export_hynix_intraday.py" 2>&1 | Out-Null   # 分时(自推NAS)
      if ($ok) { Write-RefreshStatus "done" "海力士映射已更新" $kind; Write-Host "[watch] 刷新 chipmap 完成" -ForegroundColor Green }
      else { Write-RefreshStatus "error" "海力士刷新失败, 检查脚本/日志" $kind; Write-Host "[watch] 刷新 chipmap 失败" -ForegroundColor Red }
    } else {
      if ($kind -like "*cross*" -and $kind -like "*market*") {
        [void](Invoke-CrossMarketRefresh $kind)
      } else {
        Write-RefreshStatus "error" "未知刷新类型: $kind" $kind
      }
    }
    Remove-Item $refreshReqFile -Force -ErrorAction SilentlyContinue
  }
  if (-not (Test-Path -LiteralPath $dedicatedRefreshTasksMarker -PathType Leaf)) {
    Invoke-TransferEventsAutoIfDue
    Invoke-PlacementEventsAutoIfDue
    Invoke-EarningsTimesAutoIfDue
  }
  Invoke-EarningsEventTimesAutoIfDue

  # ===== 瓶颈链/卖铲子分析: LLM走框架(:8045)+tushare落地, export_thesis.py 自写状态+拷NAS =====
  if (Test-Path $thesisReqFile) {
    $theme = ""
    $themeReadError = ""
    try { $theme = [string]((Get-Content $thesisReqFile -Raw -Encoding UTF8 | ConvertFrom-Json).theme) } catch { $themeReadError = "JSON无法解析" }
    if ($themeReadError) {
      Reject-WatcherRequest $thesisReqFile $thesisStatusFile $themeReadError
    } elseif (-not (Test-SafeRequestLabel $theme -MaxLength 40)) {
      Reject-WatcherRequest $thesisReqFile $thesisStatusFile "theme 只允许Unicode字母数字、空格和 ._:-，且最长40字符"
    } else {
      Write-Host "[watch] 瓶颈链分析: $theme ..." -ForegroundColor Cyan
      $env:SHARED_DIR = $shared
      $env:THESIS_THEME = $theme   # 中文theme走env(Windows UTF-16原生)传python, 不走argv(ANSI易乱)
      if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) { $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim() }
      & "D:\anaconda3\python.exe" "$proj\scripts\export_thesis.py" "$theme"
      Remove-Item Env:\THESIS_THEME -ErrorAction SilentlyContinue
      if ($LASTEXITCODE -eq 0) { Write-Host "[watch] 瓶颈链 $theme 完成" -ForegroundColor Green }
      else { Write-Host "[watch] 瓶颈链 $theme 失败 exit $LASTEXITCODE" -ForegroundColor Red }
    }
    Remove-Item $thesisReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== Alpha158 预测 (全158因子+csi300, predict_next_day.py RDAGENT_ALPHA158=1, 出次日买入清单, 与24因子页并存做对比) =====
  if (Test-Path $predA158ReqFile) {
    $a158model = "xgb"
    $a158ReadError = ""
    try { $a158model = [string]((Get-Content $predA158ReqFile -Raw | ConvertFrom-Json).model) } catch { $a158ReadError = "JSON无法解析" }
    if (-not $a158model) { $a158model = "xgb" }
    $a158model = $a158model.ToLowerInvariant()
    if ($a158ReadError -or -not (Test-AllowedRdagentModel $a158model -AllowAll)) {
      $reason = if ($a158ReadError) { $a158ReadError } else { "model 不在允许列表" }
      Reject-WatcherRequest $predA158ReqFile $predA158StatusFile $reason
      continue
    }
    robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
    $a158SyncExit = $LASTEXITCODE
    Push-Location "C:\rdagent"; python build_csi300.py | Out-Null; $a158BuildExit = $LASTEXITCODE; Pop-Location
    $a158Preflight = Test-RdagentPredictionPreflight -Universe "csi300"
    if ($a158SyncExit -ge 8 -or $a158BuildExit -ne 0 -or -not $a158Preflight.Ok) {
      $failure = if ($a158SyncExit -ge 8) { "robocopy exit $a158SyncExit" } elseif ($a158BuildExit -ne 0) { "build_csi300 exit $a158BuildExit" } else { $a158Preflight.Message }
      (@{ state = "error"; msg = "Alpha158 prediction preflight failed: $failure"; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $predA158StatusFile -Encoding utf8
      Remove-Item $predA158ReqFile -Force -ErrorAction SilentlyContinue
      continue
    }
    $a158list = if ($a158model -eq "all") { @("lgb","xgb","catboost","ols","ridge","lasso","dlinear","patchtst","timesnet","itransformer") } else { @($a158model) }
    $ai = 0; $a158Ok = 0; $a158Failures = @()
    foreach ($mm in $a158list) {
      $ai++
      Write-Host "[watch] Alpha158 预测: $mm ($ai/$($a158list.Count)) ..." -ForegroundColor Cyan
      (@{ state = "running"; msg = "[$ai/$($a158list.Count)] 训练+预测 $mm"; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $predA158StatusFile -Encoding utf8
      $a158RunStartedUtc = [datetime]::UtcNow
      wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_EXPECTED_MARKET_DATE_BASIS=latest_market_parquet RDAGENT_EXPECTED_MARKET_DATE='$($a158Preflight.MarketDate)' RDAGENT_ALPHA158=1 RDAGENT_UNIVERSE=csi300 RDAGENT_MODEL='$mm' RDAGENT_RETRAIN=1 python predict_next_day.py > /mnt/c/rdagent/_a158_web.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
      $a158PredictExit = $LASTEXITCODE
      $predictionPath = "C:\rdagent\predictions_a158.json"
      $scf = "C:\rdagent\a158_scores_$mm.json"
      $predictionArtifact = Test-RdagentPredictionArtifact -Path $predictionPath -Universe "csi300" -Model $mm -MarketDate $a158Preflight.MarketDate -RunStartedUtc $a158RunStartedUtc
      $scoreArtifact = Test-RdagentScoreArtifact -Path $scf -Model $mm -MarketDate $a158Preflight.MarketDate -ExpectedCount $a158Preflight.ExpectedCount -RunStartedUtc $a158RunStartedUtc
      if ($a158PredictExit -eq 0 -and $predictionArtifact.Ok -and $scoreArtifact.Ok) {
        $publishedPrediction = Publish-FileAtomic $predictionPath (Join-Path $shared "predictions_a158.json")
        $publishedScores = Publish-FileAtomic $scf (Join-Path $shared "a158_scores_$mm.json")
        if (-not $publishedPrediction -or -not $publishedScores) {
          $a158Failures += "$mm/publish"
          Write-Host "[watch] Alpha158 publish failed ($mm); previous shared artifacts retained" -ForegroundColor Red
          continue
        }
        $a158Ok++
        Write-Host "[watch] Alpha158 预测完成 ($mm)" -ForegroundColor Green
      } else {
        $why = if ($a158PredictExit -ne 0) { "exit $a158PredictExit" } elseif (-not $predictionArtifact.Ok) { $predictionArtifact.Message } else { $scoreArtifact.Message }
        $a158Failures += "$mm/$why"
        Write-Host "[watch] Alpha158 预测失败 ($mm): $why; previous shared artifacts retained" -ForegroundColor Red
      }
    }
    $a158State = if ($a158Ok -eq $a158list.Count) { "done" } else { "error" }
    $dmsg = "Alpha158 预测安全发布 $a158Ok/$($a158list.Count)；失败不会覆盖上一版"
    (@{ state = $a158State; msg = $dmsg; failures = $a158Failures; market_date = $a158Preflight.MarketDate; freshness_basis = $a158Preflight.FreshnessBasis; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $predA158StatusFile -Encoding utf8
    Remove-Item $predA158ReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== 分池买入清单一键全跑: 某股票池(csi1000/csi500)上所有模型按arena IR降序各出次日买入清单 =====
  if (Test-Path $poolReqFile) {
    $pUniv = "csi1000"; $pModel = ""
    $poolReadError = ""
    try { $pr = Get-Content $poolReqFile -Raw | ConvertFrom-Json
          $pUniv = [string]$pr.universe
          if ($pr.model) { $pModel = ([string]$pr.model).ToLowerInvariant() } } catch { $poolReadError = "JSON无法解析" }
    if (-not $pUniv) { $pUniv = "csi1000" }
    $pUniv = $pUniv.ToLowerInvariant()
    if ($poolReadError -or $pUniv -notin @("csi500", "csi1000") -or -not (Test-AllowedRdagentModel $pModel -AllowAll -AllowEmpty)) {
      $reason = if ($poolReadError) { $poolReadError } elseif ($pUniv -notin @("csi500", "csi1000")) { "universe 不在允许列表" } else { "model 不在允许列表" }
      Reject-WatcherRequest $poolReqFile $poolStatusFile $reason
      continue
    }
    function Write-PoolStatus($s,$m){ (@{ state=$s; msg=$m; universe=$pUniv; updated_at=(Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $poolStatusFile -Encoding utf8 }
    Write-PoolStatus "running" "分池预测[$pUniv]: 同步数据 + 重建universe"
    robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
    $poolSyncExit = $LASTEXITCODE
    Push-Location "C:\rdagent"; python build_csi300.py | Out-Null; Pop-Location
    $poolPreflight = Test-RdagentPredictionPreflight -Universe $pUniv
    if ($poolSyncExit -ge 8 -or -not $poolPreflight.Ok) {
      $failure = if ($poolSyncExit -ge 8) { "robocopy exit $poolSyncExit" } else { $poolPreflight.Message }
      Write-PoolStatus "error" "分池预测预检失败: $failure；未发布旧产物。请重建本机 $pUniv PIT 成分/行情后重试"
      Remove-Item $poolReqFile -Force -ErrorAction SilentlyContinue
      continue
    }
    $models = @()
    try {
      $arena = Get-Content "C:\rdagent\universe_arena.json" -Raw | ConvertFrom-Json
      $models = @($arena | Where-Object { $_.universe -eq $pUniv } | Sort-Object { - [double]$_.ir } | ForEach-Object { [string]$_.model })
    } catch {}
    $models = @($models | ForEach-Object { ([string]$_).ToLowerInvariant() } | Where-Object { Test-AllowedRdagentModel $_ })
    if (-not $models -or $models.Count -eq 0) { $models = @("dlinear","timesnet","ols","lasso","patchtst","itransformer","lgb","catboost","xgb","ridge") }
    if ($pModel -and $pModel -ne "all") { $models = @($pModel) }   # 只跑选中的单个模型, 省时间
    $pi = 0; $poolOk = 0; $poolFailures = @()
    foreach ($mm in $models) {
      $pi++
      Write-PoolStatus "running" "分池预测[$pUniv] ($pi/$($models.Count)): $mm 训练+预测买入清单"
      $poolRunStartedUtc = [datetime]::UtcNow
      wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_EXPECTED_MARKET_DATE_BASIS=latest_market_parquet RDAGENT_EXPECTED_MARKET_DATE='$($poolPreflight.MarketDate)' RDAGENT_ALPHA158=1 RDAGENT_UNIVERSE='$pUniv' RDAGENT_MODEL='$mm' RDAGENT_RETRAIN=1 python predict_next_day.py > /mnt/c/rdagent/_pool_pred.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
      $poolPredictExit = $LASTEXITCODE
      $bf = "C:\rdagent\pool_buy_${pUniv}_${mm}.json"
      $artifact = Test-RdagentPredictionArtifact -Path $bf -Universe $pUniv -Model $mm -MarketDate $poolPreflight.MarketDate -RunStartedUtc $poolRunStartedUtc
      $published = $false
      if ($poolPredictExit -eq 0 -and $artifact.Ok) {
        $published = Publish-FileAtomic $bf (Join-Path $shared "pool_buy_${pUniv}_${mm}.json")
      }
      if ($published) {
        $poolOk++
      } else {
        $why = if ($poolPredictExit -ne 0) { "exit $poolPredictExit" } elseif (-not $artifact.Ok) { $artifact.Message } else { "atomic publish failed" }
        $poolFailures += "$mm/$why"
        Write-Host "[watch] pool prediction not published ($pUniv/$mm): $why" -ForegroundColor Red
      }
    }
    if ($poolOk -eq $models.Count) {
      Write-PoolStatus "done" "分池预测[$pUniv]完成: $poolOk/$($models.Count)个模型安全发布"
    } else {
      Write-PoolStatus "error" "分池预测[$pUniv]仅安全发布 $poolOk/$($models.Count)；失败项保留共享端上一版: $($poolFailures -join ' | ')"
    }
    Remove-Item $poolReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== 🧬基本面批次 vs 基线 次日买入清单对比 + 留痕 (fund_compare_predict.py, 同模型同池只因子集不同) =====
  if (Test-Path $fcompReqFile) {
    $fcBatch = ""; $fcBaseline = ""; $fcModel = "lgb"; $fcUniv = "csi300"
    $fcReadError = ""
    try { $fc = Get-Content $fcompReqFile -Raw | ConvertFrom-Json
          $fcBatch = [string]$fc.batch; $fcBaseline = [string]$fc.baseline
          if ($fc.model) { $fcModel = [string]$fc.model }
          if ($fc.universe) { $fcUniv = [string]$fc.universe } } catch { $fcReadError = "JSON无法解析" }
    $fcModel = $fcModel.ToLowerInvariant()
    $fcUniv = $fcUniv.ToLowerInvariant()
    $fcValidationError = $fcReadError
    if (-not $fcValidationError -and -not $fcBatch) { $fcValidationError = "缺 batch" }
    elseif (-not $fcValidationError -and -not (Test-SafeRequestLabel $fcBatch)) { $fcValidationError = "batch 字符非法" }
    elseif (-not $fcValidationError -and -not (Test-SafeRequestLabel $fcBaseline -AllowEmpty)) { $fcValidationError = "baseline 字符非法" }
    elseif (-not $fcValidationError -and -not (Test-AllowedRdagentModel $fcModel -AllowAll)) { $fcValidationError = "model 不在允许列表" }
    elseif (-not $fcValidationError -and -not (Test-AllowedRdagentUniverse $fcUniv)) { $fcValidationError = "universe 不在允许列表" }
    if ($fcValidationError) {
      Reject-WatcherRequest $fcompReqFile $fcompStatusFile $fcValidationError
      continue
    }
    function Write-FcStatus($s,$m){ (@{ state=$s; msg=$m; batch=$fcBatch; updated_at=(Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $fcompStatusFile -Encoding utf8 }
    if ($fcBatch) {
      $blName = if ($fcBaseline) { $fcBaseline } else { "默认SOTA" }
      $fcModels = if ($fcModel -eq "all") { @("lgb","xgb","catboost","ridge","lasso","ols") } else { @($fcModel) }
      Write-FcStatus "running" "对比[$fcBatch vs $blName]@$fcUniv $($fcModels.Count)个模型: 同步数据+重建universe"
      robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
      $fcSyncExit = $LASTEXITCODE
      Push-Location "C:\rdagent"; python build_csi300.py | Out-Null; Pop-Location
      $fcPreflight = Test-RdagentPredictionPreflight -Universe $fcUniv
      if ($fcSyncExit -ge 8 -or -not $fcPreflight.Ok) {
        $failure = if ($fcSyncExit -ge 8) { "robocopy exit $fcSyncExit" } else { $fcPreflight.Message }
        Write-FcStatus "error" "基本面对比预检失败: $failure；未发布旧产物。请重建本机 $fcUniv PIT 成分/行情后重试"
        Remove-Item $fcompReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      $fcOk = 0; $fcErr = ""
      $fi = 0
      foreach ($fcm in $fcModels) {
        $fi++
        Write-FcStatus "running" "对比[$fcBatch vs $blName]@$fcUniv ($fi/$($fcModels.Count)): $fcm 两侧预测(基线复用缓存, fund重训)"
        (@{ batch=$fcBatch; baseline=$fcBaseline; model=$fcm; universe=$fcUniv } | ConvertTo-Json -Compress) | Out-File -FilePath "C:\rdagent\fund_compare_req.json" -Encoding utf8
        $fcRunStartedUtc = [datetime]::UtcNow
        wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_EXPECTED_MARKET_DATE_BASIS=latest_market_parquet RDAGENT_EXPECTED_MARKET_DATE='$($fcPreflight.MarketDate)' python fund_compare_predict.py > /mnt/c/rdagent/_fund_compare.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
        $fcPredictExit = $LASTEXITCODE
        $fcLatestPath = "C:\rdagent\fund_compare_latest.json"
        $fcFresh = (Test-Path -LiteralPath $fcLatestPath -PathType Leaf) -and ((Get-Item -LiteralPath $fcLatestPath).LastWriteTimeUtc -ge $fcRunStartedUtc.AddSeconds(-2))
        if ($fcPredictExit -eq 0 -and $fcFresh -and (Publish-FileAtomic $fcLatestPath (Join-Path $shared "fund_compare_latest.json"))) {
          if (Test-Path "C:\rdagent\fund_compare_history.json") { [void](Publish-FileAtomic "C:\rdagent\fund_compare_history.json" (Join-Path $shared "fund_compare_history.json")) }
          $fcOk++
        } else {
          $fcErr = if ($fcPredictExit -ne 0) { "exit $fcPredictExit" } elseif (-not $fcFresh) { "stale/missing result" } else { "atomic publish failed" }
          try { $fcErr += ": " + ((Get-Content "C:\rdagent\_fund_compare.log" -Tail 4 -ErrorAction Stop) -join " | ") } catch {}
        }
      }
      if ($fcOk -gt 0) {
        Write-FcStatus "done" "对比完成[$fcBatch vs $blName]@${fcUniv}: $fcOk/$($fcModels.Count)个模型, 去页面看新增/共有/掉出(留痕)"
      } else {
        Write-FcStatus "error" "对比失败: $fcErr"
      }
    }
    Remove-Item $fcompReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== 用某OHLCV批次因子 + 指定股票池(真路B) 全模型预测次日清单 (predict_next_day RDAGENT_FACTOR_BATCH + RDAGENT_UNIVERSE) =====
  if (Test-Path $batchPredReqFile) {
    $bpBatch = ""; $bpUniv = "csi300"; $bpModel = ""
    $bpReadError = ""
    try { $bp = Get-Content $batchPredReqFile -Raw | ConvertFrom-Json
          $bpBatch = [string]$bp.batch; if ($bp.universe) { $bpUniv = [string]$bp.universe }
          if ($bp.model) { $bpModel = ([string]$bp.model).ToLowerInvariant() } } catch { $bpReadError = "JSON无法解析" }
    $bpUniv = $bpUniv.ToLowerInvariant()
    $bpValidationError = $bpReadError
    if (-not $bpValidationError -and -not $bpBatch) { $bpValidationError = "缺 batch" }
    elseif (-not $bpValidationError -and -not (Test-SafeRequestLabel $bpBatch)) { $bpValidationError = "batch 字符非法" }
    elseif (-not $bpValidationError -and -not (Test-AllowedRdagentUniverse $bpUniv)) { $bpValidationError = "universe 不在允许列表" }
    elseif (-not $bpValidationError -and -not (Test-AllowedRdagentModel $bpModel -AllowAll -AllowEmpty)) { $bpValidationError = "model 不在允许列表" }
    if ($bpValidationError) {
      Reject-WatcherRequest $batchPredReqFile $batchPredStatusFile $bpValidationError
      continue
    }
    function Write-BpStatus($s,$m){ (@{ state=$s; msg=$m; batch=$bpBatch; universe=$bpUniv; updated_at=(Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $batchPredStatusFile -Encoding utf8 }
    if ($bpBatch) {
      Write-BpStatus "running" "批次预测[$bpBatch -> $bpUniv]: 同步数据+重建universe"
      robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
      $bpSyncExit = $LASTEXITCODE
      Push-Location "C:\rdagent"; python build_csi300.py | Out-Null; Pop-Location
      $bpPreflight = Test-RdagentPredictionPreflight -Universe $bpUniv
      if ($bpSyncExit -ge 8 -or -not $bpPreflight.Ok) {
        $failure = if ($bpSyncExit -ge 8) { "robocopy exit $bpSyncExit" } else { $bpPreflight.Message }
        Write-BpStatus "error" "批次预测预检失败: $failure；未发布旧产物。请重建本机 $bpUniv PIT 成分/行情后重试"
        Remove-Item $batchPredReqFile -Force -ErrorAction SilentlyContinue
        continue
      }
      $bpModels = @("lgb","xgb","catboost","ols","ridge","lasso","dlinear","timesnet","patchtst","itransformer")
      if ($bpModel -and $bpModel -ne "all") { $bpModels = @($bpModel) }   # 只跑选中单模型(如timesnet, 省时间)
      $bi = 0; $bpOk = 0
      foreach ($bm in $bpModels) {
        $bi++
        Write-BpStatus "running" "批次预测[$bpBatch -> $bpUniv] ($bi/$($bpModels.Count)): $bm 训练+预测"
        $outf = "/mnt/c/rdagent/batch_buy_${bpBatch}_${bpUniv}_${bm}.json"
        # "default"=SOTA: 传空批次让 predict_next_day 走 sota_workspace.txt; 文件名仍用 default
        $bpBatchEnv = if ($bpBatch -eq "default") { "" } else { $bpBatch }
        $bpRunStartedUtc = [datetime]::UtcNow
        wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_EXPECTED_MARKET_DATE_BASIS=latest_market_parquet RDAGENT_EXPECTED_MARKET_DATE='$($bpPreflight.MarketDate)' RDAGENT_FACTOR_BATCH='$bpBatchEnv' RDAGENT_UNIVERSE='$bpUniv' RDAGENT_MODEL='$bm' RDAGENT_RETRAIN=1 RDAGENT_BUYLIST_OUT='$outf' python predict_next_day.py >> /mnt/c/rdagent/_batch_predict.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
        $bpPredictExit = $LASTEXITCODE
        $bf = "C:\rdagent\batch_buy_${bpBatch}_${bpUniv}_${bm}.json"
        $artifact = Test-RdagentPredictionArtifact -Path $bf -Universe $bpUniv -Model $bm -MarketDate $bpPreflight.MarketDate -RunStartedUtc $bpRunStartedUtc
        $published = $false
        if ($bpPredictExit -eq 0 -and $artifact.Ok) {
          $published = Publish-FileAtomic $bf (Join-Path $shared "batch_buy_${bpBatch}_${bpUniv}_${bm}.json")
        }
        if ($published) {
          $bpOk++
        } else {
          $why = if ($bpPredictExit -ne 0) { "exit $bpPredictExit" } elseif (-not $artifact.Ok) { $artifact.Message } else { "atomic publish failed" }
          Write-Host "[watch] batch prediction not published ($bpBatch/$bpUniv/$bm): $why" -ForegroundColor Red
        }
      }
      if ($bpOk -eq $bpModels.Count) {
        Write-BpStatus "done" "批次预测[$bpBatch -> $bpUniv]完成: $bpOk/$($bpModels.Count)个模型安全发布"
      } else {
        Write-BpStatus "error" "批次预测[$bpBatch -> $bpUniv]仅安全发布 $bpOk/$($bpModels.Count)；失败项保留共享端上一版"
      }
    }
    Remove-Item $batchPredReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== Alpha158 模型擂台: 在全158因子+csi300上回测某模型, run_model.py RDAGENT_ALPHA158=1, 入 alpha158_arena.json =====
  if (Test-Path $arenaReqFile) {
    $arModel = "catboost"
    $arReadError = ""
    try { $arModel = [string]((Get-Content $arenaReqFile -Raw | ConvertFrom-Json).model) } catch { $arReadError = "JSON无法解析" }
    if (-not $arModel) { $arModel = "catboost" }
    $arModel = $arModel.ToLowerInvariant()
    if ($arReadError -or -not (Test-AllowedRdagentModel $arModel -AllowAll)) {
      $reason = if ($arReadError) { $arReadError } else { "model 不在允许列表" }
      Reject-WatcherRequest $arenaReqFile $arenaStatusFile $reason
      continue
    }
    $arList = if ($arModel -eq "all") { @("lgb","xgb","catboost","ols","ridge","lasso","dlinear","patchtst","timesnet","itransformer") } else { @($arModel) }
    $ri = 0
    foreach ($mm in $arList) {
      $ri++
      Write-Host "[watch] Alpha158 擂台回测: $mm ($ri/$($arList.Count)) ..." -ForegroundColor Cyan
      (@{ state = "running"; msg = "[$ri/$($arList.Count)] 回测 $mm"; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $arenaStatusFile -Encoding utf8
      wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_ALPHA158=1 RDAGENT_MODEL='$mm' SEEDS=0 python run_model.py > /mnt/c/rdagent/_arena_web.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
      if ($LASTEXITCODE -eq 0 -and (Test-Path "C:\rdagent\alpha158_arena.json")) {
        Copy-Item "C:\rdagent\alpha158_arena.json" (Join-Path $shared "alpha158_arena.json") -Force
        Write-Host "[watch] Alpha158 擂台完成 ($mm)" -ForegroundColor Green
      } else {
        Write-Host "[watch] Alpha158 擂台失败 ($mm) exit $LASTEXITCODE" -ForegroundColor Red
      }
    }
    $dmsg = if ($arModel -eq "all") { "擂台一键全跑完成($($arList.Count)模型)" } else { "擂台回测完成: $arModel" }
    (@{ state = "done"; msg = $dmsg; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $arenaStatusFile -Encoding utf8
    Remove-Item $arenaReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== 股票池擂台: 同模型在 csi300/500/1000/all 上回测, run_model.py RDAGENT_ALPHA158=1 RDAGENT_UNIVERSE=<u> =====
  if (Test-Path $uarenaReqFile) {
    $uUniv = "csi300"; $uModel = "xgb"
    $uReadError = ""
    try { $rr = (Get-Content $uarenaReqFile -Raw | ConvertFrom-Json); $uUniv = [string]$rr.universe; $uModel = [string]$rr.model } catch { $uReadError = "JSON无法解析" }
    if (-not $uUniv) { $uUniv = "csi300" }; if (-not $uModel) { $uModel = "xgb" }
    $uUniv = $uUniv.ToLowerInvariant(); $uModel = $uModel.ToLowerInvariant()
    if ($uReadError -or -not (Test-AllowedRdagentUniverse $uUniv -AllowAll -AllowAllUniverses) -or -not (Test-AllowedRdagentModel $uModel -AllowAll)) {
      $reason = if ($uReadError) { $uReadError } elseif (-not (Test-AllowedRdagentUniverse $uUniv -AllowAll -AllowAllUniverses)) { "universe 不在允许列表" } else { "model 不在允许列表" }
      Reject-WatcherRequest $uarenaReqFile $uarenaStatusFile $reason
      continue
    }
    $uMlist = if ($uModel -eq "all") { @("lgb","xgb","catboost","ols","ridge","lasso","dlinear","patchtst","timesnet","itransformer") } else { @($uModel) }
    $ui = 0
    foreach ($mm in $uMlist) {
      $ui++
      Write-Host "[watch] 股票池擂台: $uUniv / $mm ($ui/$($uMlist.Count)) ..." -ForegroundColor Cyan
      (@{ state = "running"; msg = "[$ui/$($uMlist.Count)] $uUniv 回测 $mm"; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $uarenaStatusFile -Encoding utf8
      wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_ALPHA158=1 RDAGENT_UNIVERSE='$uUniv' RDAGENT_MODEL='$mm' SEEDS=0 python run_model.py > /mnt/c/rdagent/_uarena_web.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
      if ($LASTEXITCODE -eq 0 -and (Test-Path "C:\rdagent\universe_arena.json")) {
        Copy-Item "C:\rdagent\universe_arena.json" (Join-Path $shared "universe_arena.json") -Force
        Write-Host "[watch] 股票池擂台完成 ($uUniv/$mm)" -ForegroundColor Green
      } else { Write-Host "[watch] 股票池擂台失败 ($uUniv/$mm) exit $LASTEXITCODE" -ForegroundColor Red }
    }
    (@{ state = "done"; msg = "股票池回测完成: $uUniv/$uModel"; updated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $uarenaStatusFile -Encoding utf8
    Remove-Item $uarenaReqFile -Force -ErrorAction SilentlyContinue
  }

  # ===== 批次擂台: 用某批次因子(非A158), 在各股票池×各模型回测, 入 batch_arena.json =====
  if (Test-Path $barenaReqFile) {
    $baBatch = ""; $baUniv = "csi300"; $baModel = "all"
    $baReadError = ""
    try { $br = (Get-Content $barenaReqFile -Raw | ConvertFrom-Json); $baBatch = [string]$br.batch
          if ($br.universe) { $baUniv = [string]$br.universe }; if ($br.model) { $baModel = ([string]$br.model).ToLowerInvariant() } } catch { $baReadError = "JSON无法解析" }
    $baUniv = $baUniv.ToLowerInvariant()
    $baValidationError = $baReadError
    if (-not $baValidationError -and -not $baBatch) { $baValidationError = "缺 batch" }
    elseif (-not $baValidationError -and -not (Test-SafeRequestLabel $baBatch)) { $baValidationError = "batch 字符非法" }
    elseif (-not $baValidationError -and -not (Test-AllowedRdagentUniverse $baUniv -AllowAll -AllowAllUniverses)) { $baValidationError = "universe 不在允许列表" }
    elseif (-not $baValidationError -and -not (Test-AllowedRdagentModel $baModel -AllowAll)) { $baValidationError = "model 不在允许列表" }
    if ($baValidationError) {
      Reject-WatcherRequest $barenaReqFile $barenaStatusFile $baValidationError
      continue
    } else {
      $baUlist = if ($baUniv -eq "allunivs") { @("csi300","csi500","csi1000") } else { @($baUniv) }
      $baMlist = if ($baModel -eq "all") { @("lgb","xgb","catboost","ols","ridge","lasso","dlinear","patchtst","timesnet","itransformer") } else { @($baModel) }
      # csi500/1000 需要universe文件: 同步数据+重建
      robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
      Push-Location "C:\rdagent"; python build_csi300.py | Out-Null; Pop-Location
      $bTot = $baUlist.Count * $baMlist.Count; $bi = 0; $baFailed = @()
      foreach ($bu in $baUlist) {
        foreach ($bm in $baMlist) {
          $bi++
          (@{ state="running"; msg="批次擂台[$baBatch] ($bi/$bTot): $bu / $bm 回测"; updated_at=(Get-Date -Format "yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Compress) | Out-File -FilePath $barenaStatusFile -Encoding utf8
          # "default"=SOTA: 传空 RDAGENT_FACTOR_BATCH 让 run_model 走 sota_workspace.txt(15因子); 结果仍记 batch=default
          $baBatchEnv = if ($baBatch -eq "default") { "" } else { $baBatch }
          wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_FACTOR_BATCH='$baBatchEnv' RDAGENT_UNIVERSE='$bu' RDAGENT_MODEL='$bm' SEEDS=0 python run_model.py > /mnt/c/rdagent/_barena_web.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock"
          $baExit = $LASTEXITCODE
          if ($baExit -ne 0) {
            $baFailed += "$bu/$bm(exit $baExit)"
            Write-Host "[watch] batch arena failed: $bu/$bm exit $baExit" -ForegroundColor Red
          }
          if (Test-Path "C:\rdagent\batch_arena.json") { Copy-Item "C:\rdagent\batch_arena.json" (Join-Path $shared "batch_arena.json") -Force }
        }
      }
      if ($baFailed.Count -eq 0) {
        $baFinal = @{ state="done"; msg="批次擂台完成: $baBatch ($bTot组合)"; updated_at=(Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
      } elseif ($baFailed.Count -lt $bTot) {
        $baFinal = @{ state="done"; msg="批次擂台部分完成: $($bTot-$baFailed.Count)/$bTot 成功; 失败=$($baFailed -join ',')"; updated_at=(Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
      } else {
        $baFinal = @{ state="error"; msg="批次擂台失败: 全部 $bTot 个组合失败; $($baFailed -join ',')"; updated_at=(Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
      }
      ($baFinal | ConvertTo-Json -Compress) | Out-File -FilePath $barenaStatusFile -Encoding utf8
    }
    Remove-Item $barenaReqFile -Force -ErrorAction SilentlyContinue
  }
  } finally {
    # 释放锁 (仅当自己持有时才删)
    try { if (((Get-Content $lockFile -Raw -ErrorAction SilentlyContinue) -replace '\s', '') -eq "$PID") { Remove-Item $lockFile -Force -ErrorAction SilentlyContinue } } catch {}
  }
  Start-Sleep -Seconds 15
}
