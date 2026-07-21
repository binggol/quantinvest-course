import ast
import importlib.util
import os
import re
import subprocess
import sys
import types
import warnings
from pathlib import Path

import pytest


BACKUP_DIR = Path(__file__).with_name("rdagent_backup")


def _source(name: str) -> str:
    return (BACKUP_DIR / name).read_text(encoding="utf-8")


def _tree(name: str) -> ast.AST:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(_source(name))


@pytest.fixture
def run_daily_module(monkeypatch, tmp_path):
    tushare = types.ModuleType("tushare")
    tushare.pro_api = lambda _token: None
    notify = types.ModuleType("notify")
    notify.send_push = lambda _title, _body: None
    monkeypatch.setitem(sys.modules, "tushare", tushare)
    monkeypatch.setitem(sys.modules, "notify", notify)
    monkeypatch.setenv("TUSHARE_TOKEN", "unit-test-token")
    nas = tmp_path / "nas"
    nas.mkdir()
    monkeypatch.setenv("QI_SHARED_DIR", str(nas))

    path = BACKUP_DIR / "run_daily.py"
    spec = importlib.util.spec_from_file_location("rdagent_backup_run_daily", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_run_daily_ipo_message_format_matches_its_arguments():
    tree = _tree("run_daily.py")
    expressions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
        and "打新 今日可申购" in node.left.value
    ]

    assert len(expressions) == 1
    expression = expressions[0]
    assert isinstance(expression.right, ast.Tuple)

    conversions = re.findall(r"(?<!%)%([ds])", expression.left.value)
    assert len(conversions) == len(expression.right.elts)

    sample_arguments = tuple(1 if conversion == "d" else "sample" for conversion in conversions)
    rendered = expression.left.value % sample_arguments
    assert "1只" in rendered
    assert "sample" in rendered


def test_run_daily_token_uses_private_sources_without_an_embedded_secret():
    source = _source("run_daily.py")
    assert 'os.environ.get("TUSHARE_TOKEN", "").strip()' in source
    assert 'TOKEN_FILE=r"C:\\rdagent\\data\\.tushare_token"' in source
    assert 'open(TOKEN_FILE, encoding="utf-8").read().strip()' in source
    assert not re.search(r"(?i)(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])", source)


def test_run_daily_subprocess_execution_is_checked():
    tree = _tree("run_daily.py")
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]

    assert len(calls) == 1
    check = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "check")
    assert isinstance(check, ast.Constant)
    assert check.value is True


def test_qlib_coverage_status_uses_ascii_markers():
    source = _source("verify_and_backfill_qlib.py")

    assert "✓" not in source
    assert "⚠" not in source
    assert "[OK] 全覆盖" in source
    assert "[WARN] 仍有" in source


def test_failed_child_does_not_publish_stale_output(
    run_daily_module, monkeypatch, tmp_path
):
    runtime = tmp_path / "runtime"
    nas = tmp_path / "publish"
    runtime.mkdir()
    nas.mkdir()
    (runtime / "result.json").write_text('{"stale": true}', encoding="utf-8")
    destination = nas / "result.json"
    destination.write_text('{"current": true}', encoding="utf-8")
    run_daily_module.RDAGENT_DIR = str(runtime)
    run_daily_module.NAS = str(nas)

    def fail_child(args, **kwargs):
        assert kwargs["check"] is True
        raise subprocess.CalledProcessError(9, args)

    monkeypatch.setattr(run_daily_module.subprocess, "run", fail_child)
    failures = []

    assert not run_daily_module.run_and_publish(
        "child.py", ("result.json",), failures
    )
    assert destination.read_text(encoding="utf-8") == '{"current": true}'
    assert len(failures) == 1


def test_success_exit_without_fresh_runtime_output_is_not_published(
    run_daily_module, monkeypatch, tmp_path
):
    runtime = tmp_path / "runtime"
    nas = tmp_path / "publish"
    runtime.mkdir()
    nas.mkdir()
    source = runtime / "fraud_avoid.json"
    source.write_text('{"stale": true}', encoding="utf-8")
    os.utime(source, (1, 1))
    destination = nas / source.name
    destination.write_text('{"current": true}', encoding="utf-8")
    run_daily_module.RDAGENT_DIR = str(runtime)
    run_daily_module.NAS = str(nas)
    monkeypatch.setattr(run_daily_module, "run", lambda *_a, **_k: True)
    failures = []

    assert not run_daily_module.run_and_publish(
        "export_fundamentals.py", (source.name,), failures
    )
    assert destination.read_text(encoding="utf-8") == '{"current": true}'
    assert len(failures) == 1


@pytest.mark.parametrize("payload", ['{\"broken\": }', '{\"value\": NaN}'])
def test_invalid_json_does_not_replace_published_file(
    run_daily_module, tmp_path, payload
):
    source = tmp_path / "source.json"
    destination = tmp_path / "published.json"
    source.write_text(payload, encoding="utf-8")
    destination.write_text('{\"old\": true}', encoding="utf-8")
    failures = []

    assert not run_daily_module.publish_json(str(source), str(destination), failures)
    assert destination.read_text(encoding="utf-8") == '{\"old\": true}'
    assert len(failures) == 1
    assert not list(tmp_path.glob(".published.json.*.tmp"))


