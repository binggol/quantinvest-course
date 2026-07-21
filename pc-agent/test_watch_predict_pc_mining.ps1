$ErrorActionPreference = "Stop"

function Assert-True([bool]$Condition, [string]$Message) {
  if (-not $Condition) { throw "ASSERT FAILED: $Message" }
}

$watcher = Join-Path $PSScriptRoot "watch_predict_pc.ps1"
$supervisor = Join-Path $PSScriptRoot "rdagent_mine_supervisor.ps1"
. $supervisor
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $watcher,
  [ref]$tokens,
  [ref]$parseErrors
)
Assert-True ($parseErrors.Count -eq 0) "watch_predict_pc.ps1 must parse without errors"
$source = [System.IO.File]::ReadAllText($watcher)
Assert-True ($source.Contains('Publish-RdagentWorkspace -Value $newWs')) "winner workspace is not persisted before publication"
Assert-True ($source.Contains('$manifest.workspace = $persistentWs')) "batch manifest is not repointed to persistent Z workspace"
Assert-True ($source.Contains('$env:SHARED_DIR          = $shared')) "RD-Agent exports do not inherit the resolved UNC shared directory"
Assert-True ($source.Contains('if ($exportExit -ne 0)')) "batch export failure is not checked"
Assert-True ($source.Contains('if ($modelExit -ne 0)')) "automatic LGB failure is not checked"
Assert-True ($source.Contains('$env:RDAGENT_SCREEN_EXACT_WORKSPACE = $newWs')) "orthogonal screen is not bound to the current winner workspace"
Assert-True ($source.Contains('$winnerScreen.scope -ne "exact_workspace"')) "winner screen scope is not validated fail-closed"
Assert-True ($source.Contains('[string]$winnerScreen.universe -ne $rdUniverse')) "winner screen universe is not validated fail-closed"
Assert-True ($source.Contains('if ([int]$winnerScreen.n_pass -lt 1)')) "failed orthogonal/decay screen does not block batch publication"
Assert-True ($source.Contains('if ($rdUniverse -eq "csi500")')) "CSI 500 production publication is not fail-closed without an exact evaluator"
Assert-True ($source.Contains('There is no independent CSI 500 residual evaluator yet.')) "CSI 500 research-only status is not explicit"
Assert-True ($source.Contains('RDAGENT_FACTOR_EXACT_SCREEN_PATH=C:/rdagent/rdagent_screen.json')) "factor analysis does not receive the exact winner screen"
Assert-True ($source.Contains('RDAGENT_FACTOR_EXACT_SCREEN_UNIVERSE=$rdUniverse')) "factor analysis does not receive the screen universe"
Assert-True ($source.Contains('if ($faExit -eq 3)')) "empty FDR/exact-screen intersection is not handled as a normal no-winner run"
Assert-True ($source.Contains('No-winner runs are persisted with their completed backtest details.')) "no-winner status can regress to claiming the run was not recorded"
Assert-True ($source.Contains('SEEDS=0,1,2 RDAGENT_UNIVERSE=''$rdUniverse'' RDAGENT_MODEL=lgb')) "automatic production curve is not a universe-bound three-seed score ensemble"
Assert-True ($source.Contains('--accepted-manifest $researchManifestPath')) "accepted research workspaces are not preserved before publication"
Assert-True ($source.Contains('promote_production_champion.py')) "production pointer is not controlled by the joint promotion gate"
Assert-True ($source.Contains('"--decision-output", $promotionDecisionPath')) "production promotion decision is not retained as an audit manifest"
Assert-True ($source.Contains('$promotionExit -eq 3')) "a valid no-promotion decision is not handled without mutating production"
Assert-True ($source.Contains('$promotionResearchOnly = $rdUniverse -ne "csi300"')) "non-csi300 research batches can overwrite the global csi300 champion"
Assert-True ($source.Contains('RDAGENT_AUTO_SOTA_PROMOTION')) "production promotion commit is not protected by an explicit rollout flag"
Assert-True ($source.Contains('if ($autoPromotionCommit) { $promotionArgs += "--commit" }')) "production promoter cannot stay in default shadow mode"
Assert-True ($source.Contains('evaluate_rdagent_pareto_queue.ps1')) "CSI300 mining still ignores non-final Pareto workspaces"
Assert-True ($source.Contains('RDAGENT_PARETO_MAX_CANDIDATES')) "Pareto evaluation has no bounded candidate cap"
Assert-True ($source.Contains('$mineSyncExit = $LASTEXITCODE')) "mining data sync exit code is not captured"
Assert-True ($source.Contains('if ($mineSyncExit -ge 8)')) "mining continues after a failed robocopy"
Assert-True ($source.Contains('$csi300BuildLog = "C:\rdagent\_build_csi300.log"')) "CSI 300 rebuild does not retain a diagnostic log"
Assert-True ($source.Contains('& "D:\anaconda3\python.exe" -u "C:\rdagent\build_csi300.py"')) "CSI 300 rebuild does not use the pinned Python runtime"
Assert-True ($source.Contains('for ($buildAttempt = 1; $buildAttempt -le 2; $buildAttempt++)')) "CSI 300 rebuild is not retried after a transient failure"
Assert-True ([regex]::IsMatch($source, 'Write-RdStatus "error" "mine\[\$trackName\].*\$csi300BuildLog"')) "CSI 300 rebuild failure does not identify its diagnostic log"
Assert-True (-not $source.Contains('更新凭据后下次任务会自动重跑')) "credential errors must not promise a retry after deleting the request"
Assert-True ($source.Contains('$autoRefreshRetryBaseMinutes = 15')) "automatic refresh retry must have a nonzero base delay"
Assert-True ($source.Contains('$autoRefreshRetryCapMinutes = 360')) "automatic refresh retry must have a bounded delay"
Assert-True ($source.Contains('next_retry_at')) "automatic refresh failures must persist their next retry time"
Assert-True ([regex]::Matches($source, '\$proj\\scripts\\export_earnings_announcement_times\.py').Count -eq 1) "all earnings refresh paths must use the single publish-before-backtest helper"
Assert-True ($source.Contains('backfill_earnings_event_times.py" --data-dir $shared')) "event backfill checkpoints must be written atomically in the shared data directory"
Assert-True ($source.Contains('$script:rdStatusRequestId = $rdRequestId')) "RD-Agent status must be correlated with the request being processed"
Assert-True ($source.Contains('$script:rdStatusAttemptId = [guid]::NewGuid().ToString("N")')) "each mining launch must have a unique attempt id"
Assert-True ($source.Contains('$env:RDAGENT_PROGRESS_REQUEST_ID = $script:rdStatusRequestId')) "mining progress publisher must inherit the active request id"
Assert-True ($source.Contains('$env:RDAGENT_PROGRESS_REQUESTED_AT = $script:rdStatusRequestedAt')) "mining progress publisher must inherit the original request timestamp"
Assert-True ($source.Contains('$env:RDAGENT_PROGRESS_ATTEMPT_ID = $script:rdStatusAttemptId')) "mining progress publisher must inherit the active attempt id"
Assert-True ($source.Contains('$env:RDAGENT_PROGRESS_LOG_PATH = $mineLog')) "mining progress publisher must receive one assigned log"
Assert-True ($source.Contains('$env:RDAGENT_PROGRESS_STDOUT_LOG_PATH = $mineStdoutLog')) "mining progress publisher must receive the assigned stdout log"
Assert-True ($source.Contains('$env:RDAGENT_PROGRESS_LEASE_PATH = $progressLease')) "mining progress publisher must receive an attempt lease"
Assert-True ($source.Contains('$progressLease = "$mineLog.$($script:rdStatusAttemptId).running"')) "attempt leases must not collide"
Assert-True ($source.Contains('Remove-Item Env:\RDAGENT_PROGRESS_REQUEST_ID')) "temporary progress request id must be removed from the watcher environment"
foreach ($name in @("ATTEMPT_ID", "LOG_PATH", "STDOUT_LOG_PATH", "LEASE_PATH", "OWNER_PID")) {
  Assert-True ($source.Contains("Remove-Item Env:\RDAGENT_PROGRESS_$name")) "temporary progress $name must be removed from the watcher environment"
}
Assert-True ($source.Contains('-WindowStyle Hidden -PassThru')) "watcher must capture the exact progress publisher process"
Assert-True ($source.Contains('Remove-Item -LiteralPath $progressLease')) "attempt lease must be removed in cleanup"
Assert-True ($source.Contains('Stop-Process -Id $progressPublisher.Id')) "stale publisher cleanup must target only this attempt"
Assert-True ($source.IndexOf('$runStamp = Get-Date -Format yyyyMMdd_HHmmss') -lt $source.IndexOf('$logPath = "C:\rdagent\log\${prefix}_$runStamp"')) "trace and console log must share one run timestamp"
Assert-True ($source.IndexOf('if (Test-RdagentMiningProcess)') -lt $source.IndexOf('Start-Process -FilePath "D:\anaconda3\Scripts\rdagent.exe"')) "existing miner guard must run before launching fin_factor"
Assert-True ($source.Contains('-RedirectStandardError $mineLog')) "mining supervisor must retain loguru/tqdm stderr in the canonical mining log"
Assert-True ($source.Contains('-RedirectStandardOutput $mineStdoutLog')) "mining stdout must be retained separately"
Assert-True ($source.Contains('$mineLogPaths = @(@($mineLog, $mineStdoutLog)')) "terminal mining classification must inspect both console streams"
Assert-True ($source.Contains('$hasMineLogs = $mineLogPaths.Count -gt 0')) "empty console log sets must be handled without Select-String binding errors"
Assert-True ($source.Contains('Select-String -LiteralPath $mineLogPaths')) "terminal mining evidence must come from both console streams"
Assert-True ($source.Contains('Start Loop\s+\d+\s*,\s*Step\s+2\s*:\s*running|Combined Results:')) "current RD-Agent running markers must be recognized"
Assert-True ($source.Contains('$failureAge -ge 180 -and $quietAge -ge 180')) "worker-pipe termination must require a persistent signature and three quiet minutes"
Assert-True ($source.Contains('Test-RdagentMiningAttemptIdentity')) "worker-pipe termination must fail closed on exact attempt identity"
Assert-True ($source.Contains('Stop-RdagentMiningAttempt -ProcessId $mineProcess.Id')) "worker-pipe termination must target only the current attempt PID"
Assert-True ($source.Contains('$mineExit = 74')) "supervised worker-pipe termination must remain a nonzero partial run"

