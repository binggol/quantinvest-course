# 一次性监控: 等当前基本面挖矿(rdagent fin_factor)跑完 + watcher完成后处理(批次导出),
# 再重启 watch_predict_pc.ps1 让它加载新的 fund_compare 分支. 然后自身退出.
# 由 Claude 装载 (2026-06-21). 日志: scripts\_restart_watcher.log
$ErrorActionPreference = 'SilentlyContinue'
$shared = "\/app/qlib_data\csv_tmp"
$rdStatus = Join-Path $shared "rdagent_status.json"
$logf = Join-Path $PSScriptRoot "_restart_watcher.log"
function Log($m){ "$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))  $m" | Out-File -FilePath $logf -Append -Encoding utf8 }

Log "monitor armed (pid $PID). waiting for fin_factor mine to finish..."

function MineAlive {
  # rdagent.exe fin_factor 或 python.exe 跑 fin_factor (排除本监控自身)
  $p = Get-CimInstance Win32_Process -Filter "Name='rdagent.exe'" | Where-Object { $_.CommandLine -match 'fin_factor' }
  if ($p) { return $true }
  $q = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'fin_factor' }
  return [bool]$q
}
function StatusRunning {
  try { $s = Get-Content $rdStatus -Raw | ConvertFrom-Json; return ($s.state -eq 'running') } catch { return $false }
}

$gone = 0
while ($true) {
  Start-Sleep -Seconds 45
  if (MineAlive) { $gone = 0; continue }
  # 进程没了; 再确认 watcher 也不在 running 状态(批次/resid后处理已完成)
  if (StatusRunning) { Log "mine proc gone but status still running (post-processing batch); wait"; $gone = 0; continue }
  $gone++
  Log "mine gone + status idle (check $gone/2)"
  if ($gone -ge 2) { break }
}

Start-Sleep -Seconds 20   # 余量, 让 watcher 写完 done / 删 request
Log "restarting watcher now."
# 杀旧 watcher(排除本监控自身 $PID)
$old = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'watch_predict_pc\.ps1' -and $_.ProcessId -ne $PID }
foreach ($w in $old) { Log "stop old watcher pid $($w.ProcessId)"; Stop-Process -Id $w.ProcessId -Force }
Start-Sleep -Seconds 3
# 经计划任务重启(与平时启动方式一致, 工作目录正确)
Start-ScheduledTask -TaskName 'quantinvest预测监听'
Start-Sleep -Seconds 5
$new = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'watch_predict_pc\.ps1' -and $_.ProcessId -ne $PID }
if ($new) { Log "watcher restarted OK pid=$($new.ProcessId -join ',')  (fund_compare branch loaded)" }
else { Log "WARN: watcher not detected after restart — 手动起: Start-ScheduledTask 'quantinvest预测监听'" }
Log "monitor done, exiting."
