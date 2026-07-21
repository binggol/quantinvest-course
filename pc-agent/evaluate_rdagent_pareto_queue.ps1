param(
  [Parameter(Mandatory = $true)][string]$ResearchManifest,
  [ValidateSet("csi300")][string]$Universe = "csi300",
  [string]$SharedRoot = "",
  [string]$WorkspaceNasRoot = "",
  [ValidateRange(1, 8)][int]$MaxCandidates = 2,
  [ValidateRange(1, 3)][int]$RetryLimit = 2,
  [switch]$PlanOnly,
  [switch]$AuditOnly
)

$ErrorActionPreference = "Stop"

function Write-JsonAtomic {
  param([string]$Path, [object]$Value)
  $parent = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
  $nonce = [guid]::NewGuid().ToString('N')
  $temp = "$Path.$PID.$nonce.tmp"
  $backup = "$Path.$PID.$nonce.bak"
  try {
    $serialized = $Value | ConvertTo-Json -Depth 16
    [System.IO.File]::WriteAllText(
      $temp,
      $serialized,
      [System.Text.UTF8Encoding]::new($false)
    )
    # Refuse to publish a truncated or non-JSON checkpoint.
    Get-Content -LiteralPath $temp -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
    if (Test-Path -LiteralPath $Path) {
      [System.IO.File]::Replace($temp, $Path, $backup)
    } else {
      [System.IO.File]::Move($temp, $Path)
    }
  } finally {
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
  }
}

function Set-ObjectProperty {
  param([object]$Object, [string]$Name, [object]$Value)
  $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}

function Test-SafeLabel([AllowNull()][string]$Value) {
  return [bool]($Value -and $Value -match '\A[A-Za-z0-9_.-]{1,64}\z')
}