$supervisorSource = [System.IO.File]::ReadAllText($supervisor)
foreach ($needle in @('Process SpawnPoolWorker-', 'multiprocessing\\connection\.py', '_get_more_data', 'assert left > 0', 'AssertionError')) {
  Assert-True ($supervisorSource.Contains($needle)) "worker-pipe signature is missing $needle"
}
Assert-True ($supervisorSource.Contains("[string]`$process.Name -ine 'rdagent.exe'")) "attempt identity must require the rdagent wrapper"
Assert-True ($supervisorSource.Contains('[int]$process.ParentProcessId -ne $WatcherProcessId')) "attempt identity must require the watcher as direct parent"
Assert-True ($supervisorSource.Contains("[string]`$status.state -ne 'running'")) "attempt identity must require a running status lease"
Assert-True ($supervisorSource.Contains('& $taskkill /PID $ProcessId /T /F')) "termination must use one exact PID tree"

$signatureLog = Join-Path ([System.IO.Path]::GetTempPath()) "rdagent_pipe_signature_$PID.log"
try {
  @'
Process SpawnPoolWorker-7:
Traceback (most recent call last):
  File "D:\anaconda3\Lib\multiprocessing\connection.py", line 353, in _get_more_data
    assert left > 0
AssertionError
'@ | Set-Content -LiteralPath $signatureLog -Encoding UTF8
  $pipeState = Get-RdagentMiningLogState -Path $signatureLog
  Assert-True ($pipeState.PipeFailure) "complete Windows pipe signature must be detected"
  Assert-True (-not $pipeState.RecoveredAfterFailure) "a raw worker failure must not be marked recovered"
  Add-Content -LiteralPath $signatureLog -Encoding UTF8 -Value '2026-07-19 01:02:03.000 | INFO     | rdagent.test:resume:1 - recovered'
  $recoveredState = Get-RdagentMiningLogState -Path $signatureLog
  Assert-True ($recoveredState.RecoveredAfterFailure) "a later healthy RD-Agent line must cancel termination"
  Set-Content -LiteralPath $signatureLog -Encoding UTF8 -Value "AssertionError"
  Assert-True (-not (Get-RdagentMiningLogState -Path $signatureLog).PipeFailure) "a lone AssertionError must never terminate mining"
} finally {
  Remove-Item -LiteralPath $signatureLog -Force -ErrorAction SilentlyContinue
}

$rdStatusDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Write-RdStatus"
  }, $true)
