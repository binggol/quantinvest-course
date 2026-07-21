import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts import refresh_page_sources as runner


def _base(today: date) -> dict:
    return {"updated": f"{today.isoformat()} 09:30", "as_of": today.isoformat()}


def _valid_payload(key: str, today: date) -> dict:
    payload = _base(today)
    if key == "event":
        payload.update(
            {
                "cats": {
                    "test": {
                        "desc": "test category",
                        "win_days": 30,
                        "n": 1,
                        "n_window": 1,
                        "items": [{"code": "000001"}],
                    }
                },
                "note": "test",
            }
        )
    elif key == "inquiry":
        payload.update(n=1, n_window=1, items=[{"code": "000001"}])
    elif key == "investigation":
        payload.update(n=1, n_blacklist=1, items=[{"code": "000001"}])
    elif key == "repo_cancel":
        payload.update(n=1, n_in_window=1, items=[{"code": "000001"}])
    elif key == "commit":
        payload.update(n=1, n_window=1, items=[{"code": "000001"}])
    elif key == "leverage":
        payload.update(n=1, thr_pct=2.5, items=[{"code": "000001"}])
    elif key == "lhb":
        payload.update(n=1, thr_pct=-5.0, items=[{"code": "000001"}])
    elif key == "bigbath":
        payload.update(
            n=2,
            n_rebound=1,
            items=[{"code": "000001"}],
            source_health={
                "forecast_codes": 100,
                "forecast_cache": r"C:\rdagent\_forecast_1000.pkl",
            },
        )
    elif key == "late":
        payload.pop("as_of")
        payload.update(
            rpt_period=f"{today.year - 1}1231",
            season="annual-report season",
            in_season=False,
            items=[],
            msg="outside annual-report season",
        )
    elif key == "foreign":
        announced = today + timedelta(days=10)
        effective = today + timedelta(days=20)
        payload.update(
            schedule=[
                {
                    "index": "MSCI China",
                    "ann_date": announced.isoformat(),
                    "eff_date": effective.isoformat(),
                    "days_to_ann": 10,
                    "days_to_eff": 20,
                }
            ],
            candidates=[{"code": "000001"}],
            n_cand=1,
            disclaimer="non-official candidate proxy",
        )
    else:  # pragma: no cover - protects test fixture maintenance
        raise AssertionError(key)
    return payload


@pytest.mark.parametrize("key", tuple(runner.JOBS))
def test_every_job_accepts_its_expected_business_shape(key):
    today = date.today()
    payload = _valid_payload(key, today)

    rows = runner.JOBS[key].validator(payload, today)

    assert isinstance(rows, int)
    assert rows >= 0


def test_invalid_semantics_roll_back_local_and_do_not_publish(tmp_path):
    data = tmp_path / "data"
    shared = tmp_path / "shared"
    data.mkdir()
    shared.mkdir()
    local = data / "inquiry_letter.json"
    destination = shared / local.name
    old_local = b'{"marker":"old-local"}'
    local.write_bytes(old_local)
    destination.write_text('{"marker":"old-shared"}', encoding="utf-8")

    def fake_run(command, **kwargs):
        assert kwargs["check"] is False
        invalid = _valid_payload("inquiry", date.today())
        invalid["n"] = 2
        local.write_text(json.dumps(invalid), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    state_path = data / "page_refresh_state.json"
    exit_code = runner.run_selection(
        "inquiry",
        data_dir=data,
        shared_dir=shared,
        state_path=state_path,
        run_command=fake_run,
    )

    assert exit_code == 1
    assert local.read_bytes() == old_local
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "marker": "old-shared"
    }
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "error"
    assert state["jobs"]["inquiry"]["status"] == "error"
    assert "does not match" in state["jobs"]["inquiry"]["error"]
    assert not list(data.glob(".page-refresh-*"))
    assert not list(data.glob(".page_refresh_state.json.*.tmp"))


def test_page_refresh_groups_are_serialized_in_one_process(tmp_path):
    with runner.page_refresh_lock(tmp_path):
        with pytest.raises(runner.PageRefreshAlreadyRunning):
            with runner.page_refresh_lock(tmp_path):
                pass


