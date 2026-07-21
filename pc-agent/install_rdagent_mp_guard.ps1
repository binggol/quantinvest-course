param(
  [string]$RdagentRoot = "C:\rdagent",
  [string]$Python = "D:\anaconda3\python.exe"
)

$ErrorActionPreference = "Stop"
$target = Join-Path $RdagentRoot "rdagent\core\utils.py"
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
  throw "RD-Agent core utils not found: $target"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw "Python runtime not found: $Python"
}

$content = [System.IO.File]::ReadAllText($target).Replace("`r`n", "`n")
$importNeedle = "import multiprocessing as mp`n"
$importReplacement = "import multiprocessing as mp`nimport os`nimport time`n"
if (-not $content.Contains("import os`n") -or -not $content.Contains("import time`n")) {
  if (-not $content.Contains($importNeedle)) {
    throw "Unexpected RD-Agent import block in $target"
  }
  $content = $content.Replace($importNeedle, $importReplacement)
}

$oldBlock = @'
    with mp.Pool(processes=max(1, min(n, len(func_calls)))) as pool:
        results = [
            pool.apply_async(_subprocess_wrapper, args=(f, LLM_CACHE_SEED_GEN.get_next_seed(), args))
            for f, args in func_calls
        ]
        return [result.get() for result in results]
'@.TrimEnd().Replace("`r`n", "`n")

$newBlock = @'
    process_count = max(1, min(n, len(func_calls)))
    timeout_text = os.environ.get("RDAGENT_MP_RESULT_TIMEOUT_SEC", "7200").strip()
    try:
        result_timeout = float(timeout_text)
    except ValueError:
        result_timeout = 7200.0
    if result_timeout <= 0:
        result_timeout = 7200.0

    with mp.Pool(processes=process_count) as pool:
        results = [
            pool.apply_async(_subprocess_wrapper, args=(f, LLM_CACHE_SEED_GEN.get_next_seed(), args))
            for f, args in func_calls
        ]
        worker_pids = {worker.pid for worker in pool._pool if worker.pid is not None}  # noqa: SLF001
        deadline = time.monotonic() + result_timeout
        values = [None] * len(results)
        pending = set(range(len(results)))
        while pending:
            for index in tuple(pending):
                result = results[index]
                if result.ready():
                    values[index] = result.get(timeout=0)
                    pending.remove(index)
            if not pending:
                break

            workers = list(pool._pool)  # noqa: SLF001
            current_pids = {worker.pid for worker in workers if worker.pid is not None}
            if current_pids != worker_pids or any(worker.exitcode is not None for worker in workers):
                raise RDAgentException(
                    "A multiprocessing worker exited before returning its result; aborting this attempt."
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Multiprocessing results did not finish within {result_timeout:.0f} seconds."
                )
            time.sleep(min(1.0, remaining))
        return values
'@.TrimEnd().Replace("`r`n", "`n")

if ($content.Contains($oldBlock)) {
  $content = $content.Replace($oldBlock, $newBlock)
} elseif (-not $content.Contains('RDAGENT_MP_RESULT_TIMEOUT_SEC')) {
  throw "Unexpected multiprocessing_wrapper implementation in $target"
}

$temp = "$target.mp_guard.$PID.py"
$backup = "$target.pre_mp_guard.bak"
try {
  [System.IO.File]::WriteAllText($temp, $content, [System.Text.UTF8Encoding]::new($false))
  & $Python -m py_compile $temp
  if ($LASTEXITCODE -ne 0) { throw "Patched RD-Agent core utils failed syntax validation" }
  if (-not (Test-Path -LiteralPath $backup)) {
    Copy-Item -LiteralPath $target -Destination $backup -Force
  }
  Move-Item -LiteralPath $temp -Destination $target -Force
} finally {
  Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
}

Write-Host "RD-Agent multiprocessing worker-loss guard installed: $target"
