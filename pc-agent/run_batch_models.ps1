# Re-run every model for a batch without the watcher. Publish each successful
# model result to the shared directory used by the web application.
param(
  [string]$Batch = "20260608_2321",
  [string[]]$Models = @("lgb","xgb","catboost","ols","ridge","lasso")
)
$ErrorActionPreference = "Stop"
$allowedModels = @("lgb", "xgb", "catboost", "ols", "ridge", "lasso")
if ($Batch -notmatch '\A[A-Za-z0-9._:-]+\z') {
  throw "Batch contains unsupported characters"
}
foreach ($model in $Models) {
  if ($model -notin $allowedModels) {
    throw "Unsupported model: $model"
  }
}
$shared = $env:SHARED_DIR
if (-not $shared) { $shared = "\/app/qlib_data\csv_tmp" }
$log = "C:\rdagent\daily_logs\run_batch_$(Get-Date -Format yyyyMMdd_HHmmss).log"
if (-not (Test-Path "C:\rdagent\daily_logs")) { New-Item -ItemType Directory -Force "C:\rdagent\daily_logs" | Out-Null }
function Log($m){ $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m; Write-Host $line; Add-Content -Path $log -Value $line -Encoding utf8 }

function CopyResults {
  foreach ($f in 'model_results.json','model_curves.json','model_runs_history.json') {
    if (Test-Path "C:\rdagent\$f") { Copy-Item "C:\rdagent\$f" (Join-Path $shared $f) -Force }
  }
}

$n = $Models.Count; $i = 0; $failed = @()
Log "==== batch=$Batch, models=$n ===="
foreach ($m in $Models) {
  $i++
  Log "($i/$n) $m train+backtest started"
  wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && RDAGENT_MODEL='$m' RDAGENT_FACTOR_BATCH='$Batch' python run_model.py" 2>&1 | Add-Content -Path $log -Encoding utf8
  $ex = $LASTEXITCODE
  if ($ex -eq 0) { Log "($i/$n) $m completed; publishing results"; CopyResults }
  else { $failed += "$m(exit=$ex)"; Log "($i/$n) $m failed exit=$ex; continuing" }
}
if ($failed.Count) {
  Log "==== completed with failures: $($failed -join ', ') ===="
  exit 1
}
Log "==== all models completed successfully ===="