Assert-True ($null -ne $rdStatusDefinition) "missing Write-RdStatus"
Assert-True ($rdStatusDefinition.Extent.Text -match 'request_id') "RD-Agent status does not publish request_id"
Assert-True ($rdStatusDefinition.Extent.Text -match 'attempt_id') "RD-Agent status does not publish attempt_id"
Assert-True ($rdStatusDefinition.Extent.Text -match 'Write-JsonAtomic') "RD-Agent status publication is not atomic"

$miningProcessDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Test-RdagentMiningProcess"
  }, $true)
Assert-True ($null -ne $miningProcessDefinition) "missing Test-RdagentMiningProcess"
Assert-True ($miningProcessDefinition.Extent.Text -match 'fin_factor') "miner guard must match fin_factor command lines"
Assert-True ($miningProcessDefinition.Extent.Text -match 'python\.exe') "miner guard must detect an orphaned Python main process"

$transferDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Invoke-TransferEventsIncremental"
  }, $true)
Assert-True ($null -ne $transferDefinition) "missing transfer refresh function"
Assert-True ($transferDefinition.Extent.Text -match 'Set-AutoRefreshState.+"error"') "transfer auto failures must persist retry state"
Assert-True ($transferDefinition.Extent.Text -match 'Try-AcquireProcessLockFile\s+\$transferDocumentsLockFile') "transfer watcher must share the scheduled production mutex"
Assert-True ($transferDefinition.Extent.Text -match 'finally\s*\{[\s\S]*Release-ProcessLockFile\s+\$transferDocumentsLockFile') "transfer watcher must release the production mutex on every return path"
$transferText = $transferDefinition.Extent.Text
$transferEnrichIndex = $transferText.IndexOf('enrich_transfer_terms.py')
$transferPublishIndex = $transferText.IndexOf('Publish-PlacementFileSet')
Assert-True ($transferPublishIndex -gt $transferEnrichIndex) "transfer and overlay must publish only after both outputs are complete"
Assert-True ($transferText -match '"cninfo_transfer\.json"\s+"transfer"[\s\S]*"transfer_terms_overlay\.json"\s+"overlay"') "transfer watcher must publish the announcement and overlay as one rollback bundle"

$placementPublishDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Publish-PlacementFileSet"
  }, $true)
$placementRefreshDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Invoke-PlacementEventsRefresh"
  }, $true)
$placementAutoDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Invoke-PlacementEventsAutoIfDue"
  }, $true)
Assert-True ($null -ne $placementPublishDefinition -and $null -ne $placementRefreshDefinition -and $null -ne $placementAutoDefinition) "missing placement refresh functions"
$placementPublishText = $placementPublishDefinition.Extent.Text
$placementRefreshText = $placementRefreshDefinition.Extent.Text
$placementAutoText = $placementAutoDefinition.Extent.Text
Assert-True ($placementRefreshText -match 'Set-AutoRefreshState\s+\$placementAutoFile.+"error"') "placement auto failures must persist exponential retry state"
Assert-True ($placementRefreshText -match 'Try-AcquireProcessLockFile\s+\$placementDocumentsLockFile') "placement watcher must share the scheduled production mutex"
Assert-True ($placementRefreshText -match 'finally\s*\{[\s\S]*Release-ProcessLockFile\s+\$placementDocumentsLockFile') "placement watcher must release the production mutex on every return path"
Assert-True ($placementAutoText -match 'Read-AutoRefreshState\s+\$placementAutoFile') "placement scheduling must read the shared auto-refresh state schema"
Assert-True ($placementAutoText -match 'last_success_slot') "placement scheduling must suppress only a successful slot"
Assert-True ($placementAutoText -match 'Test-AutoRefreshRetryReady\s+\$state') "placement scheduling must retry failures after backoff"
Assert-True (-not ($placementAutoText -match 'last_attempt_slot')) "placement failures must not be permanently suppressed as attempted"
Assert-True (-not ($source.Contains('function Write-PlacementAutoState'))) "placement must not keep a divergent legacy auto-state writer"
Assert-True ($placementPublishText -match 'asset_injection\.json') "placement grouped publish is missing the asset seed"
Assert-True ($placementPublishText -match 'cninfo_placement\.json') "placement grouped publish is missing the lifecycle output"
Assert-True ($placementPublishText -match '\.stage') "placement grouped publish must stage both outputs"
Assert-True ($placementPublishText -match '\.backup') "placement grouped publish must preserve rollback copies"
Assert-True ($placementPublishText -match 'Test-PlacementJson\s+\$entry\.Stage\s+\$entry\.Kind') "placement grouped publish must validate staged outputs"
Assert-True ($placementPublishText -match 'for \(\$index = \$entries\.Count - 1; \$index -ge 0; \$index--\)') "placement grouped publish must roll back in reverse order"
Assert-True ($placementPublishText -match 'Move-Item\s+-LiteralPath\s+\$entry\.Backup\s+-Destination\s+\$entry\.Destination') "placement grouped publish must atomically restore committed files"
$placementPublishIndex = $placementRefreshText.IndexOf('Publish-PlacementFileSet $assetPath $placementPath $shared')
$placementDoneIndex = $placementRefreshText.IndexOf('Set-AutoRefreshState $placementAutoFile $autoSlot "done"')
Assert-True ($placementPublishIndex -ge 0 -and $placementPublishIndex -lt $placementDoneIndex) "placement state must become done only after the complete pair is published"

$placementJsonDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Test-PlacementJson"
  }, $true)
Assert-True ($null -ne $placementJsonDefinition) "missing placement JSON validator"
Invoke-Expression $placementJsonDefinition.Extent.Text
Invoke-Expression $placementPublishDefinition.Extent.Text

$placementPublishRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("watcher_placement_publish_" + [guid]::NewGuid().ToString("N"))
$placementSourceRoot = Join-Path $placementPublishRoot "source"
$placementDestinationRoot = Join-Path $placementPublishRoot "shared"
$assetSource = Join-Path $placementSourceRoot "asset_injection.json"
$lifecycleSource = Join-Path $placementSourceRoot "cninfo_placement.json"
$assetDestination = Join-Path $placementDestinationRoot "asset_injection.json"
$lifecycleDestination = Join-Path $placementDestinationRoot "cninfo_placement.json"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$oldAssetJson = '{"updated":"old-asset","items":[{"code":"000001"}]}'
$oldLifecycleJson = '{"updated":"old-lifecycle","count":1,"items":[{"code":"000001"}],"errors":[]}'
$newAssetJson = '{"updated":"new-asset","items":[{"code":"000002"}]}'
$newLifecycleJson = '{"updated":"new-lifecycle","count":1,"items":[{"code":"000002"}],"errors":[]}'
try {
  New-Item -ItemType Directory -Force -Path $placementSourceRoot, $placementDestinationRoot | Out-Null
  [System.IO.File]::WriteAllText($assetSource, $newAssetJson, $utf8NoBom)
  [System.IO.File]::WriteAllText($lifecycleSource, $newLifecycleJson, $utf8NoBom)
  [System.IO.File]::WriteAllText($assetDestination, $oldAssetJson, $utf8NoBom)
  [System.IO.File]::WriteAllText($lifecycleDestination, $oldLifecycleJson, $utf8NoBom)
  $oldAssetBytes = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($assetDestination))
  $oldLifecycleBytes = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($lifecycleDestination))

  $script:placementStageCommits = 0
  function Move-Item {
    [CmdletBinding()]
    param(
      [Parameter(Mandatory = $true)][string]$LiteralPath,
      [Parameter(Mandatory = $true)][string]$Destination,
      [switch]$Force
    )
    if ($LiteralPath.EndsWith(".stage", [System.StringComparison]::OrdinalIgnoreCase)) {
      $script:placementStageCommits += 1
      if ($script:placementStageCommits -eq 2) {
        throw "injected second placement commit failure"
      }
    }
    Microsoft.PowerShell.Management\Move-Item -LiteralPath $LiteralPath -Destination $Destination -Force -ErrorAction Stop
  }

  $published = Publish-PlacementFileSet $assetSource $lifecycleSource $placementDestinationRoot
  Assert-True (-not $published) "placement grouped publish must report a partial commit failure"
  Assert-True ($script:placementStageCommits -eq 2) "placement rollback test did not reach the second staged commit"
  $restoredAssetBytes = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($assetDestination))
  $restoredLifecycleBytes = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($lifecycleDestination))
  Assert-True ($restoredAssetBytes -eq $oldAssetBytes) "placement rollback left a new asset file paired with the old lifecycle"
  Assert-True ($restoredLifecycleBytes -eq $oldLifecycleBytes) "placement rollback changed the old lifecycle bytes"
} finally {
  Remove-Item Function:\Move-Item -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $placementPublishRoot) {
    Microsoft.PowerShell.Management\Remove-Item -LiteralPath $placementPublishRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$earningsDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Invoke-EarningsTimesIncremental"
  }, $true)
Assert-True ($null -ne $earningsDefinition) "missing earnings refresh function"
Assert-True ($earningsDefinition.Extent.Text -match 'freshLocal') "earnings export must reject an unchanged local output"
Assert-True ($earningsDefinition.Extent.Text -match 'Publish-FileAtomic\s+\$localPath') "fresh local earnings data must be published before use"
Assert-True (-not ($earningsDefinition.Extent.Text -match 'Get-DataOutput')) "earnings export must not select an older shared output"
$earningsText = $earningsDefinition.Extent.Text
Assert-True ([regex]::Matches($earningsText, 'Try-AcquireProcessLockFile\s+\$earningsAnnouncementsLockFile').Count -eq 2) "earnings refresh must use the backfill mutex for both seed and publish"
Assert-True ($earningsText -match 'lock-busy-before-export') "earnings refresh must fail closed when the seed lock is busy"
Assert-True ($earningsText -match 'lock-busy-before-publish') "earnings refresh must fail closed when the publish lock is busy"
Assert-True ($earningsText -match '\$sharedCurrent\s+-ne\s+\$sharedBaseline') "earnings refresh must reject a shared snapshot changed by backfill"

$seedAcquireIndex = $earningsText.IndexOf('Try-AcquireProcessLockFile $earningsAnnouncementsLockFile "watcher-earnings-seed"')
$seedReleaseIndex = $earningsText.IndexOf('Release-ProcessLockFile $earningsAnnouncementsLockFile $seedLock')
$exporterIndex = $earningsText.IndexOf('export_earnings_announcement_times.py')
$publishAcquireIndex = $earningsText.IndexOf('Try-AcquireProcessLockFile $earningsAnnouncementsLockFile "watcher-earnings-publish"')
$compareIndex = $earningsText.IndexOf('$sharedCurrent -ne $sharedBaseline')
$publishIndex = $earningsText.IndexOf('Publish-FileAtomic $localPath $sharedPath')
$publishReleaseIndex = $earningsText.IndexOf('Release-ProcessLockFile $earningsAnnouncementsLockFile $publishLock')
Assert-True ($seedAcquireIndex -ge 0 -and $seedAcquireIndex -lt $seedReleaseIndex) "earnings seed mutex is not released"
Assert-True ($seedReleaseIndex -lt $exporterIndex -and $exporterIndex -lt $publishAcquireIndex) "slow earnings exporter must run outside both short mutex sections"
Assert-True ($publishAcquireIndex -lt $compareIndex -and $compareIndex -lt $publishIndex -and $publishIndex -lt $publishReleaseIndex) "earnings publish must compare-and-swap while holding the mutex"

$lockRecoveryDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Try-RecoverStaleProcessLockFile"
  }, $true)
$lockAcquireDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Try-AcquireProcessLockFile"
  }, $true)
$lockReleaseDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Release-ProcessLockFile"
  }, $true)
Assert-True ($null -ne $lockRecoveryDefinition -and $null -ne $lockAcquireDefinition -and $null -ne $lockReleaseDefinition) "missing watcher process-lock helpers"
Assert-True ($lockRecoveryDefinition.Extent.Text -match '\.reclaim') "watcher stale recovery must share Python's reclaim guard"
Assert-True ($lockRecoveryDefinition.Extent.Text -match '\.Lock\(0, 1\)') "watcher stale recovery must take an OS byte-range guard"
Assert-True ($lockRecoveryDefinition.Extent.Text -match 'Get-Process\s+-Id\s+\$ownerPid') "watcher stale recovery must preserve a live same-host owner"
Assert-True ($lockAcquireDefinition.Extent.Text -match 'FileMode\]::CreateNew') "watcher lock acquisition must atomically create the shared lock file"
Assert-True ($lockAcquireDefinition.Extent.Text -match 'Try-RecoverStaleProcessLockFile\s+\$path') "watcher lock acquisition must recover an orphaned cross-language lock"
Assert-True (-not ($lockAcquireDefinition.Extent.Text -match 'Remove-Item')) "watcher lock acquisition must not delete unknown or active Python locks"
Assert-True ($lockReleaseDefinition.Extent.Text -match '\$current\.token\s+-eq\s+\[string\]\$owner\.token') "watcher must release only its own tokenized lock"