function Get-WorkspaceId([AllowNull()][string]$Value) {
  if (-not $Value) { return $null }
  $match = [regex]::Match(
    $Value.Replace('\', '/'),
    '\A(?:D:/rdagent_workspace|Z:/claude/rdagent_workspace)/(?<id>[0-9a-f]{32})\z',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
  )
  if (-not $match.Success) { return $null }
  return $match.Groups['id'].Value.ToLowerInvariant()
}

function ConvertTo-ExactScreenAudit {
  param(
    [object]$Screen,
    [string]$CandidateId,
    [string]$Workspace,
    [string]$UniverseName
  )
  $workspaceId = Get-WorkspaceId $Workspace
  $screenWorkspaceId = Get-WorkspaceId ([string]$Screen.workspace)
  if (-not $workspaceId -or $screenWorkspaceId -ne $workspaceId) {
    throw 'exact screen workspace identity mismatch'
  }
  if ([string]$Screen.scope -ne 'exact_workspace' -or [string]$Screen.universe -ne $UniverseName) {
    throw 'exact screen scope/universe mismatch'
  }
  $factorRows = @($Screen.factors)
  $factorNames = @($factorRows | ForEach-Object { [string]$_.factor })
  $passedNames = @($factorRows | Where-Object { $_.pass -eq $true } | ForEach-Object { [string]$_.factor })
  if ($factorNames.Count -ne @($factorNames | Where-Object { $_ } | Sort-Object -Unique).Count -or
      [int]$Screen.screened -ne $factorRows.Count -or
      [int]$Screen.n_pass -ne $passedNames.Count) {
    throw 'exact screen factor rows/counts are inconsistent'
  }
  if ($null -ne $Screen.distinct_total -and [int]$Screen.distinct_total -ne $factorRows.Count) {
    throw 'exact screen distinct-factor count is inconsistent'
  }
  $declaredPassed = @($Screen.passed_factors | ForEach-Object { [string]$_ } | Sort-Object -Unique)
  $actualPassed = @($passedNames | Sort-Object -Unique)
  if ($declaredPassed.Count -and (Compare-Object $declaredPassed $actualPassed)) {
    throw 'exact screen passed-factor list is inconsistent'
  }
  return [ordered]@{
    status = 'valid'
    artifact = "pareto_screen_$CandidateId.json"
    evaluated_at = [string]$Screen.updated
    scope = 'exact_workspace'
    universe = $UniverseName
    workspace_id = $workspaceId
    screened = $factorRows.Count
    distinct_total = if ($null -eq $Screen.distinct_total) { $factorRows.Count } else { [int]$Screen.distinct_total }
    n_pass = $passedNames.Count
    passed_factors = $passedNames
    base_ic = $Screen.base_ic
    horizons = @($Screen.horizons)
    decay_gate = $Screen.decay_gate
    factors = $factorRows
  }
}

function Read-ExactScreenAudit {
  param([string]$CandidateId, [string]$Workspace, [string]$UniverseName)
  $path = "C:\rdagent\final\pareto_screen_$CandidateId.json"
  if (-not (Test-Path -LiteralPath $path)) { return $null }
  $screen = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
  return ConvertTo-ExactScreenAudit $screen $CandidateId $Workspace $UniverseName
}

function Publish-Workspace {
  param([string]$Workspace, [string]$NasRoot)
  $id = Get-WorkspaceId $Workspace
  if (-not $id) { throw "unsafe RD-Agent workspace: $Workspace" }
  $persistent = "Z:/claude/rdagent_workspace/$id"
  $destination = Join-Path $NasRoot $id
  if ($Workspace.StartsWith('Z:/', [System.StringComparison]::OrdinalIgnoreCase)) {
    if (-not (Test-Path -LiteralPath $destination -PathType Container)) {
      throw "persistent workspace is missing: $destination"
    }
    return $persistent
  }
  $source = [System.IO.Path]::GetFullPath($Workspace.Replace('/', '\'))
  $expectedRoot = [System.IO.Path]::GetFullPath('D:\rdagent_workspace').TrimEnd('\') + '\'
  if (-not $source.StartsWith($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "workspace escaped D:\rdagent_workspace"
  }
  foreach ($relative in @('mlruns', 'ret.pkl', 'qlib_res.csv', 'combined_factors_df.parquet')) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $relative))) {
      throw "workspace is incomplete: missing $relative"
    }
  }
  New-Item -ItemType Directory -Force -Path $NasRoot | Out-Null
  robocopy $source $destination /E /MT:8 /R:2 /W:2 /COPY:DAT /DCOPY:DAT /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "workspace persistence failed: robocopy exit $LASTEXITCODE" }
  foreach ($relative in @('ret.pkl', 'qlib_res.csv', 'combined_factors_df.parquet')) {
    if ((Get-Item -LiteralPath (Join-Path $source $relative)).Length -ne
        (Get-Item -LiteralPath (Join-Path $destination $relative)).Length) {
      throw "workspace persistence verification failed: $relative"
    }
  }
  return $persistent
}

function Set-QueueItem {
  param([object]$State, [object]$Item)
  $rows = @($State.items | Where-Object { [string]$_.candidate_id -ne [string]$Item.candidate_id })
  $State.items = @(@($rows) + @($Item) | Sort-Object `
    @{ Expression = { if ($null -eq $_.rank) { [int]::MaxValue } else { [int]$_.rank } }; Descending = $false }, `
    @{ Expression = { [string]$_.candidate_id }; Descending = $false })
  $State.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
}

