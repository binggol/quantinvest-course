import re
from pathlib import Path


def _command_pattern(path: str) -> str:
    source = Path(path).read_text(encoding="utf-8")
    match = re.search(r"\$watcherCommandPattern\s*=\s*'([^']+)'", source)
    assert match, f"watcher command pattern missing from {path}"
    return match.group(1)


def test_watcher_process_match_is_exact() -> None:
    watcher_pattern = _command_pattern("scripts/watch_predict_pc.ps1")
    restart_pattern = _command_pattern("scripts/restart_watch_predict_pc.ps1")
    daily_pattern = _command_pattern("scripts/daily_auto_pipeline.ps1")
    assert watcher_pattern == restart_pattern == daily_pattern

    valid = [
        'powershell.exe -NoProfile -File "C:\\quantinvest\\scripts\\watch_predict_pc.ps1"',
        "powershell.exe -File C:\\quantinvest\\scripts\\watch_predict_pc.ps1",
        'pwsh.exe -NoProfile -File "C:\\quantinvest\\scripts\\watch_predict_pc.ps1"',
    ]
    unrelated = [
        'powershell.exe -File "C:\\quantinvest\\scripts\\restart_watch_predict_pc.ps1" -Hidden',
        'powershell.exe -Command "Get-CimInstance | find watch_predict_pc.ps1"',
        'powershell.exe -File "C:\\quantinvest\\scripts\\watch_predict_pc.ps1.bak"',
    ]

    assert all(re.search(watcher_pattern, command) for command in valid)
    assert not any(re.search(watcher_pattern, command) for command in unrelated)


def test_watcher_singleton_never_kills_the_existing_owner() -> None:
    source = Path("scripts/watch_predict_pc.ps1").read_text(encoding="utf-8-sig")
    singleton = source.split("# ===== 单例守卫", 1)[1].split("# Shared dir", 1)[0]
    assert "[System.IO.FileShare]::None" in singleton
    assert "exit 0" in singleton
    assert "Stop-Process" not in singleton
    assert "Name='powershell.exe' OR Name='pwsh.exe'" in singleton


def test_daily_pipeline_starts_watcher_only_after_a_successful_empty_scan() -> None:
    source = Path("scripts/daily_auto_pipeline.ps1").read_text(encoding="utf-8-sig")
    start = source.index("# 每日自愈启动常驻监听")
    end = source.index("# 0. tushare token", start)
    block = source[start:end]
    assert '$watcherScanOk = $false' in block
    assert '$watcherScanOk -and $runningWatchers.Count -gt 0' in block
    assert '} elseif ($watcherScanOk) {' in block
    assert "跳过重复启动" in block
    assert "Name='powershell.exe' OR Name='pwsh.exe'" in block


def test_explicit_restart_cannot_orphan_a_mining_tree() -> None:
    source = Path("scripts/restart_watch_predict_pc.ps1").read_text(encoding="utf-8-sig")
    assert "fin_factor is still running; watcher restart aborted" in source
    assert "ProcessId=$oldPid" in source
    assert "Test-WatcherPidOwnership $proc" in source
    assert "[System.IO.FileShare]::None" in source
    assert "($pidWritten - $processStarted).TotalSeconds" in source
    assert "could not be stopped; run this script as administrator" in source
    assert "old watcher is still alive; new instance was not started" in source
    assert "new watcher did not publish its PID and instance lock" in source
    assert "-PassThru" in source