def test_valid_json_is_published_with_atomic_replace(
    run_daily_module, monkeypatch, tmp_path
):
    source = tmp_path / "source.json"
    destination = tmp_path / "published.json"
    payload = b'\xef\xbb\xbf{\"value\": 1}\n'
    source.write_bytes(payload)
    destination.write_text('{\"old\": true}', encoding="utf-8")
    real_replace = os.replace
    replace_calls = []

    def record_replace(temp_path, destination_path):
        replace_calls.append((Path(temp_path), Path(destination_path)))
        real_replace(temp_path, destination_path)

    monkeypatch.setattr(run_daily_module.os, "replace", record_replace)
    failures = []

    assert run_daily_module.publish_json(str(source), str(destination), failures)
    assert failures == []
    assert destination.read_bytes() == payload
    assert len(replace_calls) == 1
    assert replace_calls[0][0].parent == destination.parent
    assert replace_calls[0][1] == destination
    assert not list(tmp_path.glob(".published.json.*.tmp"))


def test_main_returns_failure_when_children_fail(
    run_daily_module, monkeypatch, tmp_path
):
    monkeypatch.setattr(run_daily_module, "is_trade_day", lambda _failures: True)
    monkeypatch.setattr(
        run_daily_module.time,
        "localtime",
        lambda: types.SimpleNamespace(tm_wday=1),
    )
    run_daily_module.STATE = str(tmp_path / "state.json")
    run_daily_module.RDAGENT_DIR = str(tmp_path / "runtime")
    run_daily_module.PROJECT_DIR = str(tmp_path / "project")
    run_daily_module.NAS = str(tmp_path / "nas")

    def fail_command(_args, *, failures, label, **_kwargs):
        failures.append(f"{label} failed")
        return False

    monkeypatch.setattr(run_daily_module, "run_command", fail_command)

    assert run_daily_module.main() == 1


def test_calendar_api_failure_uses_local_calendar_or_fails(
    run_daily_module, monkeypatch, tmp_path
):
    class OfflinePro:
        def trade_cal(self, **_kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(run_daily_module.ts, "pro_api", lambda _token: OfflinePro())
    calendar = tmp_path / "day.txt"
    calendar.write_text(
        "2026-07-10\n2026-07-13\n2026-07-14\n", encoding="utf-8"
    )
    run_daily_module.QLIB_CALENDAR = str(calendar)

    run_daily_module.TODAY = "20260713"
    failures = []
    assert run_daily_module.is_trade_day(failures) is True
    assert failures == []

    run_daily_module.TODAY = "20260712"
    assert run_daily_module.is_trade_day(failures) is False
    assert failures == []

    run_daily_module.QLIB_CALENDAR = str(tmp_path / "missing.txt")
    assert run_daily_module.is_trade_day(failures) is None
    assert len(failures) == 1


def test_nas_path_uses_reachable_mapping_then_unc_fallback(
    run_daily_module, monkeypatch, tmp_path
):
    mapped = tmp_path / "mapped"
    override = tmp_path / "override"
    run_daily_module.MAPPED_NAS = str(mapped)
    run_daily_module.UNC_NAS = r"\\server\share\csv_tmp"
    monkeypatch.delenv("QI_SHARED_DIR", raising=False)
    monkeypatch.delenv("QI_SHARED_UNC", raising=False)

    assert run_daily_module.resolve_nas_path() == run_daily_module.UNC_NAS
    monkeypatch.setenv("QI_SHARED_UNC", r"\\backup\share\csv_tmp")
    assert run_daily_module.resolve_nas_path() == r"\\backup\share\csv_tmp"
    monkeypatch.delenv("QI_SHARED_UNC")
    mapped.mkdir()
    assert run_daily_module.resolve_nas_path() == str(mapped)
    monkeypatch.setenv("QI_SHARED_DIR", str(override))
    assert run_daily_module.resolve_nas_path() == str(override)


def test_project_exporter_must_create_a_fresh_output(
    run_daily_module, monkeypatch, tmp_path
):
    project = tmp_path / "project"
    data = project / "data"
    data.mkdir(parents=True)
    stale = data / "index_inclusion.json"
    stale.write_text('{"updated_at": "2020-01-01"}', encoding="utf-8")
    os.utime(stale, (1, 1))
    run_daily_module.PROJECT_DIR = str(project)

    monkeypatch.setattr(run_daily_module, "run_command", lambda *_a, **_k: True)
    failures = []

    assert not run_daily_module.run_project_and_publish(
        "export_index_inclusion.py",
        ("index_inclusion.json",),
        failures,
    )
    assert len(failures) == 1
    assert "无本次新产出" in failures[0]


def test_weekly_fundamentals_publish_both_risk_outputs():
    source = _source("run_daily.py")
    assert '("fundamentals.json", "margin_avoid.json", "fraud_avoid.json")' in source
    assert 'state["last_weekly_refresh"] = weekly_slot' in source


def test_daily_console_supplemental_outputs_are_scheduled_by_runtime():
    source = _source("run_daily.py")
    assert '"export_index_inclusion.py"' in source
    assert '"export_index_inclusion_pro.py"' in source
    assert '"build_rolling_earnings.py"' in source
