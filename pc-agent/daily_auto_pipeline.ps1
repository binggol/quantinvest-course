# 每日凌晨自动化: 最新批次 -> 全跑6模型(xgb/catboost优先) -> 汇总xgb+catboost清单 -> 缺失/久远研报自动生成
# 复刻 watch_predict_pc.ps1 的 run_all 链路, 但独立调度(Windows计划任务 每天1:30), 无需网页点击。
# 注册: 见文件末尾注释。日志: C:\rdagent\daily_logs\auto_pipeline_<date>.log
$ErrorActionPreference = "Stop"
$proj     = "C:\path\to\quantinvest-course"  # TODO: 改为你的项目路径
# 计划任务会话无Z:盘符映射 → 用UNC路径访问NAS(Z:映射到 \/app/shared)
$uncBase  = "\/app/qlib_data"
$shared   = if (Test-Path "Z:\claude") { "Z:\claude\qlib\data\csv_tmp" } else { "$uncBase\csv_tmp" }
$qlibData = if (Test-Path "Z:\claude") { "Z:\claude\qlib\data\cn_data" } else { "$uncBase\cn_data" }
$rd       = "C:\rdagent"
$python   = "D:\anaconda3\python.exe"
$logdir   = "C:\rdagent\daily_logs"
$watcherRestartRequest = Join-Path $proj "data\watcher_restart_admin.request.json"
$rdagentRecoveryRequest = Join-Path $proj "data\rdagent_recovery_admin.request.json"
$watcherCommandPattern = '(?i)(?:^|\s)-File\s+"?[^"\r\n]*[\\/]watch_predict_pc\.ps1"?(?:\s|$)'
if (-not (Test-Path $logdir)) { New-Item -ItemType Directory -Path $logdir -Force | Out-Null }
$log = Join-Path $logdir ("auto_pipeline_" + (Get-Date -Format "yyyyMMdd") + ".log")
function Log($m) { $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m"; Write-Host $line; Add-Content -Path $log -Value $line -Encoding utf8 }
function Invoke-NativeCommand([scriptblock]$Command, [switch]$LogOutput) {
  # Windows PowerShell turns a native program's stderr into ErrorRecord objects.
  # With the script-wide Stop preference, harmless pandas/yfinance warnings used
  # to abort this task before LASTEXITCODE could be checked.
  $previousPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $output = @(& $Command 2>&1)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousPreference
  }
  if ($LogOutput) {
    foreach ($line in $output) { Log ([string]$line) }
  }
  return [int]$exitCode
}
function Assert-Directory($path, $label) {
  if (-not (Test-Path -LiteralPath $path -PathType Container)) {
    throw "$label 不可用: $path"
  }
}
function Invoke-QlibMirror {
  Log "同步行情数据 (NAS->C)"
  New-Item -ItemType Directory -Path "C:\qlib_data\cn_data" -Force | Out-Null
  robocopy "$qlibData" "C:\qlib_data\cn_data" /MIR /MT:8 /R:1 /W:2 /XF csi300.txt csi300.txt.bak csi500.txt csi1000.txt /NFL /NDL /NJH /NP | Out-Null
  $copyExit = $LASTEXITCODE
  # Robocopy 0..7 are documented success/informational codes; 8+ is failure.
  if ($copyExit -ge 8) { throw "行情同步失败 (robocopy exit $copyExit)" }
}