Invoke-Expression $lockRecoveryDefinition.Extent.Text
Invoke-Expression $lockAcquireDefinition.Extent.Text
Invoke-Expression $lockReleaseDefinition.Extent.Text
$processLockRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("watcher_process_lock_" + [guid]::NewGuid().ToString("N"))
$processLockPath = Join-Path $processLockRoot "earnings.lock"
try {
  New-Item -ItemType Directory -Force -Path $processLockRoot | Out-Null
  $deadOwner = @{
    pid = -1
    host = [Environment]::MachineName
    token = "dead-owner"
    created_at = (Get-Date).ToString("s")
  }
  [System.IO.File]::WriteAllText($processLockPath, ($deadOwner | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))
  $recoveredOwner = Try-AcquireProcessLockFile $processLockPath "test-recovery"
  Assert-True ($null -ne $recoveredOwner) "watcher did not recover a dead same-host lock"
  Assert-True ([string]$recoveredOwner.token -ne "dead-owner") "watcher reused the stale owner token"
  Release-ProcessLockFile $processLockPath $recoveredOwner

  $liveOwner = @{
    pid = [int]$PID
    host = [Environment]::MachineName
    token = "live-long-owner"
    created_at = (Get-Date).AddHours(-13).ToString("s")
  }
  [System.IO.File]::WriteAllText($processLockPath, ($liveOwner | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))
  (Get-Item -LiteralPath $processLockPath).LastWriteTime = (Get-Date).AddHours(-13)
  $mustStayBusy = Try-AcquireProcessLockFile $processLockPath "must-not-steal-live"
  Assert-True ($null -eq $mustStayBusy) "watcher stole an old but live same-host lock"
  $preservedOwner = Get-Content -LiteralPath $processLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-True ([string]$preservedOwner.token -eq "live-long-owner") "watcher changed the live owner lock"
} finally {
  if (Test-Path -LiteralPath $processLockRoot) {
    Microsoft.PowerShell.Management\Remove-Item -LiteralPath $processLockRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$rollingDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Invoke-RollingEarningsBacktest"
  }, $true)
Assert-True ($null -ne $rollingDefinition) "missing synchronized rolling earnings backtest runner"
Assert-True ($rollingDefinition.Extent.Text -match '--lock-file') "manual rolling backtests must use the shared lock"
Assert-True ($rollingDefinition.Extent.Text -match '--status-file') "manual rolling backtests must publish completion status"

foreach ($helperName in @("Write-JsonAtomic", "Read-AutoRefreshState", "Get-AutoRefreshRetryMinutes", "Set-AutoRefreshState", "Test-AutoRefreshRetryReady")) {
  $helperDefinition = $ast.Find({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $helperName
    }, $true)
  Assert-True ($null -ne $helperDefinition) "missing automatic refresh helper $helperName"
  Invoke-Expression $helperDefinition.Extent.Text
}
$autoRefreshRetryBaseMinutes = 15
$autoRefreshRetryCapMinutes = 360
$autoStateRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("watcher_auto_state_" + [guid]::NewGuid().ToString("N"))
$autoStatePath = Join-Path $autoStateRoot "state.json"
try {
  [void](Set-AutoRefreshState $autoStatePath "slot-a" "running" "test")
  [void](Set-AutoRefreshState $autoStatePath "slot-a" "error" "test-error-1")
  $firstFailure = Read-AutoRefreshState $autoStatePath
  Assert-True ([int]$firstFailure.failure_count -eq 1) "first automatic refresh failure was not recorded"
  Assert-True ([datetime]::Parse([string]$firstFailure.next_retry_at) -gt (Get-Date)) "first failure does not defer the next retry"
  [void](Set-AutoRefreshState $autoStatePath "slot-a" "running" "test")
  [void](Set-AutoRefreshState $autoStatePath "slot-a" "error" "test-error-2")
  $secondFailure = Read-AutoRefreshState $autoStatePath
  Assert-True ([int]$secondFailure.failure_count -eq 2) "automatic refresh failure count does not increase"
  Assert-True ((Get-AutoRefreshRetryMinutes 99) -eq 360) "automatic refresh backoff is not capped"
  [void](Set-AutoRefreshState $autoStatePath "slot-a" "done" "test-success")
  $success = Read-AutoRefreshState $autoStatePath
  Assert-True ([int]$success.failure_count -eq 0) "successful refresh does not reset failure count"
  Assert-True (-not $success.next_retry_at) "successful refresh leaves a stale retry deadline"
} finally {
  Remove-Item -LiteralPath $autoStateRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$earningsAutoDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Start-RollingEarningsBacktestAuto"
  }, $true)
Assert-True ($null -ne $earningsAutoDefinition) "missing nonblocking rolling earnings backtest launcher"
Assert-True ($earningsAutoDefinition.Extent.Text -match 'Start-Process') "automatic rolling earnings backtest must not block the request loop"
Assert-True ($earningsAutoDefinition.Extent.Text -match 'WindowStyle\s+Hidden') "automatic rolling earnings backtest must run hidden"
Assert-True ($earningsAutoDefinition.Extent.Text -match '--lock-file') "automatic rolling backtest must share the manual mutex"
Assert-True ($earningsAutoDefinition.Extent.Text -match '--status-file') "automatic rolling backtest must publish status"

$definition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Test-RdagentMiningPreflight"
  }, $true)
Assert-True ($null -ne $definition) "missing Test-RdagentMiningPreflight"
Invoke-Expression $definition.Extent.Text

$gatewayDefinition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Test-RdagentModelGateway"
  }, $true)
Assert-True ($null -ne $gatewayDefinition) "missing Test-RdagentModelGateway"
Assert-True ($gatewayDefinition.Extent.Text -match 'CHAT_FALLBACK_MODELS') "gateway preflight must inspect configured fallbacks"
Assert-True ($gatewayDefinition.Extent.Text -match 'CHAT_FALLBACK_\$\{number\}_API_KEY_ENV') "gateway preflight must resolve independent fallback credentials"
Assert-True ($gatewayDefinition.Extent.Text -match 'for \(\$primaryAttempt = 1; \$primaryAttempt -le 2;') "primary chat must have one bounded transient retry"
Assert-True ($gatewayDefinition.Extent.Text -match 'if \(\$primaryProbe.Ok\) \{') "healthy primary must return before probing fallbacks"
Assert-True ($gatewayDefinition.Extent.Text -match 'if \(\$fallbackProbe.Ok\) \{') "fallback chain must stop at the first healthy model"
Assert-True ($gatewayDefinition.Extent.Text -match 'RestartRecommended') "gateway preflight must return a restart recommendation"

