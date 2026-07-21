import json
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from scripts import export_asset_injection


def test_asset_seed_is_local_atomic_and_marks_real_issue_date(tmp_path, monkeypatch):
    today = date.today()
    frame = pd.DataFrame(
        [
            {
                "股票代码": "600001",
                "股票简称": "样本一",
                "发行方式": "定向增发",
                "发行日期": (today - timedelta(days=45)).isoformat(),
                "增发上市日期": (today - timedelta(days=30)).isoformat(),
                "锁定期": "3年",
                "发行价格": 12.34,
            },
            {
                "股票代码": "000002",
                "股票简称": "样本二",
                "发行方式": "非公开发行",
                "发行日期": None,
                "增发上市日期": (today - timedelta(days=20)).isoformat(),
                "锁定期": "5年",
                "发行价格": 8.9,
            },
        ]
    )
    output = tmp_path / "asset_injection.json"
    monkeypatch.setattr(export_asset_injection, "OUT", str(output))
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_qbzf_em=lambda: frame))

    export_asset_injection.main()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["n"] == 2
    dated = next(item for item in payload["items"] if item["code"] == "600001")
    assert dated["issue_date_source"] == "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE"
    missing_date = next(item for item in payload["items"] if item["code"] == "000002")
    assert missing_date["issue_date"] == ""
    assert "issue_date_source" not in missing_date
    assert not output.with_suffix(output.suffix + ".tmp").exists()


def test_asset_exporter_has_no_implicit_nas_write_or_universal_36_month_claim():
    source = Path("scripts/export_asset_injection.py").read_text(encoding="utf-8")

    assert "NAS =" not in source
    assert "shutil.copy" not in source
    assert "36月法定" not in source
    assert "锁36月=" not in source
    assert "逐笔交易条款" in source


def test_watcher_refreshes_placement_twice_daily_with_retry_and_grouped_publish():
    source = Path("scripts/watch_predict_pc.ps1").read_text(encoding="utf-8")
    block = source[
        source.index("function Test-PlacementJson"):
        source.index("function Invoke-EarningsTimesIncremental")
    ]

    assert "export_asset_injection.py" in block
    assert "export_placement_events.py" in block
    assert 'EndsWith("-afterclose")' in block
    assert "AddHours(-18)" in block
    assert '"-preopen"' in block
    assert '"-afterclose"' in block
    assert "[DayOfWeek]::Saturday" in block
    assert "Set-AutoRefreshState $placementAutoFile" in block
    assert "Read-AutoRefreshState $placementAutoFile" in block
    assert "last_success_slot" in block
    assert "Test-AutoRefreshRetryReady $state" in block
    assert "Write-PlacementAutoState" not in block
    assert "@($payload.errors).Count -gt 0" in block
    assert "$items.Count -eq 0" in block

    validation = block.index('Test-PlacementJson $placementCandidate "lifecycle"')
    grouped_publish = block.index(
        "Publish-PlacementFileSet $assetPath $placementPath $shared"
    )
    done_state = block.index(
        'Set-AutoRefreshState $placementAutoFile $autoSlot "done"'
    )
    assert validation < grouped_publish < done_state

    publish = block[
        block.index("function Publish-PlacementFileSet"):
        block.index("function Invoke-PlacementEventsRefresh")
    ]
    assert '$firstName = "asset_injection.json"' in publish
    assert '$secondName = "cninfo_placement.json"' in publish
    assert "Destination = Join-Path $destinationRoot $firstName" in publish
    assert "Destination = Join-Path $destinationRoot $secondName" in publish
    assert '".stage"' in publish
    assert '".backup"' in publish
    assert "Test-PlacementJson $entry.Stage $entry.Kind" in publish
    assert publish.index("Test-PlacementJson $entry.Stage $entry.Kind") < publish.index(
        "Move-Item -LiteralPath $entry.Stage -Destination $entry.Destination"
    )
    assert "for ($index = $entries.Count - 1; $index -ge 0; $index--)" in publish
    assert "Move-Item -LiteralPath $entry.Backup -Destination $entry.Destination" in publish
    assert "rollback_ok=$rollbackOk" in publish
    assert "Invoke-PlacementEventsAutoIfDue" in source


def test_daily_pipeline_self_heals_the_persistent_watcher():
    source = Path("scripts/daily_auto_pipeline.ps1").read_text(encoding="utf-8-sig")

    assert '"scripts\\watch_predict_pc.ps1"' in source
    assert 'Start-Process -FilePath "powershell.exe"' in source
    assert '-WindowStyle Hidden' in source


def test_daily_pipeline_fails_closed_and_publishes_to_resolved_shared_directory():
    source = Path("scripts/daily_auto_pipeline.ps1").read_text(encoding="utf-8-sig")

    assert "$env:SHARED_DIR = $shared" in source
    assert '[System.IO.FileShare]::None' in source
    assert "if ($copyExit -ge 8)" in source
    assert 'throw "build_csi300.py 失败' in source
    assert '$failed += "$m/predict"' in source
    assert '$failed += "$m/publish"' in source
    assert "exit $pipelineExit" in source
