$ErrorActionPreference = "Stop"

function Assert-True {
  param([bool]$Condition, [string]$Message)
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

# Load only the real constants and validation functions. Dot-sourcing the watcher would
# start its resident loop and external processes, so select the definitions from its AST.
foreach ($variableName in @("allowedRdagentModels", "allowedRdagentUniverses")) {
  $assignment = $ast.Find({
      param($node)
      $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
      $node.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
      $node.Left.VariablePath.UserPath -eq $variableName
    }, $true)
  Assert-True ($null -ne $assignment) "missing security constant: $variableName"
  Invoke-Expression $assignment.Extent.Text
}

foreach ($functionName in @(
    "Test-SafeRequestLabel",
    "Test-AllowedRdagentModel",
    "Test-AllowedRdagentUniverse",
    "Get-RdagentWorkspaceId",
    "Test-SafeWorkspacePath",
    "Get-PersistentRdagentWorkspacePath",
    "Reject-WatcherRequest"
  )) {
  $definition = $ast.Find({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $functionName
    }, $true)
  Assert-True ($null -ne $definition) "missing security function: $functionName"
  Invoke-Expression $definition.Extent.Text
}

$unicodeLabel = "$([char]0x6279)$([char]0x6B21) 2026.07:$([char]0x57FA)$([char]0x672C)$([char]0x9762)-1"
foreach ($label in @(
    "20260711_1200",
    "batch 2026.07:alpha-1",
    $unicodeLabel
  )) {
  Assert-True (Test-SafeRequestLabel $label) "safe label rejected: $label"
}
Assert-True (Test-SafeRequestLabel "" -AllowEmpty) "empty optional label rejected"
Assert-True (-not (Test-SafeRequestLabel "")) "empty required label accepted"

$unsafeLabels = @(
  "x'y",
  'x"y',
  "x;y",
  "x`ny",
  'x$y',
  'x`y',
  "x/y",
  'x\y',
  "x`ty",
  '$(id)',
  '../escape'
)
foreach ($label in $unsafeLabels) {
  Assert-True (-not (Test-SafeRequestLabel $label)) "unsafe label accepted: $label"
}

foreach ($model in @("lgb", "xgb", "catboost", "dlinear", "itransformer")) {
  Assert-True (Test-AllowedRdagentModel $model) "allowed model rejected: $model"
}
Assert-True (Test-AllowedRdagentModel "all" -AllowAll) "all model rejected where permitted"
Assert-True (-not (Test-AllowedRdagentModel "all")) "all model accepted without opt-in"
foreach ($model in @("lgb;id", "xgb'", "unknown", "lgb`nwhoami")) {
  Assert-True (-not (Test-AllowedRdagentModel $model -AllowAll)) "unsafe model accepted: $model"
}

foreach ($universe in @("csi300", "csi500", "csi1000")) {
  Assert-True (Test-AllowedRdagentUniverse $universe) "allowed universe rejected: $universe"
}
Assert-True (Test-AllowedRdagentUniverse "all" -AllowAll) "all universe rejected where permitted"
Assert-True (Test-AllowedRdagentUniverse "allunivs" -AllowAllUniverses) "allunivs rejected where permitted"
foreach ($universe in @("csi300;id", "../csi300", "unknown")) {
  Assert-True (-not (Test-AllowedRdagentUniverse $universe -AllowAll -AllowAllUniverses)) "unsafe universe accepted: $universe"
}

$workspaceId = "c46678b39fc04c8f976e70ae05c6364f"
foreach ($path in @(
    "D:/rdagent_workspace/$workspaceId",
    "Z:/claude/rdagent_workspace/$workspaceId"
  )) {
  Assert-True (Test-SafeWorkspacePath $path) "safe workspace path rejected: $path"
  Assert-True ((Get-RdagentWorkspaceId $path) -eq $workspaceId) "workspace ID parse failed: $path"
  Assert-True ((Get-PersistentRdagentWorkspacePath $path) -eq "Z:/claude/rdagent_workspace/$workspaceId") "persistent workspace mapping failed: $path"
}
foreach ($path in @(
    "C:/rdagent/log/mine_csi300_20260711/workspace-1",
    "C:/rdagent/../Windows/System32",
    "C:/rdagent/log/a';id",
    'C:/rdagent/log/$HOME',
    "C:\rdagent\log\workspace",
    "D:/rdagent/log/workspace",
    "D:/rdagent_workspace/$workspaceId/child",
    "D:/rdagent_workspace/../$workspaceId",
    "D:\rdagent_workspace\$workspaceId",
    "Z:/claude/rdagent_workspace_archive/$workspaceId",
    "Z:/claude/rdagent_workspace/$workspaceId;id",
    "D:/rdagent_workspace/1234"
  )) {
  Assert-True (-not (Test-SafeWorkspacePath $path)) "unsafe workspace path accepted: $path"
}

$testId = [guid]::NewGuid().ToString("N")
$tempRoot = [System.IO.Path]::GetTempPath()
$tempRequest = Join-Path $tempRoot "watcher_security_$testId.request.json"
$tempStatus = Join-Path $tempRoot "watcher_security_$testId.status.json"
try {
  [System.IO.File]::WriteAllText($tempRequest, '{"model":"lgb;id"}')
  Reject-WatcherRequest $tempRequest $tempStatus "test rejection"
  Assert-True (-not (Test-Path $tempRequest)) "rejected request file was not deleted"
  Assert-True (Test-Path $tempStatus) "rejected request did not write a status file"
  $rejection = Get-Content $tempStatus -Raw | ConvertFrom-Json
  Assert-True ($rejection.state -eq "error") "rejected request status is not error"
  Assert-True (-not [string]::IsNullOrWhiteSpace([string]$rejection.msg)) "rejected request status lacks a message"
} finally {
  Remove-Item $tempRequest -Force -ErrorAction SilentlyContinue
  Remove-Item $tempStatus -Force -ErrorAction SilentlyContinue
}

$source = [System.IO.File]::ReadAllText($watcher)
foreach ($snippet in @(
    'Reject-WatcherRequest $rdReqFile $rdStatusFile',
    'Reject-WatcherRequest $thesisReqFile $thesisStatusFile',
    'Reject-WatcherRequest $predA158ReqFile $predA158StatusFile',
    'Reject-WatcherRequest $poolReqFile $poolStatusFile',
    'Reject-WatcherRequest $fcompReqFile $fcompStatusFile',
    'Reject-WatcherRequest $batchPredReqFile $batchPredStatusFile',
    'Reject-WatcherRequest $arenaReqFile $arenaStatusFile',
    'Reject-WatcherRequest $uarenaReqFile $uarenaStatusFile',
    'Reject-WatcherRequest $barenaReqFile $barenaStatusFile'
  )) {
  Assert-True ($source.Contains($snippet)) "request branch lacks reject-and-delete path: $snippet"
}
Assert-True (-not $source.Contains("RDAGENT_SOTA_WS_OVERRIDE='$newWs'")) "workspace path is still interpolated into bash -lc"
Assert-True ($source.Contains('wsl -e env "RDAGENT_SOTA_WS_OVERRIDE=$newWs" bash -lc')) "workspace path is not passed as a separate env argument"

# Every variable on a WSL/bash command line must be reviewed here. Any future interpolation
# fails this test until it has both strict validation and an explicit audit entry.
$allowedWslVariables = @(
  "rdModel", "rdBatch", "rdHold", "rdTopN", "rdCost", "m", "newWs", "newBatch",
  "rdMode", "mm", "pUniv", "bpBatchEnv", "bpUniv", "bm", "outf", "uUniv",
  "baBatchEnv", "bu", "rdUniverse", "predictionPreflight", "a158Preflight",
  "poolPreflight", "fcPreflight", "bpPreflight", "incumbentBatch"
)
$wslLines = @($source -split "`r?`n" | Where-Object { $_ -match '\bwsl\s+-e\b' -and $_ -match '\bbash\s+-lc\b' })
Assert-True ($wslLines.Count -gt 0) "no WSL/bash commands found to audit"
foreach ($line in $wslLines) {
  $names = @()
  foreach ($match in [regex]::Matches($line, '\$\{(?<name>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?<name>[A-Za-z_][A-Za-z0-9_]*)')) {
    $names += $match.Groups["name"].Value
  }
  foreach ($name in ($names | Select-Object -Unique)) {
    Assert-True ($name -in $allowedWslVariables) "unaudited WSL interpolation variable: $name"
  }
}

Write-Host "test_watch_predict_pc_security.ps1 passed ($($wslLines.Count) WSL/bash calls audited)"
