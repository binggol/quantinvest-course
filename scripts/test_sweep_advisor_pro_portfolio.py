from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import sweep_advisor_pro_portfolio as sweep


def _clean_python_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return environment


def test_direct_script_help_works_outside_repository(tmp_path):
    script = Path(sweep.__file__).resolve()

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=_clean_python_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--signal-cache" in completed.stdout


def test_direct_script_bootstrap_can_import_backtest_module_without_running_sweep(tmp_path):
    script = Path(sweep.__file__).resolve()
    command = (
        "import importlib, runpy; "
        f"runpy.run_path({str(script)!r}, run_name='sweep_direct_import'); "
        "importlib.import_module('scripts.backtest_advisor_pro_frequency'); "
        "print('direct-import-ok')"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=tmp_path,
        env=_clean_python_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "direct-import-ok"


def test_default_grid_has_80_unique_runs_and_expected_modes():
    runs = sweep.generate_grid(signal_cache_identity="cache-v1")

    assert len(runs) == 80
    assert len({run.run_id for run in runs}) == 80
    assert {
        (run.spec["portfolio_topn"], run.spec["frequency_days"])
        for run in runs
    } == {
        (topn, frequency)
        for topn in sweep.DEFAULT_TOPNS
        for frequency in sweep.DEFAULT_FREQUENCIES
    }
    assert all(
        (run.spec["rebalance_mode"] == "target_weight")
        == (run.spec["replacement_ratio"] == 1.0)
        for run in runs
    )


def test_grid_deduplicates_equal_effective_replacement_caps():
    runs = sweep.generate_grid(
        topns=[8],
        frequencies=[5],
        replacement_ratios=[0.10, 0.11, 0.20, 1.0],
        accounts=[10_000_000],
        signal_cache_identity="cache-v1",
    )

    assert len(runs) == 3
    assert {
        (run.spec["max_replacements"], run.spec["rebalance_mode"])
        for run in runs
    } == {(1, "replace_only"), (2, "replace_only"), (None, "target_weight")}


def test_stable_run_id_covers_nested_parameters_but_not_mapping_order():
    base = {
        "portfolio_topn": 10,
        "frequency_days": 5,
        "frequency_offset": 0,
        "replacement_ratio": 0.2,
        "rebalance_mode": "replace_only",
        "run_parameters": {"commission": 0.0003, "retry_days": 5},
    }
    reordered = dict(reversed(list(base.items())))
    reordered["run_parameters"] = {"retry_days": 5, "commission": 0.0003}

    assert sweep.stable_run_id(base) == sweep.stable_run_id(reordered)
    changed = {**base, "run_parameters": {"commission": 0.0004, "retry_days": 5}}
    assert sweep.stable_run_id(base) != sweep.stable_run_id(changed)


def test_hash_chunks_are_disjoint_and_cover_the_grid():
    runs = sweep.generate_grid(signal_cache_identity="cache-v1")
    chunks = [
        sweep.select_chunk(runs, chunk_index=index, chunk_count=7)
        for index in range(7)
    ]
    ids = [{run.run_id for run in chunk} for chunk in chunks]

    assert set().union(*ids) == {run.run_id for run in runs}
    assert sum(len(chunk) for chunk in ids) == len(runs)
    assert all(ids[left].isdisjoint(ids[right]) for left in range(7) for right in range(left))


def test_compact_result_removes_large_paths_recursively():
    result = sweep.compact_result(
        {
            "signals": [1, 2],
            "daily_path": [3, 4],
            "metrics": {
                "sharpe": 1.5,
                "nested": {"daily_path": [5], "max_drawdown": -0.2},
            },
        }
    )

    assert result == {
        "metrics": {"sharpe": 1.5, "nested": {"max_drawdown": -0.2}}
    }


class FakeQlib:
    def __init__(self):
        self.calls = []

    def init(self, **kwargs):
        self.calls.append(kwargs)


@dataclass(frozen=True)
class FakePortfolioSpec:
    portfolio_topn: int
    max_replacements: int | None
    rebalance_mode: str
    account: float | None = None


class FakeBacktest:
    PortfolioSpec = FakePortfolioSpec

    def __init__(self, fail_max_replacements=object()):
        self.calls = []
        self.fail_max_replacements = fail_max_replacements

    def run_frequency(
        self, args, records, frequency_days, frequency_offset=0, spec=None
    ):
        self.calls.append(
            {
                "args": args,
                "records_id": id(records),
                "frequency_days": frequency_days,
                "frequency_offset": frequency_offset,
                "spec": spec,
            }
        )
        if spec.max_replacements == self.fail_max_replacements:
            raise RuntimeError(f"failed cap {spec.max_replacements}")
        period = {
            "n": 750,
            "annualized_return": 0.12,
            "sharpe": 1.1,
            "calmar": 0.8,
            "rolling_252d_sharpe_p10": 0.2,
            "max_drawdown": -0.15,
            "worst_60d": -0.08,
            "double_cost_annualized_return": 0.11,
        }
        return {
            "frequency_days": frequency_days,
            "evaluation_periods": {
                "validation_2022_2024": {
                    "exposure_matched_hedged": period,
                },
                "recent_2025_plus": {"exposure_matched_hedged": period},
            },
            "execution": {
                "attempts": 10,
                "unfilled": 1,
                "no_fill_rate": 0.1,
                "annualized_one_way_turnover": 2.0,
                "final_holding_count": 0,
                "settlement_mode": "liquidated",
            },
            "signals": [{"large": "payload"}],
            "daily_path": [{"large": "payload"}],
        }


def _args(tmp_path: Path, *, ratios: str = "10%", resume: bool = False):
    argv = [
        "--qlib-data",
        str(tmp_path / "qlib"),
        "--signal-cache",
        str(tmp_path / "signals.pkl"),
        "--out",
        str(tmp_path / "summary.json"),
        "--checkpoint-dir",
        str(tmp_path / "checkpoints"),
        "--portfolio-topns",
        "8",
        "--frequencies",
        "5",
        "--replacement-ratios",
        ratios,
        "--accounts",
        "10000000",
    ]
    if resume:
        argv.append("--resume")
    return sweep.build_parser().parse_args(argv)


def _cache_loader_counter():
    state = {"calls": 0, "records": [{"ranked_codes": ["SH600000"]}]}

    def load(_path):
        state["calls"] += 1
        return {
            "signature": {"scoring_schema": "test-v1"},
            "records": state["records"],
        }

    return state, load


def test_execute_initializes_and_loads_once_and_writes_compact_checkpoint(tmp_path):
    qlib = FakeQlib()
    backtest = FakeBacktest()
    cache_state, loader = _cache_loader_counter()

    summary = sweep.execute_sweep(
        _args(tmp_path),
        qlib_module=qlib,
        backtest_module=backtest,
        cache_loader=loader,
    )

    assert len(qlib.calls) == 1
    assert cache_state["calls"] == 1
    assert len(backtest.calls) == 1
    assert backtest.calls[0]["records_id"] == id(cache_state["records"])
    assert backtest.calls[0]["spec"] == FakePortfolioSpec(8, 1, "replace_only", 10_000_000)
    assert summary["status"] == "done"
    assert summary["success_count"] == 1
    checkpoint = summary["runs"][0]
    assert "signals" not in checkpoint["result"]
    assert "daily_path" not in checkpoint["result"]
    assert json.loads(Path(summary["out"]).read_text(encoding="utf-8"))["status"] == "done"
    assert len(list((tmp_path / "checkpoints").glob("*.json"))) == 1
    assert not list((tmp_path / "checkpoints").glob("*.tmp"))


def test_failure_is_checkpointed_remaining_runs_continue_and_resume_only_retries_failure(
    tmp_path,
):
    cache_state, loader = _cache_loader_counter()
    failing = FakeBacktest(fail_max_replacements=None)
    first = sweep.execute_sweep(
        _args(tmp_path, ratios="10%,100%"),
        qlib_module=FakeQlib(),
        backtest_module=failing,
        cache_loader=loader,
    )

    assert len(failing.calls) == 2
    assert first["status"] == "failed"
    assert first["success_count"] == 1
    assert first["failed_count"] == 1
    failed = next(item for item in first["runs"] if item["status"] == "failed")
    assert failed["error"]["type"] == "RuntimeError"

    recovered = FakeBacktest()
    second = sweep.execute_sweep(
        _args(tmp_path, ratios="10%,100%", resume=True),
        qlib_module=FakeQlib(),
        backtest_module=recovered,
        cache_loader=loader,
    )

    assert len(recovered.calls) == 1
    assert recovered.calls[0]["spec"].max_replacements is None
    assert second["status"] == "done"
    assert second["success_count"] == 2
    assert second["failed_count"] == 0
    assert second["resumed_count"] == 1
    assert cache_state["calls"] == 2


def test_load_signal_cache_rejects_old_truncated_rankings(tmp_path):
    cache_path = tmp_path / "signals.pkl"
    import pickle

    with cache_path.open("wb") as handle:
        pickle.dump({"records": [{"codes": ["SH600000"]}]}, handle)

    with pytest.raises(ValueError, match="ranked_codes"):
        sweep.load_signal_cache(cache_path)