$manifestPath = [System.IO.Path]::GetFullPath($ResearchManifest)
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.kind -ne 'rdagent_accepted_research_candidates') {
  throw "unexpected research manifest kind"
}
$researchTraceName = [System.IO.Path]::GetFileName(([string]$manifest.trace).Replace('\', '/'))
if ($researchTraceName -notmatch '\A(?:mine|minefund)_(?:(?:csi300|csi500|csi1000)_)?\d{8}_\d{6}\z') {
  throw 'research manifest has an unsafe trace identity'
}
$traceToken = [System.IO.Path]::GetFileNameWithoutExtension($manifestPath)
$traceToken = ($traceToken -replace '[^A-Za-z0-9_.-]', '_')
if (-not (Test-SafeLabel $traceToken)) { $traceToken = 'pareto_' + ([guid]::NewGuid().ToString('N').Substring(0, 12)) }
$queuePath = Join-Path (Split-Path -Parent $manifestPath) "pareto_queue_$traceToken.json"

$seen = @{}
$pareto = @(
  $manifest.candidates |
    Where-Object { $_.pareto_research_candidate -eq $true } |
    Sort-Object `
      @{ Expression = { [double]$_.metrics.net_annualized_return }; Descending = $true }, `
      @{ Expression = { [double]$_.metrics.net_information_ratio }; Descending = $true }, `
      @{ Expression = { [string]$_.candidate_id }; Descending = $false } |
    Where-Object {
      $id = [string]$_.candidate_id
      $valid = $id -match '\A[0-9a-f]{64}\z' -and -not $seen.ContainsKey($id)
      if ($valid) { $seen[$id] = $true }
      $valid
    }
)

if ($PlanOnly) {
  $plan = @($pareto | Select-Object -First $MaxCandidates | ForEach-Object {
    [ordered]@{
      candidate_id = [string]$_.candidate_id
      workspace = [string]$_.workspace
      net_annualized_return = [double]$_.metrics.net_annualized_return
      net_information_ratio = [double]$_.metrics.net_information_ratio
    }
  })
  [ordered]@{
    schema_version = 1
    mode = 'plan_only'
    pareto_count = $pareto.Count
    max_candidates = $MaxCandidates
    selected = $plan
  } | ConvertTo-Json -Depth 8
  return
}

if (-not $WorkspaceNasRoot -and -not $AuditOnly) { throw 'WorkspaceNasRoot is required outside PlanOnly/AuditOnly' }
if (Test-Path -LiteralPath $queuePath) {
  $state = Get-Content -LiteralPath $queuePath -Raw | ConvertFrom-Json
} else {
  $state = [ordered]@{
    schema_version = 2
    kind = 'rdagent_pareto_evaluation_queue'
    research_manifest = $manifestPath.Replace('\', '/')
    research_trace = [string]$manifest.trace
    trace_name = $researchTraceName
    universe = $Universe
    max_candidates = $MaxCandidates
    items = @()
    created_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  }
}

if ([string]$state.kind -ne 'rdagent_pareto_evaluation_queue' -or [string]$state.universe -ne $Universe) {
  throw 'existing Pareto queue identity mismatch'
}
Set-ObjectProperty $state 'schema_version' 2
Set-ObjectProperty $state 'research_manifest' $manifestPath.Replace('\', '/')
Set-ObjectProperty $state 'research_trace' ([string]$manifest.trace)
Set-ObjectProperty $state 'trace_name' $researchTraceName
Set-ObjectProperty $state 'max_candidates' $MaxCandidates

# Record all Pareto candidates before evaluating the bounded subset.  A page can
# now distinguish "pending" from a candidate that was never discovered.
$rank = 0
foreach ($candidate in $pareto) {
  $rank += 1
  $candidateId = [string]$candidate.candidate_id
  $prior = @($state.items | Where-Object { [string]$_.candidate_id -eq $candidateId } | Select-Object -First 1)
  if (-not $prior.Count) {
    $pending = [ordered]@{
      candidate_id = $candidateId
      rank = $rank
      history_index = [int]$candidate.history_index
      workspace = ([string]$candidate.workspace).Replace('\', '/')
      metrics = $candidate.metrics
      attempts = 0
      status = 'pending'
      stage = 'pending'
      terminal_reason = ''
      exact_screen = $null
      batch = $null
      error = $null
      started_at = ''
      finished_at = ''
      updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    }
    Set-QueueItem $state $pending
    continue
  }
  $item = $prior[0]
  Set-ObjectProperty $item 'rank' $rank
  if (-not [string]$item.stage) { Set-ObjectProperty $item 'stage' ([string]$item.status) }
  if ($null -eq $item.terminal_reason) { Set-ObjectProperty $item 'terminal_reason' '' }
  if ($null -eq $item.started_at) { Set-ObjectProperty $item 'started_at' '' }
  if ($null -eq $item.finished_at) { Set-ObjectProperty $item 'finished_at' '' }
  if ($null -eq $item.exact_screen) {
    try {
      $audit = Read-ExactScreenAudit $candidateId ([string]$candidate.workspace) $Universe
    } catch {
      $audit = [ordered]@{ status = 'invalid'; error = $_.Exception.Message }
    }
    Set-ObjectProperty $item 'exact_screen' $audit
    if ([string]$item.status -eq 'no_factors' -and $audit -and
        [string]$audit.status -eq 'valid' -and [int]$audit.n_pass -eq 0) {
      Set-ObjectProperty $item 'terminal_reason' 'exact_screen_no_pass'
    }
  }
  Set-QueueItem $state $item
}
Write-JsonAtomic $queuePath $state

$eligible = @()
$batchesRunThisInvocation = @()
foreach ($candidate in $pareto) {
  $prior = @($state.items | Where-Object { [string]$_.candidate_id -eq [string]$candidate.candidate_id } | Select-Object -First 1)
  if ($prior.Count -and [string]$prior[0].status -in @('completed', 'no_factors')) { continue }
  if ($prior.Count -and [int]$prior[0].attempts -ge $RetryLimit) { continue }
  $eligible += $candidate
  if ($eligible.Count -ge $MaxCandidates) { break }
}
if ($AuditOnly) { $eligible = @() }

foreach ($candidate in $eligible) {
  $candidateId = [string]$candidate.candidate_id
  $workspace = ([string]$candidate.workspace).Replace('\', '/')
  $workspaceId = Get-WorkspaceId $workspace
  $prior = @($state.items | Where-Object { [string]$_.candidate_id -eq $candidateId } | Select-Object -First 1)
  $attempts = if ($prior.Count) { [int]$prior[0].attempts + 1 } else { 1 }
  $startedAt = if ($prior.Count -and [string]$prior[0].started_at) {
    [string]$prior[0].started_at
  } else {
    Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  }
  $item = [ordered]@{
    candidate_id = $candidateId
    rank = if ($prior.Count) { [int]$prior[0].rank } else { 0 }
    history_index = [int]$candidate.history_index
    workspace = $workspace
    metrics = $candidate.metrics
    attempts = $attempts
    status = 'running'
    stage = 'exact_screen'
    terminal_reason = ''
    exact_screen = if ($prior.Count) { $prior[0].exact_screen } else { $null }
    batch = $null
    error = $null
    started_at = $startedAt
    finished_at = ''
    updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  }
  # Persist the in-flight stage before expensive work so monitoring can
  # distinguish a healthy calculation from a stuck queue.
  Set-QueueItem $state $item
  Write-JsonAtomic $queuePath $state
  try {
    if (-not $workspaceId) { throw "candidate workspace is unsafe" }
    $batchLabel = "p$($candidateId.Substring(0, 12))"
    if (-not (Test-SafeLabel $batchLabel)) { throw "generated batch label is unsafe" }
    $batchPath = "C:\rdagent\final\batches\$batchLabel.json"
    if (Test-Path -LiteralPath $batchPath) {
      $existingBatch = Get-Content -LiteralPath $batchPath -Raw | ConvertFrom-Json
      $existingCandidateId = [string]$existingBatch.research_candidate_id
      $existingScreenWorkspaceId = Get-WorkspaceId ([string]$existingBatch.exact_screen_gate.workspace)
      if (($existingCandidateId -and $existingCandidateId -ne $candidateId) -or
          (-not $existingCandidateId -and $existingScreenWorkspaceId -ne $workspaceId)) {
        throw "stable batch-label collision for $batchLabel"
      }
      $modelResults = if (Test-Path -LiteralPath 'C:\rdagent\model_results.json') {
        Get-Content -LiteralPath 'C:\rdagent\model_results.json' -Raw | ConvertFrom-Json
      } else { $null }
      $existingResult = @($modelResults.results | Where-Object { [string]$_.key -eq "${batchLabel}::lgb" })
      if ($existingResult.Count -eq 1) {
        $item.status = 'completed'
        $item.stage = 'existing_result'
        $item.terminal_reason = 'existing_result'
        $item.workspace = [string]$existingBatch.workspace
        $item.batch = $batchLabel
        $item.finished_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        $item.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Set-QueueItem $state $item
        Write-JsonAtomic $queuePath $state
        continue
      }
    }

    $env:RDAGENT_SCREEN_UNIVERSE = $Universe
    $env:RDAGENT_SCREEN_EXACT_WORKSPACE = $workspace
    $previousErrorActionPreference = $ErrorActionPreference
    $screenLog = "C:\rdagent\_pareto_screen_$($candidateId.Substring(0,12)).log"
    try {
      # Windows PowerShell materializes native stderr as ErrorRecord objects.
      # With the script-wide Stop preference, harmless dependency warnings
      # would otherwise abort before $LASTEXITCODE can be inspected.
      $ErrorActionPreference = 'Continue'
      & 'D:\anaconda3\python.exe' 'C:\rdagent\factor_rdagent_screen.py' 60 2>&1 |
        Out-File -FilePath $screenLog -Encoding utf8
      $screenExit = $LASTEXITCODE
    } finally {
      $ErrorActionPreference = $previousErrorActionPreference
      Remove-Item Env:\RDAGENT_SCREEN_UNIVERSE -ErrorAction SilentlyContinue
      Remove-Item Env:\RDAGENT_SCREEN_EXACT_WORKSPACE -ErrorAction SilentlyContinue
    }
    if ($screenExit -ne 0 -or -not (Test-Path -LiteralPath 'C:\rdagent\rdagent_screen.json')) {
      throw "exact screen failed: exit $screenExit"
    }
    $screen = Get-Content -LiteralPath 'C:\rdagent\rdagent_screen.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    $screenAudit = ConvertTo-ExactScreenAudit $screen $candidateId $workspace $Universe
    $screenArtifact = "C:\rdagent\final\pareto_screen_$candidateId.json"
    # Publish the complete artifact before the queue checkpoint references it.
    Write-JsonAtomic $screenArtifact $screen
    $item.exact_screen = $screenAudit
    if ([int]$screen.n_pass -lt 1) {
      $item.status = 'no_factors'
      $item.stage = 'no_factors'
      $item.terminal_reason = 'exact_screen_no_pass'
      $item.finished_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
      $item.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
      Set-QueueItem $state $item
      Write-JsonAtomic $queuePath $state
      continue
    }

    $item.stage = 'factor_analysis'
    $item.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Set-QueueItem $state $item
    Write-JsonAtomic $queuePath $state
    wsl -e env "RDAGENT_SOTA_WS_OVERRIDE=$workspace" `
      'RDAGENT_FACTOR_EXACT_SCREEN_PATH=C:/rdagent/rdagent_screen.json' `
      "RDAGENT_FACTOR_EXACT_SCREEN_UNIVERSE=$Universe" `
      "RDAGENT_FACTOR_BATCH_LABEL=$batchLabel" `
      bash -lc 'source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && python factor_analysis.py'
    $analysisExit = $LASTEXITCODE
    if ($analysisExit -eq 3) {
      $item.status = 'no_factors'
      $item.stage = 'no_factors'
      $item.terminal_reason = 'factor_analysis_no_pass'
      $item.finished_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
      $item.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
      Set-QueueItem $state $item
      Write-JsonAtomic $queuePath $state
      continue
    }
    if ($analysisExit -ne 0) { throw "factor_analysis failed: exit $analysisExit" }
    if (-not (Test-Path -LiteralPath $batchPath)) { throw "factor_analysis did not create $batchPath" }

    $item.stage = 'workspace_publish'
    $item.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Set-QueueItem $state $item
    Write-JsonAtomic $queuePath $state
    $persistent = Publish-Workspace -Workspace $workspace -NasRoot $WorkspaceNasRoot
    $batchManifest = Get-Content -LiteralPath $batchPath -Raw | ConvertFrom-Json
    $batchManifest.workspace = $persistent
    $batchManifest | Add-Member -NotePropertyName research_candidate_id -NotePropertyValue $candidateId -Force
    $batchManifest | Add-Member -NotePropertyName research_trace -NotePropertyValue ([string]$manifest.trace) -Force
    $batchManifest | Add-Member -NotePropertyName research_metrics -NotePropertyValue $candidate.metrics -Force
    Write-JsonAtomic $batchPath $batchManifest

    $item.stage = 'three_seed_backtest'
    $item.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Set-QueueItem $state $item
    Write-JsonAtomic $queuePath $state
    wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; SEEDS=0,1,2 RDAGENT_UNIVERSE='$Universe' RDAGENT_MODEL=lgb RDAGENT_FACTOR_BATCH='$batchLabel' python run_model.py ) 9>/mnt/c/rdagent/.gpu_train.lock"
    if ($LASTEXITCODE -ne 0) { throw "three-seed run_model failed: exit $LASTEXITCODE" }
    $batchesRunThisInvocation += $batchLabel

    $item.status = 'completed'
    $item.stage = 'completed'
    $item.terminal_reason = 'completed'
    $item.workspace = $persistent
    $item.batch = $batchLabel
    $item.finished_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $item.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  } catch {
    $item.status = 'failed'
    $item.stage = 'failed'
    $item.terminal_reason = 'failed'
    $item.error = $_.Exception.Message
    $item.finished_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $item.updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  }
  Set-QueueItem $state $item
  Write-JsonAtomic $queuePath $state
}

$completedBatches = @(
  $state.items |
    Where-Object { $_.status -eq 'completed' -and (Test-SafeLabel ([string]$_.batch)) } |
    ForEach-Object { [string]$_.batch } |
    Sort-Object -Unique
)
$promotionExit = $null
$promotionCommitted = $false
if (-not $AuditOnly -and $completedBatches.Count) {
  foreach ($batch in $completedBatches) {
    if ($batch -in $batchesRunThisInvocation) { continue }
    wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; SEEDS=0,1,2 RDAGENT_UNIVERSE='csi300' RDAGENT_MODEL=lgb RDAGENT_FACTOR_BATCH='$batch' python run_model.py ) 9>/mnt/c/rdagent/.gpu_train.lock"
    if ($LASTEXITCODE -ne 0) { throw "candidate same-window refresh failed for ${batch}: exit $LASTEXITCODE" }
  }
  $incumbentBatch = ''
  $championPath = 'C:\rdagent\final\production_champion.json'
  if (Test-Path -LiteralPath $championPath) {
    $champion = Get-Content -LiteralPath $championPath -Raw | ConvertFrom-Json
    $incumbentBatch = [string]$champion.champion.label
    if (-not (Test-SafeLabel $incumbentBatch)) { throw 'invalid incumbent champion label' }
  }
  wsl -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && ( flock 9; SEEDS=0,1,2 RDAGENT_UNIVERSE='csi300' RDAGENT_MODEL=lgb RDAGENT_FACTOR_BATCH='$incumbentBatch' python run_model.py ) 9>/mnt/c/rdagent/.gpu_train.lock"
  if ($LASTEXITCODE -ne 0) { throw "incumbent three-seed refresh failed: exit $LASTEXITCODE" }

  $promotionArgs = @('C:\rdagent\promote_production_champion.py')
  foreach ($batch in $completedBatches) { $promotionArgs += @('--candidate-batch', $batch) }
  $promotionArgs += @('--decision-output', "C:\rdagent\final\promotion_$traceToken.json")
  $commit = ([string]$env:RDAGENT_AUTO_SOTA_PROMOTION).Trim().ToLowerInvariant() -in @('1','true','yes','on')
  if ($commit) {
    $promotionArgs += '--commit'
    $promotionCommitted = $true
  }
  & 'D:\anaconda3\python.exe' @promotionArgs
  $promotionExit = $LASTEXITCODE
  if ($promotionExit -notin @(0, 3)) { throw "production tournament failed: exit $promotionExit" }
}

if (-not $AuditOnly) {
  Push-Location 'C:\rdagent'
  try { & 'D:\anaconda3\python.exe' 'export_rdagent.py'; $exportExit = $LASTEXITCODE } finally { Pop-Location }
  if ($exportExit -ne 0) { throw "export_rdagent failed: exit $exportExit" }
}

if ($SharedRoot -and -not $AuditOnly) {
  foreach ($name in @('model_results.json', 'model_curves.json')) {
    $source = Join-Path 'C:\rdagent' $name
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $SharedRoot $name) -Force }
  }
  if (Test-Path -LiteralPath 'C:\rdagent\final\production_champion.json') {
    Copy-Item -LiteralPath 'C:\rdagent\final\production_champion.json' -Destination (Join-Path $SharedRoot 'production_champion.json') -Force
  }
}

$statusCounts = [ordered]@{}
foreach ($statusName in @('pending', 'running', 'completed', 'no_factors', 'failed')) {
  $statusCounts[$statusName] = @($state.items | Where-Object { [string]$_.status -eq $statusName }).Count
}
$validScreens = @($state.items | Where-Object { [string]$_.exact_screen.status -eq 'valid' })
$screenedFactorCount = @($validScreens | ForEach-Object { [int]$_.exact_screen.screened } | Measure-Object -Sum).Sum
$passedFactorCount = @($validScreens | ForEach-Object { [int]$_.exact_screen.n_pass } | Measure-Object -Sum).Sum
$runStatus = if ([int]$statusCounts.failed -gt 0) {
  'completed_with_failures'
} elseif ([int]$statusCounts.pending -gt 0 -or [int]$statusCounts.running -gt 0) {
  'partial'
} else {
  'completed'
}
$summary = [ordered]@{
  schema_version = 2
  kind = 'rdagent_pareto_evaluation_summary'
  trace_name = $researchTraceName
  universe = $Universe
  run_status = $runStatus
  queue_artifact = Split-Path -Leaf $queuePath
  pareto_count = $pareto.Count
  attempted_this_run = $eligible.Count
  completed_batches = $completedBatches
  candidate_counts = $statusCounts
  exact_screen = [ordered]@{
    candidate_count = $validScreens.Count
    screened_factors = if ($null -eq $screenedFactorCount) { 0 } else { [int]$screenedFactorCount }
    passed_factors = if ($null -eq $passedFactorCount) { 0 } else { [int]$passedFactorCount }
  }
  promotion = [ordered]@{
    exit = $promotionExit
    committed = $promotionCommitted
    decision_artifact = "promotion_$traceToken.json"
  }
  updated_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
}
$summaryPath = Join-Path (Split-Path -Parent $manifestPath) "pareto_summary_$traceToken.json"
Write-JsonAtomic $summaryPath $summary
if ($SharedRoot) {
  Write-JsonAtomic (Join-Path $SharedRoot 'pareto_summary_latest.json') $summary
}
$summary | ConvertTo-Json -Depth 8