function Invoke-StuckRdagentRecovery([string]$RequestPath) {
  $maintenance = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if ([string]$maintenance.operation -ne "recover_stuck_fin_factor") {
    throw "不支持的 RD-Agent 维护操作"
  }

  $expectedWatcherPid = [int]$maintenance.expected_watcher_pid
  $expectedMinerPid = [int]$maintenance.expected_miner_pid
  $requestId = [string]$maintenance.request_id
  $attemptId = [string]$maintenance.attempt_id
  if ($expectedWatcherPid -le 0 -or $expectedMinerPid -le 0) {
    throw "RD-Agent 恢复请求缺少有效 PID"
  }
  if ($requestId -notmatch '^[0-9a-f]{32}$' -or $attemptId -notmatch '^[0-9a-f]{32}$') {
    throw "RD-Agent 恢复请求身份格式无效"
  }

  $actualWatcherPid = [int]((Get-Content -LiteralPath (Join-Path $proj "data\watch_predict_pc.pid") -Raw).Trim())
  if ($actualWatcherPid -ne $expectedWatcherPid) {
    throw "watcher PID 已变化: expected=$expectedWatcherPid actual=$actualWatcherPid"
  }
  $watcherProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$expectedWatcherPid" -ErrorAction Stop
  if (-not $watcherProcess -or [string]$watcherProcess.Name -notmatch '^(?i:powershell|pwsh)\.exe$' -or
      [string]$watcherProcess.CommandLine -notmatch $watcherCommandPattern) {
    throw "expected_watcher_pid 不是当前 watcher 进程"
  }

  $queuedPath = Join-Path $shared "rdagent_request.json"
  $statusPath = Join-Path $shared "rdagent_status.json"
  if (-not (Test-Path -LiteralPath $queuedPath -PathType Leaf) -or
      -not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
    throw "当前 RD-Agent 请求或状态不存在"
  }
  $queued = Get-Content -LiteralPath $queuedPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if ([string]$queued.request_id -ne $requestId -or [string]$status.request_id -ne $requestId -or
      [string]$status.attempt_id -ne $attemptId -or [string]$status.state -ne "running") {
    throw "RD-Agent 请求/状态身份已变化，拒绝恢复"
  }

  $leases = @(Get-ChildItem -LiteralPath $logdir -Filter "*.log.$attemptId.running" -File -ErrorAction Stop)
  if ($leases.Count -ne 1) {
    throw "当前 attempt 的运行租约数量异常: $($leases.Count)"
  }
  $leaseSuffix = ".$attemptId.running"
  if (-not $leases[0].FullName.EndsWith($leaseSuffix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "运行租约路径格式无效"
  }
  $mineLog = $leases[0].FullName.Substring(0, $leases[0].FullName.Length - $leaseSuffix.Length)
  $mineLogItem = Get-Item -LiteralPath $mineLog -ErrorAction Stop
  $staleMinutes = ((Get-Date) - $mineLogItem.LastWriteTime).TotalMinutes
  if ($staleMinutes -lt 90) {
    throw "挖矿日志仅停滞 $([math]::Round($staleMinutes, 1)) 分钟，未达到90分钟人工恢复门槛"
  }

  $miner = Get-CimInstance Win32_Process -Filter "ProcessId=$expectedMinerPid" -ErrorAction Stop
  if (-not $miner -or [string]$miner.Name -ne "rdagent.exe" -or
      [string]$miner.CommandLine -notmatch '(?i)(?:^|\s)fin_factor(?:\s|$)') {
    throw "expected_miner_pid 不是 fin_factor 进程"
  }
  if ([int]$miner.ParentProcessId -ne $expectedWatcherPid) {
    throw "fin_factor 不是 expected_watcher_pid 的直接子进程"
  }
  [datetime]$requestedAt = [datetime]::MinValue
  if (-not [datetime]::TryParse([string]$queued.requested_at, [ref]$requestedAt) -or
      [datetime]$miner.CreationDate -lt $requestedAt.AddMinutes(-5) -or
      [datetime]$miner.CreationDate -gt $requestedAt.AddMinutes(10)) {
    throw "fin_factor 创建时间与当前请求不匹配"
  }
  $activeMiners = @(Get-CimInstance Win32_Process -Filter "Name='rdagent.exe'" -ErrorAction Stop | Where-Object {
    [string]$_.CommandLine -match '(?i)(?:^|\s)fin_factor(?:\s|$)'
  })
  if ($activeMiners.Count -ne 1 -or [int]$activeMiners[0].ProcessId -ne $expectedMinerPid) {
    throw "活动 fin_factor 集合与恢复目标不唯一"
  }

  Log "确认挖矿卡死: request=$requestId attempt=$attemptId PID=$expectedMinerPid 日志停滞=$([math]::Round($staleMinutes, 1))分钟"
  $nativeExit = Invoke-NativeCommand { & taskkill.exe /PID $expectedMinerPid /T /F } -LogOutput
  if ($nativeExit -ne 0 -and (Get-Process -Id $expectedMinerPid -ErrorAction SilentlyContinue)) {
    throw "fin_factor 进程树终止失败 (exit $nativeExit)"
  }
  $deadline = (Get-Date).AddSeconds(20)
  while ((Get-Date) -lt $deadline -and (Get-Process -Id $expectedMinerPid -ErrorAction SilentlyContinue)) {
    Start-Sleep -Milliseconds 250
  }
  if (Get-Process -Id $expectedMinerPid -ErrorAction SilentlyContinue) {
    throw "fin_factor 进程树在超时后仍存活"
  }
  Remove-Item -LiteralPath $RequestPath -Force -ErrorAction Stop
  Log "卡死 fin_factor 树已终止；保留原始请求，由 watcher 抢救已完成轮次"
}

$pipelineLock = $null
$pipelineExit = 0
try {
  $lockPath = Join-Path $logdir "daily_auto_pipeline.lock"
  try {
    $pipelineLock = [System.IO.File]::Open(
      $lockPath,
      [System.IO.FileMode]::OpenOrCreate,
      [System.IO.FileAccess]::ReadWrite,
      [System.IO.FileShare]::None
    )
  } catch {
    Log "已有每日自动管线正在运行，本次退出"
    exit 75
  }

  Assert-Directory $shared "共享发布目录"
  Assert-Directory $qlibData "群晖 Qlib 数据目录"
  Assert-Directory $rd "RD-Agent 目录"
  if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python 不可用: $python" }
  if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw "WSL 不可用" }
  $env:SHARED_DIR = $shared
  $env:QLIB_DATA_PATH = "C:\qlib_data\cn_data"

  Log "===== 每日自动管线启动 ====="

  # PID/request/attempt/log-age bound recovery for a genuinely stuck mining
  # tree.  The fixed operation can only terminate the one stale fin_factor
  # process tree; the watcher remains alive and salvages completed loops.
  if (Test-Path -LiteralPath $rdagentRecoveryRequest -PathType Leaf) {
    try {
      Invoke-StuckRdagentRecovery $rdagentRecoveryRequest
      Log "RD-Agent 卡死恢复请求已消费，跳过模型管线"
      return
    } catch {
      throw "RD-Agent 卡死恢复请求失败: $($_.Exception.Message)"
    }
  }

  # Narrow maintenance path for an already-elevated scheduled task.  The
  # request is bound to the watcher-written PID and can only invoke the fixed
  # restart script; it cannot supply a command or arbitrary arguments.
  if (Test-Path -LiteralPath $watcherRestartRequest -PathType Leaf) {
    try {
      $maintenance = Get-Content -LiteralPath $watcherRestartRequest -Raw -Encoding UTF8 | ConvertFrom-Json
      if ([string]$maintenance.operation -ne "restart_watch_predict_pc") {
        throw "不支持的 watcher 维护操作"
      }
      $expectedWatcherPid = [int]$maintenance.expected_pid
      if ($expectedWatcherPid -le 0) {
        throw "watcher 维护请求缺少有效 expected_pid"
      }
      $actualWatcherPid = [int]((Get-Content -LiteralPath (Join-Path $proj "data\watch_predict_pc.pid") -Raw).Trim())
      if ($actualWatcherPid -ne $expectedWatcherPid) {
        throw "watcher PID 已变化: expected=$expectedWatcherPid actual=$actualWatcherPid"
      }
      $restartScript = Join-Path $proj "scripts\restart_watch_predict_pc.ps1"
      Log "执行仅 watcher 管理员重启 PID=$expectedWatcherPid"
      $nativeExit = Invoke-NativeCommand {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $restartScript -Hidden
      } -LogOutput
      if ($nativeExit -ne 0) {
        throw "watcher 管理员重启失败 (exit $nativeExit)"
      }
      Remove-Item -LiteralPath $watcherRestartRequest -Force -ErrorAction Stop
      Log "watcher 管理员重启完成；维护请求已消费，跳过模型管线"
      return
    } catch {
      throw "watcher 维护请求失败: $($_.Exception.Message)"
    }
  }

# 每日自愈启动常驻监听；先确认没有现存实例，避免替换正在阻塞等待子进程的 watcher。
  $watcher = Join-Path $proj "scripts\watch_predict_pc.ps1"
  if (Test-Path $watcher) {
    $watcherScanOk = $true
    try {
      $runningWatchers = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" | Where-Object {
        $_.CommandLine -match $watcherCommandPattern
      })
    } catch {
      $watcherScanOk = $false
      Log "无法确认数据监听状态，本次跳过启动: $($_.Exception.Message)"
    }
    if ($watcherScanOk -and $runningWatchers.Count -gt 0) {
      $runningWatcherIds = ($runningWatchers | ForEach-Object { $_.ProcessId }) -join ","
      Log "quantinvest 数据监听已运行 PID=$runningWatcherIds，跳过重复启动"
    } elseif ($watcherScanOk) {
      try {
        Start-Process -FilePath "powershell.exe" -ArgumentList @(
          "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
          "-File", "`"$watcher`""
        ) -WorkingDirectory $proj -WindowStyle Hidden -ErrorAction Stop
        Log "已启动 quantinvest 数据监听"
      } catch {
        Log "数据监听启动失败: $($_.Exception.Message)"
      }
    }
  }

# 0. tushare token
  if (-not $env:TUSHARE_TOKEN -and (Test-Path "$proj\data\.tushare_token")) {
    $env:TUSHARE_TOKEN = (Get-Content "$proj\data\.tushare_token" -Raw).Trim()
  }

# 1. 最新批次(final/batches里最新)
  $batchFile = Get-ChildItem "$rd\final\batches\*.json" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $batchFile) { throw "无批次文件: $rd\final\batches" }
  $batch = $batchFile.BaseName
  Log "最新批次: $batch"

  # A daily trigger is needed so Friday's close can be processed early Saturday.
  # Skip later weekend/holiday repeats unless either the market date or factor
  # batch file changed since the last fully successful run.
  $pipelineStatePath = Join-Path $logdir "daily_auto_pipeline_state.json"
  $marketDataRoot = Join-Path $shared "tushare_daily"
  $latestMarketFile = Get-ChildItem -LiteralPath $marketDataRoot -File -Filter "*.parquet" -ErrorAction SilentlyContinue |
    Where-Object { $_.Length -gt 0 -and $_.BaseName -match '^\d{8}' } |
    Sort-Object { $_.BaseName.Substring(0, 8) } -Descending |
    Select-Object -First 1
  if (-not $latestMarketFile) { throw "无可信最新行情日期: $marketDataRoot" }
  $marketDigits = $latestMarketFile.BaseName.Substring(0, 8)
  $latestMarketDate = "$($marketDigits.Substring(0,4))-$($marketDigits.Substring(4,2))-$($marketDigits.Substring(6,2))"
  $pipelineFingerprint = "$latestMarketDate|$batch|$($batchFile.LastWriteTimeUtc.Ticks)|$($batchFile.Length)"
  $previousFingerprint = ""
  try {
    if (Test-Path -LiteralPath $pipelineStatePath -PathType Leaf) {
      $previousState = Get-Content -LiteralPath $pipelineStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($previousState.status -eq "done") {
        $previousFingerprint = [string]$previousState.fingerprint
      }
    }
  } catch {
    Log "历史完成状态无法读取，将正常重跑: $($_.Exception.Message)"
  }
  if ($latestMarketDate -and $previousFingerprint -eq $pipelineFingerprint) {
    Log "行情日/因子批次未变化，已成功处理过，本次跳过: $latestMarketDate / $batch"
    return
  }

# 2. 同步数据 + 重建 csi300 universe
  Invoke-QlibMirror
  Log "重建 csi300 universe"
  Push-Location $rd
  try {
    $nativeExit = Invoke-NativeCommand { & $python "build_csi300.py" }
    if ($nativeExit -ne 0) { throw "build_csi300.py 失败 (exit $nativeExit)" }
    $nativeExit = Invoke-NativeCommand {
      & $python "prediction_preflight.py" --qlib-root "C:\qlib_data\cn_data" --universe csi300 --expected-market-date $latestMarketDate --expected-market-date-basis latest_market_parquet
    } -LogOutput
    if ($nativeExit -ne 0) { throw "csi300 预测数据预检失败；行情日=$latestMarketDate，未覆盖/发布旧清单" }
  } finally {
    Pop-Location
  }

# 3. 全跑6模型, xgb+catboost优先(它俩是生产+汇总用)
  $models = @("xgb", "catboost", "lgb", "ols", "ridge", "lasso")
  $n = $models.Count; $i = 0; $failed = @(); $completed = 0
  foreach ($m in $models) {
    $i++
    Log "[$i/$n] $m 训练+回测 (run_model)"
    $nativeExit = Invoke-NativeCommand { & wsl.exe -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_MODEL='$m' RDAGENT_FACTOR_BATCH='$batch' python run_model.py > /mnt/c/rdagent/run_model_${m}.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock" }
    if ($nativeExit -ne 0) { $failed += "$m/train"; Log "[$i/$n] $m 训练+回测失败(exit $nativeExit), 跳过"; continue }
    foreach ($f in @("model_results.json","model_runs_history.json","model_curves.json")) {
      $source = Join-Path $rd $f
      if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "模型结果缺失: $source" }
      Copy-Item -LiteralPath $source -Destination (Join-Path $shared $f) -Force -ErrorAction Stop
    }
    Log "[$i/$n] $m 预测次日买入清单 (predict_next_day)"
    $nativeExit = Invoke-NativeCommand { & wsl.exe -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; RDAGENT_EXPECTED_MARKET_DATE_BASIS=latest_market_parquet RDAGENT_EXPECTED_MARKET_DATE='$latestMarketDate' RDAGENT_RETRAIN=1 RDAGENT_MODEL='$m' RDAGENT_FACTOR_BATCH='$batch' python predict_next_day.py > /mnt/c/rdagent/predict_next_day_${m}.log 2>&1 ) 9>/mnt/c/rdagent/.gpu_train.lock" }
    if ($nativeExit -ne 0) {
      $failed += "$m/predict"
      Log "[$i/$n] $m 预测清单失败(exit $nativeExit)"
      continue
    }

    Push-Location $rd
    try {
      $nativeExit = Invoke-NativeCommand { & $python "post_process.py" }
      if ($nativeExit -ne 0) { throw "$m post_process.py 失败 (exit $nativeExit)" }
      $env:RDAGENT_TAG_BUYLIST = "1"
      $env:RDAGENT_MODEL = $m
      $env:RDAGENT_FACTOR_BATCH = $batch
      try {
        $nativeExit = Invoke-NativeCommand { & $python "export_rdagent.py" }
        if ($nativeExit -ne 0) { throw "$m export_rdagent.py 失败 (exit $nativeExit)" }
      } finally {
        Remove-Item Env:\RDAGENT_TAG_BUYLIST -ErrorAction SilentlyContinue
      }
    } catch {
      $failed += "$m/publish"
      Log "[$i/$n] $($_.Exception.Message)"
      continue
    } finally {
      Pop-Location
    }
    $completed++
    Log "[$i/$n] $m 清单已发布"
  }
  $failMsg = if ($failed.Count) { ", 失败=$($failed -join ',')" } else { "" }
  Log "全跑结束: 完整成功 $completed/$n$failMsg"

  # 4. 汇总 xgb+catboost 清单 -> 缺失/久远研报自动排队生成(alphagen_listener会接力跑)
  Log "汇总 xgb+catboost + 触发研报生成"
  $nativeExit = Invoke-NativeCommand { & $python "$rd\agg_buylist_gen_reports.py" $batch } -LogOutput
  if ($nativeExit -ne 0) { $failed += "aggregate-reports"; Log "汇总清单失败(exit $nativeExit)" }

  # 5. 汇总清单股票主营行业 -> 瓶颈链没分析过的行业, 按MLCC/电子布框架生成报告
  Log "汇总行业 + 瓶颈链新行业生成"
  $nativeExit = Invoke-NativeCommand { & $python "$rd\gen_thesis_for_buylist.py" $batch } -LogOutput
  if ($nativeExit -ne 0) { $failed += "industry-thesis"; Log "行业报告生成失败(exit $nativeExit)" }

  if ($failed.Count) {
    throw "每日管线部分失败: $($failed -join ', ')"
  }
  $stateTemp = "$pipelineStatePath.$PID.tmp"
  @{
    status = "done"
    fingerprint = $pipelineFingerprint
    market_date = $latestMarketDate
    batch = $batch
    completed_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
  } | ConvertTo-Json -Compress | Out-File -FilePath $stateTemp -Encoding utf8
  Move-Item -LiteralPath $stateTemp -Destination $pipelineStatePath -Force -ErrorAction Stop
  Log "===== 每日自动管线完成 ====="
} catch {
  $pipelineExit = 1
  Log "===== 每日自动管线失败: $($_.Exception.Message) ====="
} finally {
  if ($pipelineLock) { $pipelineLock.Dispose() }
}

exit $pipelineExit

# ---- 注册Windows计划任务(每天1:30, 管理员PowerShell运行一次即可) ----
# $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$proj\scripts\daily_auto_pipeline.ps1`""
# $trigger = New-ScheduledTaskTrigger -Daily -At 1:30AM
# $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 4)
# Register-ScheduledTask -TaskName "quantinvest每日自动管线" -Action $action -Trigger $trigger -Settings $set -RunLevel Highest -Description "凌晨1:30: 最新批次全跑6模型+汇总xgb/catboost+生成缺失研报"