$gatewayFunctions = @{}
foreach ($name in @(
    "Get-RdagentGatewayFailureInfo",
    "Invoke-RdagentModelsProbe",
    "Invoke-RdagentMarkerProbe",
    "Test-RdagentModelGateway",
    "Wait-RdagentModelGatewayReady"
  )) {
  $node = $ast.Find({
      param($candidate)
      $candidate -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $candidate.Name -eq $name
    }, $true)
  Assert-True ($null -ne $node) "missing $name"
  $gatewayFunctions[$name] = $node
  Invoke-Expression $node.Extent.Text
}

$failureText = $gatewayFunctions["Get-RdagentGatewayFailureInfo"].Extent.Text
$markerText = $gatewayFunctions["Invoke-RdagentMarkerProbe"].Extent.Text
$waitText = $gatewayFunctions["Wait-RdagentModelGatewayReady"].Extent.Text
Assert-True ($markerText -match '/chat/completions') "gateway preflight must verify an upstream chat response"
Assert-True ($markerText -match 'QI_GATEWAY_READY_7F4A') "gateway preflight must use a deterministic marker"
Assert-True ($markerText -match 'max_tokens\s*=\s*32') "gateway preflight must use a bounded low-cost response"
Assert-True ($markerText -match 'kimi-k3') "gateway preflight must recognize the official Kimi K3 model"
Assert-True ($markerText -match 'reasoning_effort\s*=\s*"max"') "Kimi K3 probe must request its supported reasoning mode"
Assert-True ($markerText -match 'max_completion_tokens\s*=\s*1024') "Kimi K3 probe must use a bounded completion budget"
Assert-True ($markerText -match 'probeContent.Trim\(\)\s+-ne\s+\$probeMarker') "gateway preflight must validate the marker exactly"
Assert-True (-not ($source -match 'probeContext|at least 800|max_tokens\s*=\s*512')) "gateway preflight must not generate expensive throwaway code"
Assert-True ($failureText -match 'FailureKind') "gateway failures must be classified"
Assert-True (-not ($failureText -match 'Exception\.Message|ErrorDetails|ResponseBody')) "gateway diagnostics must not expose raw provider errors"
Assert-True ($waitText -match 'TimeoutSeconds\s*=\s*30') "gateway restart readiness must be bounded to 30 seconds"
Assert-True ($source.Contains('if (-not $gateway.Ok -and $gateway.RestartRecommended)')) "caller must restart only when explicitly recommended"
Assert-True ($source.Contains('Wait-RdagentModelGatewayReady -TimeoutSeconds 30')) "caller must poll readiness after a restart"

$gatewayRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("rdagent_gateway_" + [guid]::NewGuid().ToString("N"))
$gatewayEnv = Join-Path $gatewayRoot ".env"
try {
  New-Item -ItemType Directory -Force -Path $gatewayRoot | Out-Null
  [System.IO.File]::WriteAllLines($gatewayEnv, @(
      "CHAT_OPENAI_BASE_URL=http://127.0.0.1:8045/v1",
      "CHAT_OPENAI_API_KEY=PRIMARY_TEST_KEY",
      "CHAT_MODEL=openai/primary",
      "CHAT_FALLBACK_MODELS=openai/fallback-one,openai/fallback-two",
      "CHAT_FALLBACK_1_API_KEY_ENV=CHAT_OPENAI_API_KEY",
      "CHAT_FALLBACK_1_BASE_URL_ENV=CHAT_OPENAI_BASE_URL",
      "CHAT_FALLBACK_2_API_KEY_ENV=CHAT_OPENAI_API_KEY",
      "CHAT_FALLBACK_2_BASE_URL_ENV=CHAT_OPENAI_BASE_URL"
    ))

  $script:gatewayScenario = ""
  $script:gatewayCalls = @()
  $script:modelsCalls = 0
  $script:primaryChatCalls = 0
  $script:sleepCalls = 0
  function Start-Sleep {
    param([int]$Seconds, [int]$Milliseconds)
    $script:sleepCalls += 1
  }
  function Invoke-RestMethod {
    param(
      [string]$Uri,
      [hashtable]$Headers,
      [string]$Method,
      [string]$ContentType,
      [string]$Body,
      [int]$TimeoutSec
    )
    $model = ""
    $parsedBody = $null
    if ($Body) {
      $parsedBody = $Body | ConvertFrom-Json
      $model = [string]$parsedBody.model
    }
    $script:gatewayCalls += [pscustomobject]@{ Uri = $Uri; Model = $model; Body = $parsedBody }
    if ($Uri -match '/models$') {
      $script:modelsCalls += 1
      if ($script:gatewayScenario -eq "models_transport" -or
          $script:gatewayScenario -eq "remote_models_and_primary_fail" -or
          ($script:gatewayScenario -eq "chat_transport_then_models_down" -and $script:modelsCalls -gt 1)) {
        throw [System.Net.WebException]::new("SUPERSECRET", [System.Net.WebExceptionStatus]::ConnectFailure)
      }
      return @{ data = @(@{ id = "primary" }) }
    }
    if ($model -eq "primary") {
      $script:primaryChatCalls += 1
      if ($script:gatewayScenario -eq "connection_closed_then_success" -and $script:primaryChatCalls -eq 1) {
        throw [System.Net.WebException]::new("SUPERSECRET", [System.Net.WebExceptionStatus]::ConnectionClosed)
      }
      if ($script:gatewayScenario -eq "chat_transport_then_models_down") {
        throw [System.Net.WebException]::new("SUPERSECRET", [System.Net.WebExceptionStatus]::ConnectionClosed)
      }
      if ($script:gatewayScenario -in @("fallback_success", "all_chat_fail", "remote_models_and_primary_fail")) {
        return @{ choices = @(@{ message = @{ content = "WRONG" } }) }
      }
    }
    if ($script:gatewayScenario -in @("fallback_success", "remote_models_and_primary_fail") -and $model -eq "fallback-one") {
      return @{ choices = @(@{ message = @{ content = "QI_GATEWAY_READY_7F4A" } }) }
    }
    if ($script:gatewayScenario -in @("all_chat_fail", "chat_transport_then_models_down")) {
      return @{ choices = @(@{ message = @{ content = "WRONG" } }) }
    }
    return @{ choices = @(@{ message = @{ content = "QI_GATEWAY_READY_7F4A" } }) }
  }

  $script:gatewayScenario = "primary_success"
  $script:gatewayCalls = @(); $script:modelsCalls = 0; $script:primaryChatCalls = 0; $script:sleepCalls = 0
  $result = Test-RdagentModelGateway -EnvPath $gatewayEnv
  Assert-True $result.Ok "healthy primary gateway was rejected"
  Assert-True (-not $result.Degraded) "healthy primary was marked degraded"
  Assert-True ($script:gatewayCalls.Count -eq 2) "healthy primary must only call models and primary chat"
  Assert-True (-not ($script:gatewayCalls.Model -contains "fallback-one")) "healthy primary must not call fallbacks"

  $script:gatewayCalls = @()
  $kimiResult = Invoke-RdagentMarkerProbe -BaseUrl "https://api.kimi.com/coding/v1" -ApiKey "KIMI_TEST_KEY" -Model "k3"
  Assert-True $kimiResult.Ok "Kimi K3 marker probe was rejected"
  $kimiBody = $script:gatewayCalls[-1].Body
  $kimiFields = @($kimiBody.PSObject.Properties.Name)
  Assert-True (-not ($kimiFields -contains "temperature")) "Kimi K3 probe must omit temperature"
  Assert-True (-not ($kimiFields -contains "max_tokens")) "Kimi K3 probe must omit legacy max_tokens"
  Assert-True ($kimiBody.reasoning_effort -eq "max") "Kimi K3 probe reasoning mode mismatch"
  Assert-True ([int]$kimiBody.max_completion_tokens -eq 1024) "Kimi K3 probe completion budget mismatch"

  $script:gatewayScenario = "connection_closed_then_success"
  $script:gatewayCalls = @(); $script:modelsCalls = 0; $script:primaryChatCalls = 0; $script:sleepCalls = 0
  $result = Test-RdagentModelGateway -EnvPath $gatewayEnv
  Assert-True $result.Ok "primary did not recover after an update-style connection close"
  Assert-True (-not $result.RestartRecommended) "recovered primary incorrectly requested a restart"
  Assert-True ($script:primaryChatCalls -eq 2 -and $script:sleepCalls -eq 1) "primary transient retry was not bounded correctly"

  $script:gatewayScenario = "models_transport"
  $script:gatewayCalls = @(); $script:modelsCalls = 0; $script:primaryChatCalls = 0; $script:sleepCalls = 0
  $result = Test-RdagentModelGateway -EnvPath $gatewayEnv
  Assert-True (-not $result.Ok -and $result.RestartRecommended) "repeated local models transport failure must recommend restart"
  Assert-True ($script:modelsCalls -eq 2) "models transport failure must be retried exactly once"
  Assert-True (-not ($result.Message -match 'SUPERSECRET')) "gateway result leaked a raw provider error"

  [System.IO.File]::WriteAllLines($gatewayEnv, @(
      "CHAT_OPENAI_BASE_URL=https://primary.example.invalid/v1",
      "CHAT_OPENAI_API_KEY=PRIMARY_TEST_KEY",
      "CHAT_MODEL=openai/primary",
      "CHAT_FALLBACK_MODELS=openai/fallback-one,openai/fallback-two",
      "CHAT_FALLBACK_1_API_KEY_ENV=CHAT_OPENAI_API_KEY",
      "CHAT_FALLBACK_1_BASE_URL_ENV=CHAT_OPENAI_BASE_URL",
      "CHAT_FALLBACK_2_API_KEY_ENV=CHAT_OPENAI_API_KEY",
      "CHAT_FALLBACK_2_BASE_URL_ENV=CHAT_OPENAI_BASE_URL"
    ))
  $script:gatewayScenario = "remote_models_and_primary_fail"
  $script:gatewayCalls = @(); $script:modelsCalls = 0; $script:primaryChatCalls = 0; $script:sleepCalls = 0
  $result = Test-RdagentModelGateway -EnvPath $gatewayEnv
  Assert-True ($result.Ok -and $result.Degraded) "remote /models failure must still permit a healthy chat fallback"
  Assert-True ($result.FallbackModel -eq "fallback-one") "remote /models failure selected the wrong fallback"

  [System.IO.File]::WriteAllLines($gatewayEnv, @(
      "CHAT_OPENAI_BASE_URL=http://127.0.0.1:8045/v1",
      "CHAT_OPENAI_API_KEY=PRIMARY_TEST_KEY",
      "CHAT_MODEL=openai/primary",
      "CHAT_FALLBACK_MODELS=openai/fallback-one,openai/fallback-two",
      "CHAT_FALLBACK_1_API_KEY_ENV=CHAT_OPENAI_API_KEY",
      "CHAT_FALLBACK_1_BASE_URL_ENV=CHAT_OPENAI_BASE_URL",
      "CHAT_FALLBACK_2_API_KEY_ENV=CHAT_OPENAI_API_KEY",
      "CHAT_FALLBACK_2_BASE_URL_ENV=CHAT_OPENAI_BASE_URL"
    ))

  $script:gatewayScenario = "fallback_success"
  $script:gatewayCalls = @(); $script:modelsCalls = 0; $script:primaryChatCalls = 0; $script:sleepCalls = 0
  $result = Test-RdagentModelGateway -EnvPath $gatewayEnv
  Assert-True ($result.Ok -and $result.Degraded) "healthy fallback did not permit degraded operation"
  Assert-True ($result.FallbackModel -eq "fallback-one") "wrong fallback was selected"
  Assert-True (-not ($script:gatewayCalls.Model -contains "fallback-two")) "fallback chain did not stop at first success"

  $script:gatewayScenario = "all_chat_fail"
  $script:gatewayCalls = @(); $script:modelsCalls = 0; $script:primaryChatCalls = 0; $script:sleepCalls = 0
  $result = Test-RdagentModelGateway -EnvPath $gatewayEnv
  Assert-True (-not $result.Ok -and -not $result.RestartRecommended) "chat response failures must not kill a healthy local gateway"

  $script:gatewayScenario = "chat_transport_then_models_down"
  $script:gatewayCalls = @(); $script:modelsCalls = 0; $script:primaryChatCalls = 0; $script:sleepCalls = 0
  $result = Test-RdagentModelGateway -EnvPath $gatewayEnv
  Assert-True (-not $result.Ok -and $result.RestartRecommended) "chat connection loss plus failed models recheck must recommend restart"
} finally {
  Remove-Item Function:\Invoke-RestMethod -ErrorAction SilentlyContinue
  Remove-Item Function:\Start-Sleep -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $gatewayRoot) {
    Remove-Item -LiteralPath $gatewayRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$repairScript = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "repair_rdagent_factor_mining.ps1"))
