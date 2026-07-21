param(
  [string]$RdagentRoot = "C:\rdagent",
  [string]$Python = "D:\anaconda3\python.exe"
)

$ErrorActionPreference = "Stop"
$target = Join-Path $RdagentRoot "rdagent\scenarios\qlib\developer\factor_runner.py"
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
  throw "RD-Agent factor runner not found: $target"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw "Python runtime not found: $Python"
}

$original = [System.IO.File]::ReadAllText($target)
$content = $original.Replace("`r`n", "`n")

# Pandarallel creates a long-lived Windows process pool at module import time.
# Factor de-duplication is a small, correctness-sensitive operation, so use the
# equivalent serial GroupBy.apply path and avoid worker/pipe failures entirely.
$content = [regex]::Replace(
  $content,
  '(?m)^from pandarallel import pandarallel\s*\n',
  ''
)
$content = [regex]::Replace(
  $content,
  '(?m)^pandarallel\.initialize\([^\r\n]*\)\s*\n',
  ''
)

$parallelApply = @'
            concat_feature.groupby("datetime")
            .parallel_apply(
'@.TrimEnd().Replace("`r`n", "`n")
$serialApply = @'
            concat_feature.groupby("datetime")
            .apply(
'@.TrimEnd().Replace("`r`n", "`n")
if ($content.Contains($parallelApply)) {
  $content = $content.Replace($parallelApply, $serialApply)
} elseif (-not $content.Contains($serialApply)) {
  throw "Unexpected factor de-duplication GroupBy.apply block in $target"
}

# Retain the existing pairwise-Pearson semantics: do not globally discard a
# row because an unrelated factor is null, and treat strong negative and
# positive correlations symmetrically.
$positiveCorrelationOnly = '        IC_max = IC_max.unstack().max(axis=0)'
$absoluteCorrelation = '        IC_max = IC_max.unstack().abs().max(axis=0)'
if ($content.Contains($positiveCorrelationOnly)) {
  $content = $content.Replace($positiveCorrelationOnly, $absoluteCorrelation)
} elseif (-not $content.Contains($absoluteCorrelation)) {
  throw "Unexpected maximum-correlation logic in $target"
}

$globalDropna = 'combined_factors = pd.concat([SOTA_factor, new_factors], axis=1).dropna()'
$pairwiseFriendlyConcat = 'combined_factors = pd.concat([SOTA_factor, new_factors], axis=1)'
if ($content.Contains($globalDropna)) {
  $content = $content.Replace($globalDropna, $pairwiseFriendlyConcat)
} elseif (-not $content.Contains($pairwiseFriendlyConcat)) {
  throw "Unexpected combined-factor missing-value logic in $target"
}

if ($content.Contains("pandarallel") -or $content.Contains("parallel_apply")) {
  throw "Pandarallel references remain in $target"
}
if (-not $content.Contains('IC_max[IC_max < 0.99]')) {
  throw "The strict 0.99 factor-correlation gate is missing from $target"
}

$temp = "$target.serial_dedup.$PID.py"
$backup = "$target.pre_serial_dedup.bak"
try {
  [System.IO.File]::WriteAllText($temp, $content, [System.Text.UTF8Encoding]::new($false))
  & $Python -m py_compile $temp
  if ($LASTEXITCODE -ne 0) {
    throw "Patched RD-Agent factor runner failed syntax validation"
  }

  # A second installation is a true no-op: no rewrite, timestamp churn, or
  # additional backups once the validated content is already installed.
  if ($content -cne $original) {
    if (-not (Test-Path -LiteralPath $backup)) {
      Copy-Item -LiteralPath $target -Destination $backup -Force
    }
    $replaceBackup = "$target.serial_dedup.$PID.replacebak"
    try {
      [System.IO.File]::Replace($temp, $target, $replaceBackup)
    } finally {
      Remove-Item -LiteralPath $replaceBackup -Force -ErrorAction SilentlyContinue
    }
  }
} finally {
  Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
}

Write-Host "RD-Agent serial factor de-duplication installed: $target"