def test_page_refresh_groups_are_serialized_across_processes(tmp_path):
    ready = tmp_path / "ready"
    code = """
import sys, time
from pathlib import Path
from scripts.refresh_page_sources import page_refresh_lock
with page_refresh_lock(Path(sys.argv[1])):
    Path(sys.argv[2]).write_text('ready', encoding='ascii')
    time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(ready)],
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "lock-holder child never became ready"
        with pytest.raises(runner.PageRefreshAlreadyRunning):
            with runner.page_refresh_lock(tmp_path):
                pass
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_group_continues_after_one_exporter_failure(tmp_path):
    data = tmp_path / "data"
    shared = tmp_path / "shared"
    qlib = tmp_path / "qlib"
    data.mkdir()
    shared.mkdir()
    qlib.mkdir()
    selected = runner.GROUPS["company-events"]
    script_to_key = {runner.JOBS[key].script: key for key in selected}
    for key in selected:
        (data / runner.JOBS[key].output).write_text(
            json.dumps({"marker": f"old-{key}"}), encoding="utf-8"
        )
    invoked = []

    def fake_run(command, **kwargs):
        key = script_to_key[Path(command[1]).name]
        invoked.append(key)
        assert kwargs["env"]["QI_QLIB_DATA_DIR"] == str(qlib)
        assert kwargs["env"]["QI_EXPORT_NAS_DIR"] != str(shared)
        if key == "event":
            return subprocess.CompletedProcess(command, 17)
        output = data / runner.JOBS[key].output
        output.write_text(json.dumps(_valid_payload(key, date.today())), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    exit_code = runner.run_selection(
        "company-events",
        data_dir=data,
        shared_dir=shared,
        qlib_data_dir=qlib,
        run_command=fake_run,
    )

    assert exit_code == 1
    assert invoked == list(selected)
    assert json.loads((data / "event_avoid.json").read_text(encoding="utf-8")) == {
        "marker": "old-event"
    }
    assert not (shared / "event_avoid.json").exists()
    for key in selected[1:]:
        published = shared / runner.JOBS[key].output
        assert published.is_file()
        assert json.loads(published.read_text(encoding="utf-8"))["updated"].startswith(
            date.today().isoformat()
        )
    state = json.loads(
        (data / "page_refresh_state.json").read_text(encoding="utf-8")
    )
    assert state["jobs"]["event"]["status"] == "error"
    assert all(state["jobs"][key]["status"] == "success" for key in selected[1:])
    assert state["success_count"] == 4
    assert state["error_count"] == 1


def test_unchanged_output_fails_closed(tmp_path):
    data = tmp_path / "data"
    shared = tmp_path / "shared"
    data.mkdir()
    shared.mkdir()
    output = data / "inquiry_letter.json"
    output.write_text(json.dumps(_valid_payload("inquiry", date.today())), encoding="utf-8")
    before = output.read_bytes()

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0)

    exit_code = runner.run_selection(
        "inquiry", data_dir=data, shared_dir=shared, run_command=fake_run
    )

    assert exit_code == 1
    assert output.read_bytes() == before
    assert not (shared / output.name).exists()
    state = json.loads(
        (data / "page_refresh_state.json").read_text(encoding="utf-8")
    )
    assert "materially change" in state["jobs"]["inquiry"]["error"]


def test_foreign_schedule_countdown_is_semantically_validated():
    today = date.today()
    payload = _valid_payload("foreign", today)
    payload["schedule"][0]["days_to_eff"] = 999

    with pytest.raises(ValueError, match="days_to_eff is inconsistent"):
        runner.JOBS["foreign"].validator(payload, today)


def test_bigbath_rejects_an_empty_or_unverified_forecast_cache():
    payload = _valid_payload("bigbath", date.today())
    payload["source_health"]["forecast_codes"] = 0

    with pytest.raises(ValueError, match="no stock histories"):
        runner.JOBS["bigbath"].validator(payload, date.today())


@pytest.mark.parametrize("key", ("event", "inquiry", "investigation", "repo_cancel", "commit", "leverage", "lhb"))
def test_network_style_empty_snapshots_are_rejected(key):
    today = date.today()
    payload = _valid_payload(key, today)
    if key == "event":
        for category in payload["cats"].values():
            category.update(n=0, n_window=0, items=[])
    else:
        payload["n"] = 0
        payload["items"] = []
        for field in ("n_window", "n_blacklist", "n_in_window"):
            if field in payload:
                payload[field] = 0

    with pytest.raises(ValueError, match="no"):
        runner.JOBS[key].validator(payload, today)


def test_legacy_exporters_support_isolated_publish_and_local_qlib_overrides():
    root = Path(__file__).resolve().parents[1]
    for spec in runner.JOBS.values():
        source = (root / "scripts" / spec.script).read_text(encoding="utf-8")
        assert "QI_EXPORT_NAS_DIR" in source
    for script in ("export_leverage_avoid.py", "export_foreign_inclusion.py"):
        source = (root / "scripts" / script).read_text(encoding="utf-8")
        assert "QI_QLIB_DATA_DIR" in source
        assert r"C:\qlib_data\cn_data" in source
    assert "QI_FORECAST_CACHE" in (
        root / "scripts" / "export_bigbath.py"
    ).read_text(encoding="utf-8")
    bigbath = (root / "scripts" / "export_bigbath.py").read_text(encoding="utf-8")
    assert "forecast mapping is empty" in bigbath
    assert '"source_health"' in bigbath
    assert "raise SystemExit(main())" in bigbath

    for script in (
        "export_event_avoid.py",
        "export_inquiry_letter.py",
        "export_investigation_avoid.py",
        "export_repo_cancel.py",
        "export_commit_nosell.py",
    ):
        source = (root / "scripts" / script).read_text(encoding="utf-8")
        assert "query_announcements" in source
    for script in ("export_investigation_avoid.py", "export_repo_cancel.py"):
        source = (root / "scripts" / script).read_text(encoding="utf-8")
        assert "('szse', 'sse')" in source


def test_required_business_groups_are_explicit():
    assert runner.GROUPS["company-events"] == (
        "event",
        "inquiry",
        "investigation",
        "repo_cancel",
        "commit",
    )
    assert runner.GROUPS["closing-risk"] == ("leverage", "lhb", "bigbath")
    assert runner.GROUPS["weekly-sources"] == ("late", "foreign")
