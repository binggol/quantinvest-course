param(
  [string]$RdagentRoot = "C:\rdagent"
)

$ErrorActionPreference = "Stop"
$backupRoot = Join-Path $PSScriptRoot "rdagent_backup"
$stamp = "pre_hs300_fix_20260713"

function Backup-Once {
  param([string]$Path)
  $backup = "$Path.$stamp"
  if ((Test-Path -LiteralPath $Path) -and -not (Test-Path -LiteralPath $backup)) {
    Copy-Item -LiteralPath $Path -Destination $backup -Force
  }
}

function Write-Utf8NoBom {
  param([string]$Path, [string]$Content)
  $encoding = [System.Text.UTF8Encoding]::new($false)
  $tempPath = "$Path.$PID.tmp"
  $replaceBackup = "$Path.$PID.replacebak"
  try {
    [System.IO.File]::WriteAllText($tempPath, $Content, $encoding)
    if (Test-Path -LiteralPath $Path) {
      [System.IO.File]::Replace($tempPath, $Path, $replaceBackup)
    } else {
      [System.IO.File]::Move($tempPath, $Path)
    }
  } finally {
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $replaceBackup -Force -ErrorAction SilentlyContinue
  }
}

function Redact-ApiCredentials {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) { return }
  $content = [System.IO.File]::ReadAllText($Path)
  $redacted = [regex]::Replace(
    $content,
    'nvapi-[A-Za-z0-9_-]{20,}',
    '[REDACTED_NVIDIA_API_KEY]'
  )
  $redacted = [regex]::Replace(
    $redacted,
    '(?i)(\b(?:chat_|embedding_)?openai_api_key\s*=\s*)(?:''[^'']*''|"[^"]*"|[^,\s\)]+)',
    '$1''[REDACTED_API_KEY]'''
  )
  if ($redacted -ne $content) {
    Write-Utf8NoBom $Path $redacted
  }
}

$factorTemplates = Join-Path $RdagentRoot "rdagent\scenarios\qlib\experiment\factor_template"
$modelTemplates = Join-Path $RdagentRoot "rdagent\scenarios\qlib\experiment\model_template"
$yamlPaths = @(
  (Join-Path $factorTemplates "conf_baseline.yaml"),
  (Join-Path $factorTemplates "conf_combined_factors.yaml"),
  (Join-Path $factorTemplates "conf_combined_factors_sota_model.yaml"),
  (Join-Path $modelTemplates "conf_baseline_factors_model.yaml"),
  (Join-Path $modelTemplates "conf_sota_factors_model.yaml")
)
$hostProviders = @(
  'provider_uri: "C:/qlib_data/cn_data"',
  'provider_uri: "~/.qlib/qlib_data/cn_data"'
)
$containerProvider = 'provider_uri: "/root/qlib_data/cn_data"'
foreach ($path in $yamlPaths) {
  if (-not (Test-Path -LiteralPath $path)) { throw "Missing RD-Agent YAML: $path" }
  Backup-Once $path
  $content = [System.IO.File]::ReadAllText($path)
  $changed = $false
  foreach ($hostProvider in $hostProviders) {
    if ($content.Contains($hostProvider)) {
      $content = $content.Replace($hostProvider, $containerProvider)
      $changed = $true
    }
  }
  if ($changed) {
    Write-Utf8NoBom $path $content
  } elseif (-not $content.Contains($containerProvider)) {
    throw "Unexpected provider_uri in $path"
  }
}

