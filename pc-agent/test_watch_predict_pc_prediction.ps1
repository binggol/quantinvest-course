$ErrorActionPreference = "Stop"

function Assert-True([bool]$Condition, [string]$Message) {
  if (-not $Condition) { throw "ASSERT FAILED: $Message" }
}

$watcher = Join-Path $PSScriptRoot "watch_predict_pc.ps1"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $watcher,
  [ref]$tokens,
  [ref]$parseErrors
)
Assert-True ($parseErrors.Count -eq 0) "watch_predict_pc.ps1 must parse without errors"

foreach ($functionName in @(
    "Test-RdagentPredictionPreflight",
    "Test-RdagentPredictionArtifact",
    "Test-RdagentScoreArtifact"
  )) {
  $definition = $ast.Find({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $functionName
    }, $true)
  Assert-True ($null -ne $definition) "missing prediction guard: $functionName"
  Invoke-Expression $definition.Extent.Text
}

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("watch_prediction_" + [guid]::NewGuid().ToString("N"))
$localRoot = Join-Path $testRoot "local"
$sourceRoot = Join-Path $testRoot "source"
$marketRoot = Join-Path $testRoot "tushare_daily"
$today = (Get-Date).Date
$marketDate = $today.AddDays(-10).ToString("yyyy-MM-dd")
$marketDigits = $marketDate.Replace("-", "")
try {
  foreach ($root in @($localRoot, $sourceRoot)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $root "calendars"), (Join-Path $root "instruments") | Out-Null
    "$($today.AddDays(-11).ToString('yyyy-MM-dd'))`n$marketDate`n" | Set-Content -LiteralPath (Join-Path $root "calendars\day.txt") -Encoding UTF8
  }
  New-Item -ItemType Directory -Force -Path $marketRoot | Out-Null
  [System.IO.File]::WriteAllBytes((Join-Path $marketRoot "$marketDigits.parquet"), [byte[]]@(1))
  $rows = for ($index = 0; $index -lt 300; $index++) {
    "sh$($index.ToString('000000'))`t2020-01-01`t$marketDate"
  }
  $rows | Set-Content -LiteralPath (Join-Path $localRoot "instruments\csi300.txt") -Encoding UTF8

  $valid = Test-RdagentPredictionPreflight -Universe csi300 -QlibRoot $localRoot -SourceQlibRoot $sourceRoot -MarketDataRoot $marketRoot -MaxCalendarAgeDays 14
  Assert-True $valid.Ok "exact CSI300 snapshot should pass: $($valid.Message)"
  Assert-True ($valid.MarketDate -eq $marketDate) "preflight returned the wrong market date"
  Assert-True ($valid.FreshnessBasis -match 'latest_market_parquet') "freshness basis does not identify the quote benchmark"

  $partialRows = $rows | Select-Object -First 50
  $partialRows | Set-Content -LiteralPath (Join-Path $localRoot "instruments\csi500.txt") -Encoding UTF8
  $partial = Test-RdagentPredictionPreflight -Universe csi500 -QlibRoot $localRoot -SourceQlibRoot $sourceRoot -MarketDataRoot $marketRoot -MaxCalendarAgeDays 14
  Assert-True (-not $partial.Ok) "partial CSI500 snapshot was accepted"
  Assert-True ($partial.Message -match 'expected=500') "partial-universe error lacks the rebuild diagnosis"

  [System.IO.File]::WriteAllBytes((Join-Path $marketRoot "$($today.AddDays(-9).ToString('yyyyMMdd')).parquet"), [byte[]]@(1))
  $stale = Test-RdagentPredictionPreflight -Universe csi300 -QlibRoot $localRoot -SourceQlibRoot $sourceRoot -MarketDataRoot $marketRoot -MaxCalendarAgeDays 14
  Assert-True (-not $stale.Ok) "calendar behind the latest quote parquet was accepted"
  Assert-True ($stale.Message -match 'behind latest market data') "calendar freshness error is not actionable"

  $artifactPath = Join-Path $testRoot "pool_buy_csi300_lgb.json"
  $hits = for ($rank = 1; $rank -le 50; $rank++) {
    @{ code = "sh$(($rank - 1).ToString('000000'))"; rank = $rank; score = 1.0 / $rank }
  }
  @{ universe = "csi300"; model = "lgb"; as_of = $marketDate; n_universe = 270; hits = $hits } |
    ConvertTo-Json -Depth 5 -Compress |
    Set-Content -LiteralPath $artifactPath -Encoding UTF8
  $artifact = Test-RdagentPredictionArtifact -Path $artifactPath -Universe csi300 -Model lgb -MarketDate $marketDate -RunStartedUtc ([datetime]::UtcNow.AddMinutes(-1))
  Assert-True $artifact.Ok "fresh correctly identified artifact should pass: $($artifact.Message)"
  $wrongIdentity = Test-RdagentPredictionArtifact -Path $artifactPath -Universe csi500 -Model lgb -MarketDate $marketDate -RunStartedUtc ([datetime]::UtcNow.AddMinutes(-1))
  Assert-True (-not $wrongIdentity.Ok) "wrong-universe artifact was accepted"
} finally {
  Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$source = [System.IO.File]::ReadAllText($watcher)
Assert-True (-not ([regex]::IsMatch($source, '/XF\s+csi300\.txt\s+csi300\.txt\.bak\s+/NFL'))) "csi500/csi1000 were removed from the mirror exclusion list"
Assert-True ($source.Contains('$poolPredictExit = $LASTEXITCODE')) "pool prediction exit code is not checked"
Assert-True ($source.Contains('$bpPredictExit = $LASTEXITCODE')) "batch prediction exit code is not checked"
Assert-True ($source.Contains('$a158PredictExit = $LASTEXITCODE')) "Alpha158 prediction exit code is not checked"
Assert-True ($source.Contains('Test-RdagentPredictionArtifact -Path $bf')) "pool/batch artifacts are not validated before publish"
Assert-True ($source.Contains('Publish-FileAtomic $bf')) "prediction artifacts are not atomically published"
Assert-True (-not ($source.Contains('if (Test-Path $bf) { Copy-Item $bf'))) "stale local pool/batch artifact can still be copied after failure"

Write-Host "test_watch_predict_pc_prediction.ps1 passed"