Assert-True ($repairScript.Contains('CHAT_FALLBACK_MODELS')) "fallback models must be environment-configured"
Assert-True ($repairScript.Contains('CHAT_FALLBACK_{fallback_index}_')) "fallback models must support independent endpoint credentials"
Assert-True ($repairScript.Contains('"base_url": fallback_api_base')) "fallback endpoint must override LiteLLM primary base_url"
Assert-True ($repairScript.Contains('$litellmContent = $litellmContent.Replace(')) "legacy api_base fallback must be migrated"
Assert-True ($repairScript.Contains('FileLock as _FileLock')) "LLM RPM limit must coordinate across processes"
Assert-True ($repairScript.Contains('LLM_MIN_INTERVAL_SEC')) "LLM RPM interval must be configurable"
Assert-True ($repairScript.Contains('completion_attempts = [complete_kwargs]')) "each fallback request must pass through the RPM gate"
Assert-True ($repairScript.Contains('CHAT_FALLBACK_{attempt_index}_TIMEOUT')) "fallback attempts must support bounded per-model timeouts"
Assert-True ($repairScript.Contains('def _prepare_chat_request_kwargs(')) "fresh RD-Agent repair must install the Kimi K3 request contract"
Assert-True ($repairScript.Contains('max_completion_tokens')) "fresh RD-Agent repair must translate Kimi K3 completion limits"
Assert-True ($repairScript.Contains('allowed_openai_params')) "fresh RD-Agent repair must allow K3 reasoning through older LiteLLM registries"
Assert-True ($repairScript.Contains('_supports_chat_response_schema')) "fresh RD-Agent repair must preserve K3 JSON response formatting"
Assert-True ($repairScript.Contains('Redact-ApiCredentials')) "legacy runtime backups must be credential-redacted"
Assert-True ($repairScript.Contains('"_mine_progress_pub.py"')) "fresh RD-Agent repair must deploy the progress publisher"
Assert-True ($repairScript.Contains('"resolve_sota_ws.py"')) "fresh RD-Agent repair must deploy the evaluated-workspace resolver"
Assert-True ($repairScript.Contains('"promote_production_champion.py"')) "fresh RD-Agent repair must deploy the fail-closed production promoter"
Assert-True ($repairScript.Contains('RESEARCH-ONLY continuation rule')) "repair must separate research continuation from production promotion"
Assert-True ($repairScript.Contains('install_rdagent_serial_factor_dedup.ps1')) "fresh RD-Agent repair must disable Pandarallel factor de-duplication"
Assert-True ($repairScript.Contains('install_rdagent_mp_guard.ps1')) "fresh RD-Agent repair must install the multiprocessing worker-loss guard"
Assert-True ($repairScript.Contains('chat_key_configured=')) "runtime settings logs must redact API keys"
Assert-True (-not ($repairScript -match 'nvapi-[A-Za-z0-9_-]{20,}')) "repair script must not contain provider tokens"

$mpGuardInstaller = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "install_rdagent_mp_guard.ps1"))
$mpGuardTokens = $null
$mpGuardErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseInput(
  $mpGuardInstaller,
  [ref]$mpGuardTokens,
  [ref]$mpGuardErrors
)
Assert-True ($mpGuardErrors.Count -eq 0) "multiprocessing guard installer must parse without errors"
Assert-True ($mpGuardInstaller.Contains('RDAGENT_MP_RESULT_TIMEOUT_SEC')) "multiprocessing wait must have a configurable bound"
Assert-True ($mpGuardInstaller.Contains('worker.exitcode is not None')) "multiprocessing wait must detect a lost worker"

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("rdagent_preflight_" + [guid]::NewGuid().ToString("N"))
$rdagentRoot = Join-Path $root "rdagent"
$qlibRoot = Join-Path $root "qlib"
$templateRoot = Join-Path $rdagentRoot "rdagent\scenarios\qlib\experiment\factor_template"
$instrumentRoot = Join-Path $qlibRoot "instruments"
$calendarRoot = Join-Path $qlibRoot "calendars"
try {
  New-Item -ItemType Directory -Force -Path $templateRoot, $instrumentRoot, $calendarRoot | Out-Null
  foreach ($name in @("conf_baseline.yaml", "conf_combined_factors.yaml", "conf_combined_factors_sota_model.yaml")) {
    [System.IO.File]::WriteAllText((Join-Path $templateRoot $name), "qlib_init:`n    provider_uri: `"/root/qlib_data/cn_data`"")
  }
  [System.IO.File]::WriteAllText((Join-Path $calendarRoot "day.txt"), "2026-07-09`n2026-07-10`n")
  $rows = 1..300 | ForEach-Object { "sh$($_.ToString('000000'))`t2026-01-01`t2026-07-10" }
  [System.IO.File]::WriteAllLines((Join-Path $instrumentRoot "csi300.txt"), $rows)

  $result = Test-RdagentMiningPreflight -Universe "csi300" -RdagentRoot $rdagentRoot -QlibRoot $qlibRoot
  Assert-True $result.Ok "valid preflight was rejected: $($result.Message)"

  [System.IO.File]::AppendAllText((Join-Path $instrumentRoot "csi300.txt"), "`nsh999999`t2026-01-01`t2026-07-10")
  $result = Test-RdagentMiningPreflight -Universe "csi300" -RdagentRoot $rdagentRoot -QlibRoot $qlibRoot
  Assert-True (-not $result.Ok) "301-member universe was accepted"

  [System.IO.File]::WriteAllText((Join-Path $templateRoot "conf_baseline.yaml"), "qlib_init:`n    provider_uri: `"C:/qlib_data/cn_data`"")
  $result = Test-RdagentMiningPreflight -Universe "csi300" -RdagentRoot $rdagentRoot -QlibRoot $qlibRoot
  Assert-True (-not $result.Ok) "host-only provider path was accepted"
} finally {
  $resolved = [System.IO.Path]::GetFullPath($root)
  $tempResolved = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
  if ($resolved.StartsWith($tempResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
  }
}

Write-Host "test_watch_predict_pc_mining.ps1 passed"