$factorPrompts = Join-Path $RdagentRoot "rdagent\scenarios\qlib\experiment\prompts.yaml"
Backup-Once $factorPrompts
$promptContent = [System.IO.File]::ReadAllText($factorPrompts).Replace("`r`n", "`n")
$pandasMarker = "Pandas compatibility: never pass dropna to DataFrame.stack"
if (-not $promptContent.Contains($pandasMarker)) {
  $interfaceAnchor = '  User will write your python code into a python file and execute the file directly with "python {your_file_name}.py". You should calculate the factor values and save the result into a HDF5(H5) file named "result.h5" in the same directory as your python file. The result file is a HDF5(H5) file containing a pandas dataframe. The index of the dataframe is the "datetime" and "instrument", and the single column name is the factor name,and the value is the factor value. The result file should be saved in the same directory as your python file.'
  if (-not $promptContent.Contains($interfaceAnchor)) {
    throw "Unexpected factor interface prompt in $factorPrompts"
  }
  $compatibilityRule = @'
  Pandas compatibility: never pass dropna to DataFrame.stack; call stack() without dropna, then reindex to the original MultiIndex when missing rows must be restored.
'@.TrimEnd()
  $promptContent = $promptContent.Replace(
    $interfaceAnchor,
    $interfaceAnchor + "`n" + $compatibilityRule
  )
  Write-Utf8NoBom $factorPrompts $promptContent
}

# The LLM decision remains useful for research-loop continuation, but it must not
# be interpreted as authority to overwrite the production champion.  Production
# promotion is performed later from exact OOS, cost and multi-seed artifacts.
$scenarioPrompts = Join-Path $RdagentRoot "rdagent\scenarios\qlib\prompts.yaml"
Backup-Once $scenarioPrompts
$scenarioPromptContent = [System.IO.File]::ReadAllText($scenarioPrompts).Replace("`r`n", "`n")
$researchOnlyMarker = "RESEARCH-ONLY continuation rule"
if (-not $scenarioPromptContent.Contains($researchOnlyMarker)) {
  $researchRulePattern = '(?ms)^    \*\*HARD RULES for `Replace Best Result`.*?(?=^    Consider Changing Direction for Significant Gaps with SOTA:)'
  if ([regex]::Matches($scenarioPromptContent, $researchRulePattern).Count -ne 1) {
    throw "Unexpected factor SOTA decision block in $scenarioPrompts"
  }
  $researchRule = @'
    **RESEARCH-ONLY continuation rule for `Replace Best Result` — this is not a production promotion:**

    ```
    if new_IC > SOTA_IC OR new_Rank_IC > SOTA_Rank_IC OR new_annualized_return > SOTA_annualized_return:
        Replace_Best_Result = "yes"
    else:
        Replace_Best_Result = "no"
    ```

    Apply this deterministic rule only to choose the next RD-Agent research branch.
    `Decision=True` means "accepted research candidate"; it MUST NOT update
    `sota_workspace.txt`, `effective_factors.json`, or be described as the production
    champion. Every accepted workspace is exported to an external Pareto archive so
    a predictive signal is not discarded when another research branch continues.

    Production promotion is a separate fail-closed tournament after exact-workspace
    orthogonality/decay screening, selection/test isolation, a cost-bearing OOS
    backtest and at least three seeds. It jointly gates net excess return, information
    ratio, drawdown, worst-seed return and seed dispersion against the incumbent.

'@.TrimEnd().Replace("`r`n", "`n")
  $scenarioPromptContent = [regex]::Replace(
    $scenarioPromptContent,
    $researchRulePattern,
    $researchRule + "`n`n"
  )
  Write-Utf8NoBom $scenarioPrompts $scenarioPromptContent
}

$envPy = Join-Path $RdagentRoot "rdagent\utils\env.py"
Backup-Once $envPy
$envContent = [System.IO.File]::ReadAllText($envPy).Replace("`r`n", "`n")
$oldVolumeBlock = @'
        str(Path("~/.qlib/").expanduser().resolve().absolute()): {
            "bind": "/root/.qlib/",
            "mode": "rw",
        }
'@.TrimEnd()
$oldVolumeBlock = $oldVolumeBlock.Replace("`r`n", "`n")
$newVolumeBlock = @'
        str(Path("~/.qlib/").expanduser().resolve().absolute()): {
            "bind": "/root/.qlib/",
            "mode": "rw",
        },
        str(Path(os.environ.get("QLIB_HOST_DATA_ROOT", "C:/qlib_data")).resolve().absolute()): {
            "bind": "/root/qlib_data/",
            "mode": "ro",
        }
