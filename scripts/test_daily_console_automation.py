from contextlib import contextmanager
import json
import subprocess
from pathlib import Path

import pytest

from scripts import refresh_daily_console as runner
from scripts.process_lock import ProcessLockBusy


def test_atomic_publish_rejects_invalid_json_and_preserves_destination(tmp_path):
    source = tmp_path / "source.json"
    destination = tmp_path / "published.json"
    source.write_text('{"value": NaN}', encoding="utf-8")
    destination.write_text('{"old": true}', encoding="utf-8")

    with pytest.raises(ValueError):
        runner.atomic_publish(source, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == {"old": True}
    assert not list(tmp_path.glob(".published.json.*.tmp"))


def test_korea_refresh_requires_fresh_output_and_publishes_atomically(
    monkeypatch, tmp_path
):
    rdagent = tmp_path / "rdagent"
    shared = tmp_path / "shared"
    rdagent.mkdir()
    shared.mkdir()
    source = rdagent / "korea_semi.json"
    source.write_text('{"updated": "old"}', encoding="utf-8")

    def fake_run(command, **kwargs):
        assert command[-1] == str(rdagent / "export_korea_semi.py")
        assert kwargs["check"] is True
        today = runner.datetime.now().date().isoformat()
        source.write_text(
            json.dumps(
                {
                    "updated": f"{today} 14:35",
                    "hynix_date": today,
                    "hynix_ret": 0.01,
                    "signal": "ok",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    output = runner.refresh_korea(
        python=Path("python.exe"), shared_dir=shared, rdagent_dir=rdagent
    )

    assert output == shared / "korea_semi.json"
    assert json.loads(output.read_text(encoding="utf-8"))["signal"] == "ok"


def test_exporter_exit_zero_without_new_output_is_an_error(monkeypatch, tmp_path):
    output = tmp_path / "stale.json"
    output.write_text('{"updated": "old"}', encoding="utf-8")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    with pytest.raises(RuntimeError, match="did not refresh"):
        runner._run_fresh_output(["fake"], cwd=tmp_path, output=output)


def test_snowball_refresh_uses_provider_path_and_publishes_history(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    data = project / "data"
    shared = tmp_path / "shared"
    data.mkdir(parents=True)
    shared.mkdir()
    workbook = tmp_path / "snowball.xlsx"
    workbook.write_bytes(b"test workbook")
    source = data / "snowball_avoid.json"
    history = data / "snowball_history.json"
    source.write_text('{"updated": "old"}', encoding="utf-8")
    history.write_text("[]", encoding="utf-8")

    def fake_run(command, **kwargs):
        assert command[-1] == str(project / "scripts" / "export_snowball.py")
        assert kwargs["check"] is True
        assert kwargs["env"]["SNOWBALL_XLSX"] == str(workbook)
        today = runner.datetime.now().date().isoformat()
        source.write_text(
            json.dumps(
                {
                    "updated": f"{today} 09:35",
                    "as_of": today,
                    "n": 1,
                    "items": [{"code": "000001.SZ"}],
                }
            ),
            encoding="utf-8",
        )
        history.write_text(
            json.dumps([{"as_of": today, "updated": f"{today} 09:35", "n": 1}]),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    output = runner.refresh_snowball(
        python=Path("python.exe"), shared_dir=shared, xlsx=workbook
    )

    assert output == shared / "snowball_avoid.json"
    assert json.loads(output.read_text(encoding="utf-8"))["n"] == 1
    assert json.loads((shared / "snowball_history.json").read_text(encoding="utf-8"))[
        -1
    ]["n"] == 1


def test_rolling_refresh_validates_shared_snapshot(monkeypatch, tmp_path):
    project = tmp_path / "project"
    data = project / "data"
    shared = tmp_path / "shared"
    data.mkdir(parents=True)
    shared.mkdir()

    def fake_run(command, **kwargs):
        today = runner.datetime.now().date().isoformat()
        (data / "rolling_earnings.json").write_text(
            json.dumps(
                {
                    "updated": f"{today} 06:00:00",
                    "n": 1,
                    "source_health": {"shared_payloads": 2, "event_rows": 10},
                    "rolling": {"items": [{"code": "000001"}]},
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    output = runner.refresh_rolling(python=Path("python.exe"), shared_dir=shared)
    assert json.loads(output.read_text(encoding="utf-8"))["n"] == 1


def test_cross_market_rejects_invalid_session_without_overwriting_shared(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    data = project / "data"
    shared = tmp_path / "shared"
    data.mkdir(parents=True)
    shared.mkdir()
    destination = shared / "cross_market_storage.json"
    destination.write_text('{"version": "previous"}', encoding="utf-8")

    def fake_run(command, **kwargs):
        today = runner.datetime.now().date().isoformat()
        (data / "cross_market_storage.json").write_text(
            json.dumps(
                {
                    "generated_at": f"{today}T09:20:00+08:00",
                    "data_health": {"status": "missing", "market_at": ""},
                    "leaders": [{"symbol": "MU"}],
                }
            ),
            encoding="utf-8",
        )
        (data / "cross_market_storage_status.json").write_text(
            json.dumps({"status": "done", "updated": today}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="current-session"):
        runner.refresh_cross_market(python=Path("python.exe"), shared_dir=shared)
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "version": "previous"
    }


def test_inclusion_refresh_validates_and_publishes(monkeypatch, tmp_path):
    project = tmp_path / "project"
    data = project / "data"
    shared = tmp_path / "shared"
    data.mkdir(parents=True)
    shared.mkdir()
    source = data / "index_inclusion.json"
    source.write_text('{"updated_at": "old"}', encoding="utf-8")

    def fake_run(command, **kwargs):
        today = runner.datetime.now().date().isoformat()
        source.write_text(
            json.dumps(
                {
                    "updated_at": f"{today} 06:00:00",
                    "stats": {"CSI300": {"count": 1}},
                    "details": [{"code": "000001"}],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    output = runner.refresh_inclusion(python=Path("python.exe"), shared_dir=shared)
    assert json.loads(output.read_text(encoding="utf-8"))["details"][0]["code"] == "000001"


def test_transfer_documents_fail_closed_and_restore_previous_local(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    data = project / "data"
    shared = tmp_path / "shared"
    data.mkdir(parents=True)
    shared.mkdir()
    source = data / "cninfo_transfer.json"
    overlay = data / "transfer_terms_overlay.json"
    source.write_text('{"version": "previous"}', encoding="utf-8")
    overlay.write_text('{"version": "previous"}', encoding="utf-8")

    def fake_run(command, **kwargs):
        today = runner.datetime.now().date().isoformat()
        source.write_text(
            json.dumps(
                {
                    "updated": f"{today} 18:35:00",
                    "items": [{"code": "000001"}],
                    "errors": [{"code": "000002", "error": "network"}],
                    "query": {"end": today},
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="reported errors"):
        runner.refresh_transfer_documents(
            python=Path("python.exe"), shared_dir=shared
        )

    assert json.loads(source.read_text(encoding="utf-8")) == {
        "version": "previous"
    }
    assert json.loads(overlay.read_text(encoding="utf-8")) == {
        "version": "previous"
    }
    assert not (shared / source.name).exists()


@pytest.mark.parametrize(
    ("job", "filenames"),
    (
        ("transfer", ("transfer_terms_overlay.json", "cninfo_transfer.json")),
        ("placement", ("asset_injection.json", "cninfo_placement.json")),
    ),
)
def test_document_bundle_second_shared_publish_failure_restores_every_version(
    monkeypatch, tmp_path, job, filenames
):
    project = tmp_path / "project"
    data = project / "data"
    shared = tmp_path / "shared"
    data.mkdir(parents=True)
    shared.mkdir()
    today = runner.datetime.now().date().isoformat()
    current_payloads = {
        "transfer_terms_overlay.json": {
            "updated": f"{today} 18:36:00",
            "items": [{"code": "000001"}],
            "stats": {"errors": 0},
        },
        "cninfo_transfer.json": {
            "updated": f"{today} 18:35:00",
            "items": [{"code": "000001"}],
            "errors": [],
            "query": {"end": today},
        },
        "asset_injection.json": {
            "updated": f"{today} 18:45:00",
            "n": 1,
            "items": [{"code": "000001"}],
        },
        "cninfo_placement.json": {
            "updated": f"{today} 18:46:00",
            "count": 1,
            "items": [{"code": "000001"}],
            "errors": [],
        },
    }
    for filename in filenames:
        (data / filename).write_text(
            json.dumps({"version": f"old-local-{filename}"}), encoding="utf-8"
        )
        (shared / filename).write_text(
            json.dumps({"version": f"old-shared-{filename}"}), encoding="utf-8"
        )

    def fake_fresh_output(command, *, cwd, output, env=None):
        output.write_text(json.dumps(current_payloads[output.name]), encoding="utf-8")

    real_atomic_publish = runner.atomic_publish
    publish_calls = []

    def fail_second_publish(source, destination):
        publish_calls.append(destination)
        if len(publish_calls) == 2:
            raise OSError("injected second shared publish failure")
        real_atomic_publish(source, destination)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner, "_run_fresh_output", fake_fresh_output)
    monkeypatch.setattr(runner, "atomic_publish", fail_second_publish)

    with pytest.raises(OSError, match="second shared publish failure"):
        if job == "transfer":
            runner.refresh_transfer_documents(
                python=Path("python.exe"), shared_dir=shared
            )
        else:
            runner.refresh_placement_documents(
                python=Path("python.exe"), shared_dir=shared
            )

    assert publish_calls == [shared / filenames[0], shared / filenames[1]]
    for filename in filenames:
        assert json.loads((data / filename).read_text(encoding="utf-8")) == {
            "version": f"old-local-{filename}"
        }
        assert json.loads((shared / filename).read_text(encoding="utf-8")) == {
            "version": f"old-shared-{filename}"
        }


def test_money_outflow_validates_and_publishes_to_both_data_roots(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    data = project / "data"
    shared = tmp_path / "shared"
    app_data = tmp_path / "app-data"
    data.mkdir(parents=True)
    shared.mkdir()
    app_data.mkdir()
    source = data / "money_outflow_signal.json"

    def fake_run(command, **kwargs):
        assert kwargs["env"]["QI_SKIP_MONEYFLOW_NAS_PUBLISH"] == "1"
        today = runner.datetime.now().date().isoformat()
        source.write_text(
            json.dumps(
                {
                    "updated": f"{today} 21:45:00",
                    "n_moneyflow_rows": 100,
                    "n_feature_rows": 100,
                    "latest_stock_outflow": [
                        {"code": "000001.SZ", "trade_date": today}
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    output = runner.refresh_money_outflow(
        python=Path("python.exe"),
        shared_dir=shared,
        nas_app_data_dir=app_data,
    )

    assert output == shared / source.name
    assert json.loads(output.read_text(encoding="utf-8"))["n_moneyflow_rows"] == 100
    assert json.loads((app_data / source.name).read_text(encoding="utf-8"))[
        "n_feature_rows"
    ] == 100


def test_money_outflow_second_root_failure_restores_both_roots(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    data = project / "data"
    shared = tmp_path / "shared"
    app_data = tmp_path / "app-data"
    data.mkdir(parents=True)
    shared.mkdir()
    app_data.mkdir()
    source = data / "money_outflow_signal.json"
    source.write_text('{"version":"old-local"}', encoding="utf-8")
    (shared / source.name).write_text('{"version":"old-shared"}', encoding="utf-8")
    (app_data / source.name).write_text('{"version":"old-app"}', encoding="utf-8")

    def fake_run(command, **_kwargs):
        today = runner.datetime.now().date().isoformat()
        source.write_text(
            json.dumps(
                {
                    "updated": f"{today} 21:45:00",
                    "n_moneyflow_rows": 100,
                    "n_feature_rows": 100,
                    "latest_stock_outflow": [
                        {"code": "000001.SZ", "trade_date": today}
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    real_atomic_publish = runner.atomic_publish

    def fail_app_root(source_path, destination):
        if destination == app_data / source.name:
            raise OSError("injected app-root publish failure")
        real_atomic_publish(source_path, destination)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "atomic_publish", fail_app_root)

    with pytest.raises(OSError, match="app-root publish failure"):
        runner.refresh_money_outflow(
            python=Path("python.exe"),
            shared_dir=shared,
            nas_app_data_dir=app_data,
        )

    assert json.loads(source.read_text(encoding="utf-8")) == {"version": "old-local"}
    assert json.loads((shared / source.name).read_text(encoding="utf-8")) == {
        "version": "old-shared"
    }
    assert json.loads((app_data / source.name).read_text(encoding="utf-8")) == {
        "version": "old-app"
    }


def test_top_risk_publishes_cache_before_validated_signals(monkeypatch, tmp_path):
    project = tmp_path / "project"
    data = project / "data"
    cache = data / "etf_flow_cache"
    shared = tmp_path / "shared"
    app_data = tmp_path / "app-data"
    cache.mkdir(parents=True)
    shared.mkdir()
    app_data.mkdir()
    for index in range(5):
        (cache / f"cache-{index}.csv.gz").write_bytes(b"cache")
    broad = data / "etf_flow_top_signal.json"
    sector = data / "sector_etf_flow_signal.json"
    huijin = data / "huijin_etf_flow.json"

    def fake_run(command, **kwargs):
        today = runner.datetime.now().date().isoformat()
        if str(command[1]).endswith("backtest_etf_flow_signal.py"):
            broad.write_text(
                json.dumps(
                    {
                        "updated": f"{today}T22:15:00",
                        "period": ["2018-01-01", today],
                        "events": [{"trade_date": today}],
                        "etfs": ["510300.SH"],
                    }
                ),
                encoding="utf-8",
            )
        elif str(command[1]).endswith("backtest_sector_etf_flow_signal.py"):
            sector.write_text(
                json.dumps(
                    {
                        "updated": f"{today}T22:16:00",
                        "period": ["2018-01-01", today],
                        "events": [{"trade_date": today}],
                        "missing": [],
                    }
                ),
                encoding="utf-8",
            )
        else:
            huijin.write_text(
                json.dumps(
                    {
                        "updated": f"{today}T22:17:00",
                        "as_of": today,
                        "etfs": [{"code": "510300.SH"}],
                        "aggregate_series": [{"date": today}],
                        "data_quality": {"coverage_pct": 100.0},
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    output = runner.refresh_top_risk(
        python=Path("python.exe"),
        shared_dir=shared,
        nas_app_data_dir=app_data,
    )

    assert output == shared / broad.name
    assert len(list((shared / "etf_flow_cache").glob("*"))) == 5
    assert len(list((app_data / "etf_flow_cache").glob("*"))) == 5
    assert (shared / sector.name).is_file()
    assert (app_data / broad.name).is_file()
    assert (shared / huijin.name).is_file()
    assert (app_data / huijin.name).is_file()


def test_top_risk_late_publish_failure_restores_cache_and_signals(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    data = project / "data"
    cache = data / "etf_flow_cache"
    shared = tmp_path / "shared"
    app_data = tmp_path / "app-data"
    shared_cache = shared / "etf_flow_cache"
    app_cache = app_data / "etf_flow_cache"
    for directory in (cache, shared_cache, app_cache):
        directory.mkdir(parents=True)
    broad = data / "etf_flow_top_signal.json"
    sector = data / "sector_etf_flow_signal.json"
    huijin = data / "huijin_etf_flow.json"
    broad.write_text('{"version":"old-local-broad"}', encoding="utf-8")
    sector.write_text('{"version":"old-local-sector"}', encoding="utf-8")
    huijin.write_text('{"version":"old-local-huijin"}', encoding="utf-8")
    for root, label in ((shared, "shared"), (app_data, "app")):
        (root / broad.name).write_text(
            json.dumps({"version": f"old-{label}-broad"}), encoding="utf-8"
        )
        (root / sector.name).write_text(
            json.dumps({"version": f"old-{label}-sector"}), encoding="utf-8"
        )
        (root / huijin.name).write_text(
            json.dumps({"version": f"old-{label}-huijin"}), encoding="utf-8"
        )
    for index in range(5):
        name = f"cache-{index}.csv.gz"
        (cache / name).write_bytes(f"new-cache-{index}".encode())
        (shared_cache / name).write_bytes(f"old-shared-cache-{index}".encode())
        (app_cache / name).write_bytes(f"old-app-cache-{index}".encode())

    def fake_run(command, **_kwargs):
        today = runner.datetime.now().date().isoformat()
        if str(command[1]).endswith("backtest_etf_flow_signal.py"):
            broad.write_text(
                json.dumps(
                    {
                        "updated": f"{today}T22:15:00",
                        "period": ["2018-01-01", today],
                        "events": [{"trade_date": today}],
                        "etfs": ["510300.SH"],
                    }
                ),
                encoding="utf-8",
            )
        elif str(command[1]).endswith("backtest_sector_etf_flow_signal.py"):
            sector.write_text(
                json.dumps(
                    {
                        "updated": f"{today}T22:16:00",
                        "period": ["2018-01-01", today],
                        "events": [{"trade_date": today}],
                        "missing": [],
                    }
                ),
                encoding="utf-8",
            )
        else:
            huijin.write_text(
                json.dumps(
                    {
                        "updated": f"{today}T22:17:00",
                        "as_of": today,
                        "etfs": [{"code": "510300.SH"}],
                        "aggregate_series": [{"date": today}],
                        "data_quality": {"coverage_pct": 100.0},
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0)

    real_atomic_publish = runner.atomic_publish

    def fail_last_signal(source_path, destination):
        if destination == app_data / broad.name:
            raise OSError("injected late top-risk publish failure")
        real_atomic_publish(source_path, destination)

    monkeypatch.setattr(runner, "ROOT", project)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "atomic_publish", fail_last_signal)

    with pytest.raises(OSError, match="late top-risk publish failure"):
        runner.refresh_top_risk(
            python=Path("python.exe"),
            shared_dir=shared,
            nas_app_data_dir=app_data,
        )

    assert json.loads(broad.read_text(encoding="utf-8")) == {
        "version": "old-local-broad"
    }
    assert json.loads(sector.read_text(encoding="utf-8")) == {
        "version": "old-local-sector"
    }
    assert json.loads(huijin.read_text(encoding="utf-8")) == {
        "version": "old-local-huijin"
    }
    for root, label in ((shared, "shared"), (app_data, "app")):
        assert json.loads((root / broad.name).read_text(encoding="utf-8")) == {
            "version": f"old-{label}-broad"
        }
        assert json.loads((root / sector.name).read_text(encoding="utf-8")) == {
            "version": f"old-{label}-sector"
        }
        assert json.loads((root / huijin.name).read_text(encoding="utf-8")) == {
            "version": f"old-{label}-huijin"
        }
        for index in range(5):
            assert (root / "etf_flow_cache" / f"cache-{index}.csv.gz").read_bytes() == (
                f"old-{label}-cache-{index}".encode()
            )


@pytest.mark.parametrize(
    ("job", "filename"),
    (
        ("earnings", "cninfo_earnings_announcements.json"),
        ("rolling-backtest", "rolling_earnings_backtest_top50.json"),
        ("transfer", "cninfo_transfer.json"),
        ("placement", "cninfo_placement.json"),
    ),
)
def test_lock_busy_never_restores_over_active_writer(
    monkeypatch, tmp_path, job, filename
):
    root = tmp_path / "root"
    data = root / "data"
    shared = tmp_path / "shared"
    data.mkdir(parents=True)
    shared.mkdir()
    local_source = data / filename
    shared_source = shared / filename
    local_source.write_text('{"marker":"old-local"}', encoding="utf-8")
    shared_source.write_text('{"marker":"old-shared"}', encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", root)

    @contextmanager
    def busy_lock(*_args, **_kwargs):
        # Model the active owner publishing after this contender starts but
        # before its lock attempt fails.  A pre-lock snapshot/restore would
        # incorrectly replace these bytes with the old values above.
        local_source.write_text('{"marker":"active-local"}', encoding="utf-8")
        shared_source.write_text('{"marker":"active-shared"}', encoding="utf-8")
        raise ProcessLockBusy(shared / "active.lock", {"pid": 4242})
        yield

    monkeypatch.setattr(runner, "process_lock", busy_lock)

    with pytest.raises(ProcessLockBusy):
        if job == "earnings":
            runner.refresh_earnings_announcements(
                python=Path("python.exe"), shared_dir=shared
            )
        elif job == "rolling-backtest":
            runner.refresh_rolling_backtest(
                python=Path("python.exe"), shared_dir=shared
            )
        elif job == "transfer":
            runner.refresh_transfer_documents(
                python=Path("python.exe"), shared_dir=shared
            )
        else:
            runner.refresh_placement_documents(
                python=Path("python.exe"), shared_dir=shared
            )

    assert json.loads(local_source.read_text(encoding="utf-8")) == {
        "marker": "active-local"
    }
    assert json.loads(shared_source.read_text(encoding="utf-8")) == {
        "marker": "active-shared"
    }


def test_rolling_backtest_parent_lock_uses_and_cleans_internal_child_lock(
    monkeypatch, tmp_path
):
    root = tmp_path / "root"
    data = root / "data"
    shared = tmp_path / "shared"
    data.mkdir(parents=True)
    shared.mkdir()
    source = shared / "rolling_earnings_backtest_top50.json"
    local_source = data / source.name
    source.write_text('{"marker":"old-shared"}', encoding="utf-8")
    local_source.write_text('{"marker":"old-local"}', encoding="utf-8")
    status = shared / "rolling_earnings_backtest_status.json"
    production_lock = shared / "rolling_earnings_backtest.lock"
    observed_child_locks = []

    def fake_run(command, **_kwargs):
        child_lock = Path(command[command.index("--lock-file") + 1])
        observed_child_locks.append(child_lock)
        assert child_lock != production_lock
        assert production_lock.is_file()
        child_lock.write_text("child-owned", encoding="utf-8")
        today = runner.datetime.now().date().isoformat()
        source.write_text(
            json.dumps(
                {
                    "updated": f"{today}T03:35:00",
                    "n_events": 1,
                    "summary": {},
                }
            ),
            encoding="utf-8",
        )
        status.write_text(
            json.dumps({"state": "done", "reason": "scheduled-weekly"}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    output = runner.refresh_rolling_backtest(
        python=Path("python.exe"), shared_dir=shared
    )

    assert output == source
    assert len(observed_child_locks) == 1
    assert not observed_child_locks[0].exists()
    assert not production_lock.exists()
    assert json.loads(local_source.read_text(encoding="utf-8"))["n_events"] == 1


def test_daily_console_page_auto_refreshes_and_monitors_live_sources():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "daily.html").read_text(encoding="utf-8")
    app_source = (root / "app.py").read_text(encoding="utf-8")

    assert "setInterval(load,60000)" in template
    assert 'hd.id="data-health-section"' in template
    for label in ("顾问Pro篮子", "雪球合约避雷", "跨市场存储映射"):
        assert label in app_source


def test_snowball_missing_source_fails_closed():
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "export_snowball.py").read_text(encoding="utf-8")

    missing_branch = source[source.index("if not os.path.exists(XLSX):") : source.index("x = pd.read_excel")]
    assert "return 2" in missing_branch
    assert "json.dump" not in missing_branch
    assert "raise SystemExit(main())" in source
    assert "coverage < 0.8" in source
    assert "return 4" in source
    assert 'str(row.get("as_of") or "") != today' in source
    assert "end_date=today_compact" in source


def test_task_installer_uses_market_appropriate_times_and_retries():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "scripts" / "install_daily_console_tasks.ps1").read_text(
        encoding="utf-8"
    )
    pipeline = (root / "scripts" / "daily_auto_pipeline.ps1").read_text(
        encoding="utf-8"
    )

    for expected in (
        "1:30AM",
        "6:00AM",
        '-At "07:58"',
        'Times = @("09:05")',
        'Times = @("06:35", "18:40")',
        'Times = @("06:45", "18:50")',
        'Times = @("06:55", "19:00")',
        'Times = @("07:10", "18:10")',
        'Times = @("09:20")',
        'Times = @("09:35")',
        'Times = @("14:35")',
        'Times = @("19:20")',
        'Times = @("19:40")',
        'Times = @("20:40")',
        'Times = @("10:30")',
        'Times = @("22:15")',
        'Times = @("23:00")',
        'Argument = "snowball"',
        'Argument = "advisor"',
        'Argument = "transfer-documents"',
        'Argument = "placement-documents"',
        'Argument = "earnings-announcements"',
        'Argument = "growth-queue"',
        'Argument = "rolling"',
        'Argument = "top-risk"',
        'Argument = "money-outflow"',
        'Argument = "rolling-backtest"',
        'Argument = "earnings-entry-lag"',
        "company-events --lock-wait-seconds 900 --state",
        "closing-risk --lock-wait-seconds 900 --state",
        "weekly-sources --lock-wait-seconds 900 --state",
        "dedicated_refresh_tasks.enabled",
    ):
        assert expected in installer
    assert "refresh_page_sources.py" in installer
    assert "--lock-wait-seconds 900" in installer
    assert "-StartWhenAvailable" in installer
    assert "-RestartCount 2" in installer
    assert "Test-DedicatedJobConfigured" in installer
    assert "-RunLevel Limited" in installer
    assert "administrator-owned" in installer
    assert "Task.Settings.Enabled" in installer
    assert "trigger.WeeksInterval" in installer
    assert "trigger.EndBoundary" in installer
    assert "watcher ownership handoff failed" in installer
    assert "watcher_restart_admin.request.json" in installer
    assert "function Invoke-NativeCommand" in pipeline
    assert 'ErrorActionPreference = "Continue"' in pipeline
    assert "daily_auto_pipeline_state.json" in pipeline
    assert "$pipelineFingerprint" in pipeline
    assert "$previousFingerprint -eq $pipelineFingerprint" in pipeline
    assert "watcher_restart_admin.request.json" in pipeline
    assert 'operation -ne "restart_watch_predict_pc"' in pipeline
    assert "expectedWatcherPid" in pipeline
    assert "跳过模型管线" in pipeline
    assert "rdagent_recovery_admin.request.json" in pipeline
    assert 'operation -ne "recover_stuck_fin_factor"' in pipeline
    assert "expected_watcher_pid" in pipeline
    assert "expected_miner_pid" in pipeline
    assert "request_id" in pipeline
    assert "attempt_id" in pipeline
    assert 'if ($staleMinutes -lt 90)' in pipeline
    assert "未达到90分钟人工恢复门槛" in pipeline
    assert "[int]$miner.ParentProcessId -ne $expectedWatcherPid" in pipeline
    assert "$requestedAt.AddMinutes(10)" in pipeline
    assert "taskkill.exe /PID $expectedMinerPid /T /F" in pipeline
    assert "保留原始请求，由 watcher 抢救已完成轮次" in pipeline
    assert str(runner.DEFAULT_SNOWBALL_XLSX).startswith(
        r"\\your-nas\share"
    )

    watcher = (root / "scripts" / "watch_predict_pc.ps1").read_text(
        encoding="utf-8"
    )
    assert "dedicatedRefreshTasksMarker" in watcher
    assert "Invoke-EarningsEventTimesAutoIfDue" in watcher


def test_task_installer_rolls_back_dedicated_writers_and_marker_together():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "scripts" / "install_daily_console_tasks.ps1").read_text(
        encoding="utf-8"
    )

    writer_names_start = installer.index("$dedicatedWriterTaskNames")
    helper_start = installer.index("function Restore-LegacyWriterOwnership")
    install_try = installer.index(
        "try {\n  foreach ($job in $scheduledJobs)", helper_start
    )
    writer_names = installer[writer_names_start:helper_start]
    helper = installer[helper_start:install_try]
    for task_name in (
        "quantinvest-console-transfer-documents",
        "quantinvest-console-placement-documents",
        "quantinvest-console-earnings-announcements",
    ):
        assert task_name in writer_names
    assert "Disable-ScheduledTask" in helper
    assert "Unregister-ScheduledTask" in helper
    assert helper.index("Disable-ScheduledTask") < helper.index("Remove-Item")
    assert "Remove-Item -LiteralPath $dedicatedMarker" in helper

    handoff_comment = installer.index("# A running PowerShell watcher")
    installation = installer[install_try:handoff_comment]
    marker_write = installation.index("Set-Content")
    registration_catch = installation.index("catch {", marker_write)
    assert marker_write < registration_catch
    assert "Restore-LegacyWriterOwnership" in installation[registration_catch:]

    handoff_failure = installer.index("if (-not $handoffOk)")
    assert "Restore-LegacyWriterOwnership" in installer[handoff_failure:]