'@.TrimEnd()
$newVolumeBlock = $newVolumeBlock.Replace("`r`n", "`n")
if (-not $envContent.Contains('"bind": "/root/qlib_data/"')) {
  if ($envContent.Contains($oldVolumeBlock)) {
    $envContent = $envContent.Replace($oldVolumeBlock, $newVolumeBlock)
  } else {
    throw "Unexpected Qlib Docker volume configuration in $envPy"
  }
}

$duplicateDataVolume = @'
        str(Path(os.environ.get("QLIB_HOST_DATA_ROOT", "C:/qlib_data")).resolve().absolute()): {
            "bind": "/root/qlib_data/",
            "mode": "ro",
        },
        str(Path(os.environ.get("QLIB_HOST_DATA_ROOT", "C:/qlib_data")).resolve().absolute()): {
            "bind": "/root/qlib_data/",
            "mode": "ro",
        }
'@.TrimEnd().Replace("`r`n", "`n")
$singleDataVolume = @'
        str(Path(os.environ.get("QLIB_HOST_DATA_ROOT", "C:/qlib_data")).resolve().absolute()): {
            "bind": "/root/qlib_data/",
            "mode": "ro",
        }
'@.TrimEnd().Replace("`r`n", "`n")
if ($envContent.Contains($duplicateDataVolume)) {
  $envContent = $envContent.Replace($duplicateDataVolume, $singleDataVolume)
}
$dataVolumeCount = [regex]::Matches(
  $envContent,
  [regex]::Escape('"bind": "/root/qlib_data/"')
).Count
if ($dataVolumeCount -ne 1) {
  throw "Expected exactly one Qlib data volume in $envPy"
}
Write-Utf8NoBom $envPy $envContent

$litellmBackend = Join-Path $RdagentRoot "rdagent\oai\backend\litellm.py"
Backup-Once $litellmBackend
Get-ChildItem -LiteralPath (Split-Path $litellmBackend -Parent) -Filter "litellm.py.*" -File |
  Where-Object { $_.FullName -ne $litellmBackend } |
  ForEach-Object { Redact-ApiCredentials $_.FullName }
$litellmContent = [System.IO.File]::ReadAllText($litellmBackend).Replace("`r`n", "`n")
$legacyRateLimiterPattern = '(?ms)^_RL_LOCK\s*=.*?^class LiteLLMSettings'
$crossProcessRateLimiterBlock = @'
_RL_LOCK = _threading.Lock()
_RL_FILE = _os.path.join(_os.path.dirname(__file__), ".llm_ratelimit")
_RL_LOCK_FILE = _RL_FILE + ".lock"


def _rate_limit_gate() -> None:
    try:
        interval = float(_os.environ.get("LLM_MIN_INTERVAL_SEC", "1.6"))
    except (TypeError, ValueError):
        interval = 1.6
    if interval <= 0:
        return

    # The provider limit applies across processes, not just threads. Serialize
    # timestamp reads and writes so concurrent workers cannot each spend 40 RPM.
    from filelock import FileLock as _FileLock

    with _RL_LOCK:
        with _FileLock(_RL_LOCK_FILE, timeout=max(60.0, interval * 4)):
            try:
                with open(_RL_FILE, encoding="ascii") as rate_file:
                    last = float(rate_file.read().strip())
            except (OSError, TypeError, ValueError):
                last = 0.0
            wait = interval - (_time.time() - last)
            if wait > 0:
                _time.sleep(wait)
            now = _time.time()
            with open(_RL_FILE, "w", encoding="ascii") as rate_file:
                rate_file.write(str(now))


class LiteLLMSettings
'@.TrimEnd().Replace("`r`n", "`n")
if ([regex]::IsMatch($litellmContent, $legacyRateLimiterPattern)) {
  $litellmContent = [regex]::Replace(
    $litellmContent,
    $legacyRateLimiterPattern,
    $crossProcessRateLimiterBlock
  )
} elseif (-not $litellmContent.Contains('_RL_LOCK_FILE = _RL_FILE + ".lock"')) {
  throw "Unexpected LiteLLM rate limiter in $litellmBackend"
}
$kimiK3AdapterBlock = @'
_KIMI_K3_FIXED_REQUEST_FIELDS = (
    "temperature",
    "top_p",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "thinking",
)


def _is_kimi_k3_model(model: Any) -> bool:
    """Return whether *model* targets the official Kimi K3 chat model."""
    return str(model or "").strip().lower().rsplit("/", 1)[-1] in {"k3", "kimi-k3"}


def _prepare_chat_request_kwargs(model: Any, request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Apply provider-specific request contracts immediately before dispatch."""
    prepared = dict(request_kwargs)
    if not _is_kimi_k3_model(model):
        return prepared
    for field in _KIMI_K3_FIXED_REQUEST_FIELDS:
        prepared.pop(field, None)
    legacy_max_tokens = prepared.pop("max_tokens", None)
    if legacy_max_tokens is not None and "max_completion_tokens" not in prepared:
        prepared["max_completion_tokens"] = legacy_max_tokens
    prepared["reasoning_effort"] = "max"
    allowed_openai_params = list(prepared.get("allowed_openai_params") or [])
    if "reasoning_effort" not in allowed_openai_params:
        allowed_openai_params.append("reasoning_effort")
    prepared["allowed_openai_params"] = allowed_openai_params
    return prepared


def _supports_chat_response_schema(model: Any) -> bool:
    return _is_kimi_k3_model(model) or supports_response_schema(model=str(model))
'@.TrimEnd().Replace("`r`n", "`n")
if (-not $litellmContent.Contains('def _prepare_chat_request_kwargs(')) {
  $settingsClassAnchor = "class LiteLLMSettings"
  if (-not $litellmContent.Contains($settingsClassAnchor)) {
    throw "Unexpected LiteLLM settings class in $litellmBackend"
  }
  $litellmContent = $litellmContent.Replace(
    $settingsClassAnchor,
    $kimiK3AdapterBlock + "`n`n" + $settingsClassAnchor
  )
}
$hardcodedFallbackPattern = '(?ms)^        _NIM_KEY\s*=.*?^        fallback_list\s*=\s*\[.*?^        \]\n'
$environmentFallbackBlock = @'
        # Optional fallbacks must use the configured gateway credentials. Never
        # embed provider tokens in source code.
        fallback_models = [
            item.strip()
            for item in _os.environ.get("CHAT_FALLBACK_MODELS", "").split(",")
            if item.strip()
        ]
        fallback_list = []
        for fallback_index, fallback_model in enumerate(fallback_models, start=1):
            env_prefix = f"CHAT_FALLBACK_{fallback_index}_"
            key_env_name = _os.environ.get(f"{env_prefix}API_KEY_ENV", "").strip()
            base_env_name = _os.environ.get(f"{env_prefix}BASE_URL_ENV", "").strip()
            fallback_api_key = (
                _os.environ.get(key_env_name) if key_env_name else complete_kwargs.get("api_key")
            )
            fallback_api_base = (
                _os.environ.get(base_env_name) if base_env_name else complete_kwargs.get("base_url")
            )
            if key_env_name and not fallback_api_key:
                raise ValueError(f"Missing fallback credential environment variable: {key_env_name}")
            if base_env_name and not fallback_api_base:
                raise ValueError(f"Missing fallback endpoint environment variable: {base_env_name}")
            fallback_list.append(
                {
                    "model": fallback_model,
                    "api_key": fallback_api_key,
                    "base_url": fallback_api_base,
                }
            )
'@.TrimEnd().Replace("`r`n", "`n")
if ([regex]::IsMatch($litellmContent, $hardcodedFallbackPattern)) {
  $litellmContent = [regex]::Replace(
    $litellmContent,
    $hardcodedFallbackPattern,
    $environmentFallbackBlock + "`n"
  )
} elseif ($litellmContent.Contains('CHAT_FALLBACK_MODELS') -and -not $litellmContent.Contains('API_KEY_ENV')) {
  $legacyEnvironmentFallbackPattern = '(?ms)^        fallback_models\s*=\s*\[.*?^        \]\n        fallback_list\s*=\s*\[.*?^        \]\n'
  if (-not [regex]::IsMatch($litellmContent, $legacyEnvironmentFallbackPattern)) {
    throw "Unexpected LiteLLM environment fallback configuration in $litellmBackend"
  }
  $litellmContent = [regex]::Replace(
    $litellmContent,
    $legacyEnvironmentFallbackPattern,
    $environmentFallbackBlock + "`n"
  )
} elseif (-not $litellmContent.Contains('CHAT_FALLBACK_MODELS')) {
  throw "Unexpected LiteLLM fallback configuration in $litellmBackend"
}
$litellmContent = $litellmContent.Replace(
  '"api_base": fallback_api_base',
  '"base_url": fallback_api_base'
)
if (-not $litellmContent.Contains('"base_url": fallback_api_base')) {
  throw "LiteLLM fallback endpoint override was not installed in $litellmBackend"
}
$duplicateFallbackComment = @'
        # Optional fallbacks must use the configured gateway credentials. Never
        # embed provider tokens in source code.
        # Optional fallbacks must use the configured gateway credentials. Never
        # embed provider tokens in source code.
'@.TrimEnd().Replace("`r`n", "`n")
$singleFallbackComment = @'
        # Optional fallbacks must use configured credentials. Never embed
        # provider tokens in source code.
'@.TrimEnd().Replace("`r`n", "`n")
$litellmContent = $litellmContent.Replace($duplicateFallbackComment, $singleFallbackComment)

$litellmContent = [regex]::Replace(
  $litellmContent,
  '(?m)^        _rate_limit_gate\(\).*$\n',
  ''
)
$fallbackCompletionPattern = '(?ms)^        response = completion\(\n            messages=messages,\n.*?^            \*\*kwargs,\n        \)\n'
$explicitFallbackCompletion = @'
        completion_attempts = [complete_kwargs]
        for fallback in fallback_list:
            attempt_kwargs = complete_kwargs.copy()
            attempt_kwargs.update(fallback)
            completion_attempts.append(attempt_kwargs)

        response = None
        last_error = None
        for attempt_index, attempt_kwargs in enumerate(completion_attempts):
            attempt_model = attempt_kwargs["model"]
            attempt_timeout = LITELLM_SETTINGS.chat_timeout
            if attempt_index > 0:
                timeout_value = _os.environ.get(f"CHAT_FALLBACK_{attempt_index}_TIMEOUT", "").strip()
                if timeout_value:
                    try:
                        attempt_timeout = float(timeout_value)
                    except ValueError:
                        logger.warning(
                            f"Ignoring invalid CHAT_FALLBACK_{attempt_index}_TIMEOUT value."
                        )
            _rate_limit_gate()
            try:
                request_kwargs = dict(attempt_kwargs)
                request_kwargs.update(kwargs)
                request_kwargs = _prepare_chat_request_kwargs(attempt_model, request_kwargs)
                response = completion(
                    messages=messages,
                    stream=LITELLM_SETTINGS.chat_stream,
                    max_retries=0,
                    timeout=attempt_timeout,
                    **request_kwargs,
                )
                model = attempt_model
                break
            except Exception as error:
                last_error = error
                if attempt_index + 1 < len(completion_attempts):
                    logger.warning(
                        f"Chat model {attempt_model} failed ({type(error).__name__}); "
                        "trying configured fallback."
                    )
        if response is None:
            if last_error is None:
                raise RuntimeError("No chat completion attempt was configured")
            raise last_error
'@.TrimEnd().Replace("`r`n", "`n")
if ([regex]::IsMatch($litellmContent, $fallbackCompletionPattern)) {
  $litellmContent = [regex]::Replace(
    $litellmContent,
    $fallbackCompletionPattern,
    $explicitFallbackCompletion + "`n"
  )
} elseif (-not $litellmContent.Contains('completion_attempts = [complete_kwargs]')) {
  throw "Unexpected LiteLLM completion fallback flow in $litellmBackend"
}
$oldAttemptTimeoutBlock = @'
            attempt_model = attempt_kwargs["model"]
            _rate_limit_gate()
'@.TrimEnd().Replace("`r`n", "`n")
$newAttemptTimeoutBlock = @'
            attempt_model = attempt_kwargs["model"]
            attempt_timeout = LITELLM_SETTINGS.chat_timeout
            if attempt_index > 0:
                timeout_value = _os.environ.get(f"CHAT_FALLBACK_{attempt_index}_TIMEOUT", "").strip()
                if timeout_value:
                    try:
                        attempt_timeout = float(timeout_value)
                    except ValueError:
                        logger.warning(
                            f"Ignoring invalid CHAT_FALLBACK_{attempt_index}_TIMEOUT value."
                        )
            _rate_limit_gate()
'@.TrimEnd().Replace("`r`n", "`n")
if ($litellmContent.Contains($oldAttemptTimeoutBlock)) {
  $litellmContent = $litellmContent.Replace($oldAttemptTimeoutBlock, $newAttemptTimeoutBlock)
}
$litellmContent = $litellmContent.Replace(
  '                    timeout=LITELLM_SETTINGS.chat_timeout,',
  '                    timeout=attempt_timeout,'
)
if (-not $litellmContent.Contains('CHAT_FALLBACK_{attempt_index}_TIMEOUT')) {
  throw "LiteLLM fallback timeout override was not installed in $litellmBackend"
}
$legacyCompletionDispatch = @'
            try:
                response = completion(
                    messages=messages,
                    stream=LITELLM_SETTINGS.chat_stream,
                    max_retries=0,
                    timeout=attempt_timeout,
                    **attempt_kwargs,
                    **kwargs,
                )
'@.TrimEnd().Replace("`r`n", "`n")
$kimiK3CompletionDispatch = @'
            try:
                request_kwargs = dict(attempt_kwargs)
                request_kwargs.update(kwargs)
                request_kwargs = _prepare_chat_request_kwargs(attempt_model, request_kwargs)
                response = completion(
                    messages=messages,
                    stream=LITELLM_SETTINGS.chat_stream,
                    max_retries=0,
                    timeout=attempt_timeout,
                    **request_kwargs,
                )
'@.TrimEnd().Replace("`r`n", "`n")
if ($litellmContent.Contains($legacyCompletionDispatch)) {
  $litellmContent = $litellmContent.Replace($legacyCompletionDispatch, $kimiK3CompletionDispatch)
}
if (-not $litellmContent.Contains('request_kwargs = _prepare_chat_request_kwargs(attempt_model, request_kwargs)')) {
  throw "Kimi K3 request adapter was not installed in $litellmBackend"
}
$litellmContent = $litellmContent.Replace(
  'if response_format and not supports_response_schema(model=LITELLM_SETTINGS.chat_model):',
  'if response_format and not _supports_chat_response_schema(LITELLM_SETTINGS.chat_model):'
)
$litellmContent = $litellmContent.Replace(
  'return supports_response_schema(model=LITELLM_SETTINGS.chat_model) and LITELLM_SETTINGS.enable_response_schema',
  'return _supports_chat_response_schema(LITELLM_SETTINGS.chat_model) and LITELLM_SETTINGS.enable_response_schema'
)
if (-not $litellmContent.Contains('_supports_chat_response_schema(LITELLM_SETTINGS.chat_model)')) {
  throw "Kimi K3 response schema support was not installed in $litellmBackend"
}

$unsafeSettingsLog = @'
            logger.info(f"{LITELLM_SETTINGS}")
            logger.log_object(LITELLM_SETTINGS.model_dump(), tag="LITELLM_SETTINGS")
'@.TrimEnd().Replace("`r`n", "`n")
$safeSettingsLog = @'
            logger.info(
                "LLM settings: "
                f"backend={LITELLM_SETTINGS.backend!r} "
                f"chat_model={LITELLM_SETTINGS.chat_model!r} "
                f"chat_key_configured={bool(LITELLM_SETTINGS.chat_openai_api_key or LITELLM_SETTINGS.openai_api_key)} "
                f"embedding_model={LITELLM_SETTINGS.embedding_model!r} "
                f"embedding_key_configured={bool(LITELLM_SETTINGS.embedding_openai_api_key or LITELLM_SETTINGS.openai_api_key)}"
            )
'@.TrimEnd().Replace("`r`n", "`n")
if ($litellmContent.Contains($unsafeSettingsLog)) {
  $litellmContent = $litellmContent.Replace($unsafeSettingsLog, $safeSettingsLog)
} elseif (-not $litellmContent.Contains('chat_key_configured=')) {
  throw "Unexpected LiteLLM settings logging in $litellmBackend"
}
Write-Utf8NoBom $litellmBackend $litellmContent

foreach ($name in @(
  "_mine_progress_pub.py",
  "build_csi300.py",
  "build_universe.py",
  "build_all_mine_history.py",
  "factor_rdagent_screen.py",
  "factor_analysis.py",
  "resolve_sota_ws.py",
  "promote_production_champion.py",
  "factor_contract.py",
  "live_topk_dropout.py",
  "prediction_preflight.py",
  "refresh_rdagent_daily_pv.py",
  "run_model.py",
  "predict_next_day.py"
)) {
  $source = Join-Path $backupRoot $name
  $target = Join-Path $RdagentRoot $name
  if (-not (Test-Path -LiteralPath $source)) { throw "Missing maintained RD-Agent script: $source" }
  Backup-Once $target
  Copy-Item -LiteralPath $source -Destination $target -Force
}

$mineEval = Join-Path $RdagentRoot "mine_eval.py"
Backup-Once $mineEval
$mineEvalContent = [System.IO.File]::ReadAllText($mineEval)
$oldSnapshotLine = '        iw_dates=sorted(iw["trade_date"].unique())'
$newSnapshotLines = @'
        iw_counts=iw.groupby("trade_date")["con_code"].nunique()
        iw_dates=sorted(iw_counts[iw_counts == 300].index)
'@.TrimEnd()
if ($mineEvalContent.Contains($oldSnapshotLine)) {
  $mineEvalContent = $mineEvalContent.Replace($oldSnapshotLine, $newSnapshotLines)
  Write-Utf8NoBom $mineEval $mineEvalContent
} elseif (-not $mineEvalContent.Contains('iw_counts=iw.groupby("trade_date")["con_code"].nunique()')) {
  throw "Unexpected CSI 300 snapshot logic in $mineEval"
}

$factorRunner = Join-Path $RdagentRoot "rdagent\scenarios\qlib\developer\factor_runner.py"
Backup-Once $factorRunner
$factorRunnerContent = [System.IO.File]::ReadAllText($factorRunner)
$positiveCorrelationOnly = '        IC_max = IC_max.unstack().max(axis=0)'
$absoluteCorrelation = '        IC_max = IC_max.unstack().abs().max(axis=0)'
if ($factorRunnerContent.Contains($positiveCorrelationOnly)) {
  $factorRunnerContent = $factorRunnerContent.Replace($positiveCorrelationOnly, $absoluteCorrelation)
} elseif (-not $factorRunnerContent.Contains($absoluteCorrelation)) {
  throw "Unexpected factor deduplication logic in $factorRunner"
}
$globalDropna = '                combined_factors = pd.concat([SOTA_factor, new_factors], axis=1).dropna()'
$pairwiseFriendlyConcat = '                combined_factors = pd.concat([SOTA_factor, new_factors], axis=1)'
if ($factorRunnerContent.Contains($globalDropna)) {
  $factorRunnerContent = $factorRunnerContent.Replace($globalDropna, $pairwiseFriendlyConcat)
} elseif (-not $factorRunnerContent.Contains($pairwiseFriendlyConcat)) {
  throw "Unexpected combined-factor missing-value logic in $factorRunner"
}
Write-Utf8NoBom $factorRunner $factorRunnerContent

$runModel = Join-Path $RdagentRoot "run_model.py"
$factorContract = Join-Path $RdagentRoot "factor_contract.py"
Backup-Once $runModel
$runModelContent = [System.IO.File]::ReadAllText($runModel).Replace("`r`n", "`n")
$oldRunModelBlock = @'
    cfg_ws = ws   # 默认: config 直接在本 workspace 找到时, 工作目录=ws (fallback 分支会覆盖成全局SOTA)
    config_candidates = list(ws.glob("mlruns/*/*/artifacts/config"))
    if len(config_candidates) != 1:
        ptr = Path("/mnt/c/rdagent/sota_workspace.txt")
        global_sota_str = ptr.read_text(encoding="utf-8").strip() if ptr.exists() else ""
        if global_sota_str and _to_wsl(global_sota_str) != str(ws):
            print(f"[run_model] New workspace has no mlruns, falling back to global SOTA for config: {global_sota_str}", flush=True)
            cfg_ws = Path(_to_wsl(global_sota_str))
            config_candidates = list(cfg_ws.glob("mlruns/*/*/artifacts/config"))
        if len(config_candidates) != 1:
            raise RuntimeError(f"Expected exactly 1 config, found {len(config_candidates)}")
'@.TrimEnd().Replace("`r`n", "`n")
$newRunModelBlock = @'
    cfg_ws = ws
    config_candidates = list(ws.glob("mlruns/*/*/artifacts/config"))
    if len(config_candidates) != 1:
        raise RuntimeError(
            f"Batch {batch or 'default'} workspace {ws} must contain exactly 1 "
            f"evaluated config; found {len(config_candidates)}"
        )
'@.TrimEnd().Replace("`r`n", "`n")
if (-not $runModelContent.Contains("must contain exactly 1")) {
  $fallbackPattern = '(?ms)^    cfg_ws = ws.*?(?=^    cfg = pickle\.load)'
  if ([regex]::Matches($runModelContent, $fallbackPattern).Count -ne 1) {
    throw "Unexpected batch fallback logic in $runModel"
  }
  $runModelContent = [regex]::Replace(
    $runModelContent,
    $fallbackPattern,
    $newRunModelBlock + "`n`n"
  )
  Write-Utf8NoBom $runModel $runModelContent
}

$python = "D:\anaconda3\python.exe"
& (Join-Path $PSScriptRoot "install_rdagent_serial_factor_dedup.ps1") `
  -RdagentRoot $RdagentRoot `
  -Python $python
& (Join-Path $PSScriptRoot "install_rdagent_mp_guard.ps1") `
  -RdagentRoot $RdagentRoot `
  -Python $python

& $python -m py_compile `
  (Join-Path $RdagentRoot "build_csi300.py") `
  (Join-Path $RdagentRoot "build_universe.py") `
  (Join-Path $RdagentRoot "build_all_mine_history.py") `
  (Join-Path $RdagentRoot "factor_rdagent_screen.py") `
  (Join-Path $RdagentRoot "factor_analysis.py") `
  (Join-Path $RdagentRoot "resolve_sota_ws.py") `
  (Join-Path $RdagentRoot "promote_production_champion.py") `
  $factorContract `
  (Join-Path $RdagentRoot "live_topk_dropout.py") `
  (Join-Path $RdagentRoot "prediction_preflight.py") `
  (Join-Path $RdagentRoot "refresh_rdagent_daily_pv.py") `
  $runModel `
  (Join-Path $RdagentRoot "predict_next_day.py") `
  $factorRunner `
  $mineEval `
  $envPy `
  $litellmBackend
if ($LASTEXITCODE -ne 0) { throw "RD-Agent Python syntax validation failed" }

Write-Host "RD-Agent factor mining repair installed under $RdagentRoot"
