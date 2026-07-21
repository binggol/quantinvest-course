from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import repair_qlib_tail


FIELDS = repair_qlib_tail.FIELDS


def _write_bin(path: Path, start: int, values: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.asarray([float(start), *values], dtype="<f4").tofile(path)


def _read_values(root: Path, code: str, field: str) -> np.ndarray:
    return np.fromfile(root / "features" / code / f"{field}.day.bin", dtype="<f4")


def _make_root(tmp_path: Path, dates: list[str], stocks: dict[str, dict[str, list[float]]]) -> Path:
    root = tmp_path / "cn_data"
    (root / "calendars").mkdir(parents=True)
    (root / "instruments").mkdir()
    (root / "calendars" / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")
    lines = []
    for code, fields in stocks.items():
        count = len(fields["close"])
        for field in FIELDS:
            _write_bin(root / "features" / code / f"{field}.day.bin", 0, fields[field])
        lines.append(f"{code}\t{dates[0]}\t{dates[count - 1]}")
    lines.append("sh000300\t2005-04-08\t2026-07-13\textra-column")
    (root / "instruments" / "all.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _stock_fields(close: float = 10.0, adj: float = 2.0) -> dict[str, list[float]]:
    return {
        "open": [close - 1.0],
        "close": [close],
        "high": [close + 1.0],
        "low": [close - 2.0],
        "volume": [100.0],
        "change": [0.01],
        "factor": [1.0],
        "adj": [adj],
    }


def _daily_row(
    code: str,
    date_iso: str,
    *,
    open_: float,
    close: float,
    high: float,
    low: float,
    adj: float,
) -> dict[str, object]:
    exchange, digits = code[:2], code[2:]
    return {
        "ts_code": f"{digits}.{exchange.upper()}",
        "trade_date": date_iso.replace("-", ""),
        "open": open_,
        "close": close,
        "high": high,
        "low": low,
        "vol": 200.0,
        "pct_chg": 2.5,
        "adj_factor": adj,
    }


def _write_daily(directory: Path, date_iso: str, rows: list[dict[str, object]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(directory / f"{date_iso.replace('-', '')}.parquet", index=False)


def test_one_day_tail_append_and_metadata_rebuild(tmp_path):
    dates = ["2026-07-10", "2026-07-13"]
    root = _make_root(tmp_path, dates, {"sz000001": _stock_fields()})
    parquet = tmp_path / "parquet"
    _write_daily(parquet, dates[1], [
        _daily_row("sz000001", dates[1], open_=10.0, close=11.0, high=12.0, low=9.0, adj=2.0)
    ])

    summary = repair_qlib_tail.repair_qlib_tail(root, parquet)

    assert summary["status"] == "ok"
    assert summary["repaired_stocks"] == 1
    assert summary["bins_replaced"] == 8
    assert summary["dates_loaded"] == ["2026-07-13"]
    assert np.allclose(_read_values(root, "sz000001", "close"), [0.0, 10.0, 11.0])
    assert np.allclose(_read_values(root, "sz000001", "change"), [0.0, 0.01, 0.025])
    metadata = (root / "instruments" / "all.txt").read_text(encoding="utf-8").splitlines()
    assert "sz000001\t2026-07-10\t2026-07-13" in metadata
    assert "sh000300\t2005-04-08\t2026-07-13\textra-column" in metadata


def test_tail_repair_accepts_and_preserves_only_initial_undefined_change(tmp_path):
    dates = ["2026-07-10", "2026-07-13"]
    fields = _stock_fields()
    fields["change"] = [np.nan]
    root = _make_root(tmp_path, dates, {"bj920017": fields})
    parquet = tmp_path / "parquet"
    _write_daily(parquet, dates[1], [
        _daily_row(
            "bj920017", dates[1], open_=10.0, close=11.0,
            high=12.0, low=9.0, adj=2.0,
        )
    ])

    summary = repair_qlib_tail.repair_qlib_tail(root, parquet)

    assert summary["repaired_stocks"] == 1
    change = _read_values(root, "bj920017", "change")
    assert np.isnan(change[1])
    assert change[2] == pytest.approx(0.025)


@pytest.mark.parametrize("bad_change", [[0.01, np.nan], [np.inf]])
def test_tail_repair_rejects_non_initial_nan_or_infinite_change(tmp_path, bad_change):
    dates = ["2026-07-10", "2026-07-13"][:len(bad_change)]
    fields = {
        field: values * len(bad_change)
        for field, values in _stock_fields().items()
    }
    fields["change"] = bad_change
    root = _make_root(tmp_path, dates, {"bj920017": fields})

    with pytest.raises(RuntimeError, match="change bin contains non-finite"):
        repair_qlib_tail._validate_bins(root, "bj920017", len(dates))


def test_multi_day_append_rescales_old_ohlc_and_loads_each_parquet_once(tmp_path, monkeypatch):
    dates = ["2026-07-09", "2026-07-10", "2026-07-13"]
    root = _make_root(tmp_path, dates, {"sz000001": _stock_fields()})
    parquet = tmp_path / "parquet"
    _write_daily(parquet, dates[1], [
        _daily_row("sz000001", dates[1], open_=11.0, close=12.0, high=13.0, low=10.0, adj=3.0)
    ])
    _write_daily(parquet, dates[2], [
        _daily_row("sz000001", dates[2], open_=13.0, close=14.0, high=15.0, low=12.0, adj=4.0)
    ])
    real_read = repair_qlib_tail.pd.read_parquet
    calls: list[str] = []

    def counted_read(path, *args, **kwargs):
        calls.append(Path(path).name)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(repair_qlib_tail.pd, "read_parquet", counted_read)
    summary = repair_qlib_tail.repair_qlib_tail(root, parquet, through="20260713")

    assert calls.count("20260710.parquet") == 1
    assert calls.count("20260713.parquet") == 1
    assert summary["rescaled_stocks"] == 1
    # Old denominator 2 becomes 4; new observations use raw_price * adj / 4.
    assert np.allclose(_read_values(root, "sz000001", "close"), [0.0, 5.0, 9.0, 14.0])
    assert np.allclose(_read_values(root, "sz000001", "open"), [0.0, 4.5, 8.25, 13.0])
    assert np.allclose(_read_values(root, "sz000001", "adj"), [0.0, 2.0, 3.0, 4.0])


def test_missing_intermediate_stock_row_uses_suspension_placeholder(tmp_path):
    dates = ["2026-07-09", "2026-07-10", "2026-07-13"]
    root = _make_root(tmp_path, dates, {"sz000001": _stock_fields()})
    parquet = tmp_path / "parquet"
    _write_daily(parquet, dates[1], [
        _daily_row("sz000002", dates[1], open_=8.0, close=8.5, high=9.0, low=7.5, adj=1.0)
    ])
    _write_daily(parquet, dates[2], [
        _daily_row("sz000001", dates[2], open_=13.0, close=14.0, high=15.0, low=12.0, adj=4.0)
    ])
    summary = repair_qlib_tail.repair_qlib_tail(root, parquet)

    assert summary["suspension_rows_filled"] == 1
    # Old and suspension rows have adj=2, then the new max adj=4 rescales both.
    assert _read_values(root, "sz000001", "close").tolist() == pytest.approx(
        [0.0, 5.0, 5.0, 14.0]
    )
    assert _read_values(root, "sz000001", "volume").tolist() == pytest.approx(
        [0.0, 100.0, 0.0, 200.0]
    )


@pytest.mark.parametrize("problem", ["invalid_source", "misaligned_bins"])
def test_invalid_input_fails_before_writing_other_valid_stock(tmp_path, problem):
    dates = ["2026-07-10", "2026-07-13"]
    root = _make_root(
        tmp_path,
        dates,
        {"sz000001": _stock_fields(), "sz000002": _stock_fields(close=8.0, adj=1.0)},
    )
    parquet = tmp_path / "parquet"
    rows = [
        _daily_row("sz000001", dates[1], open_=10.0, close=11.0, high=12.0, low=9.0, adj=2.0),
        _daily_row("sz000002", dates[1], open_=8.0, close=8.5, high=9.0, low=7.5, adj=1.0),
    ]
    if problem == "invalid_source":
        rows[1]["high"] = 7.0
    else:
        _write_bin(root / "features" / "sz000002" / "adj.day.bin", 1, [1.0])
    _write_daily(parquet, dates[1], rows)
    old_bins = {
        (code, field): (root / "features" / code / f"{field}.day.bin").read_bytes()
        for code in ("sz000001", "sz000002") for field in FIELDS
    }

    with pytest.raises(RuntimeError):
        repair_qlib_tail.repair_qlib_tail(root, parquet)

    for (code, field), payload in old_bins.items():
        assert (root / "features" / code / f"{field}.day.bin").read_bytes() == payload


def test_metadata_uses_actual_close_tail_and_dry_run_publishes_nothing(tmp_path):
    dates = ["2026-07-10", "2026-07-13"]
    fields = _stock_fields()
    fields = {field: values * 2 for field, values in fields.items()}
    root = _make_root(tmp_path, dates, {"sz000001": fields})
    metadata_path = root / "instruments" / "all.txt"
    metadata_path.write_text(
        "sz000001\t2026-07-10\t2026-07-10\n"
        "sh000300\t2005-04-08\t2026-07-13\textra-column\n",
        encoding="utf-8",
    )
    parquet = tmp_path / "parquet"
    _write_daily(parquet, dates[1], [
        _daily_row("sz000001", dates[1], open_=10.0, close=11.0, high=12.0, low=9.0, adj=2.0)
    ])
    before = metadata_path.read_bytes()

    summary = repair_qlib_tail.repair_qlib_tail(root, parquet, dry_run=True)

    assert summary["repaired_stocks"] == 0
    assert summary["already_current"] == 1
    assert summary["metadata_changed"] is True
    assert metadata_path.read_bytes() == before

    summary = repair_qlib_tail.repair_qlib_tail(root, parquet)
    assert summary["bins_replaced"] == 0
    assert "sz000001\t2026-07-10\t2026-07-13" in metadata_path.read_text(encoding="utf-8")
    assert "sh000300\t2005-04-08\t2026-07-13\textra-column" in metadata_path.read_text(encoding="utf-8")


def test_missing_target_stock_bins_fail_closed(tmp_path):
    dates = ["2026-07-10", "2026-07-13"]
    root = _make_root(tmp_path, dates, {"sz000001": _stock_fields()})
    parquet = tmp_path / "parquet"
    _write_daily(parquet, dates[1], [
        _daily_row(
            "sz000001", dates[1], open_=10.0, close=11.0, high=12.0, low=9.0, adj=1.0
        ),
        _daily_row(
            "sz000002", dates[1], open_=20.0, close=21.0, high=22.0, low=19.0, adj=1.0
        ),
    ])

    with pytest.raises(RuntimeError, match="full rebuild required"):
        repair_qlib_tail.repair_qlib_tail(root, parquet)


def test_already_current_stock_repairs_legacy_envelope(tmp_path):
    dates = ["2026-07-10", "2026-07-13"]
    fields = _stock_fields()
    fields = {field: values * 2 for field, values in fields.items()}
    fields["low"][0] = 10.5  # Higher than close=10.0 in the old row.
    root = _make_root(tmp_path, dates, {"sz000001": fields})
    parquet = tmp_path / "parquet"
    _write_daily(parquet, dates[1], [
        _daily_row(
            "sz000001", dates[1], open_=10.0, close=11.0, high=12.0, low=9.0, adj=2.0
        )
    ])

    summary = repair_qlib_tail.repair_qlib_tail(root, parquet)

    assert summary["tail_appended_stocks"] == 0
    assert summary["legacy_envelope_rows_repaired"] == 1
    assert summary["bins_replaced"] == 8
    low = _read_values(root, "sz000001", "low")
    assert low.tolist() == pytest.approx([0.0, 9.0, 8.0])


def test_orphan_feature_directory_is_not_republished(tmp_path):
    dates = ["2026-07-10", "2026-07-13"]
    fields = _stock_fields()
    fields = {field: values * 2 for field, values in fields.items()}
    root = _make_root(tmp_path, dates, {"sz000001": fields})
    (root / "features" / "bj430017").mkdir(parents=True)
    parquet = tmp_path / "parquet"
    _write_daily(parquet, dates[1], [
        _daily_row(
            "sz000001", dates[1], open_=10.0, close=11.0, high=12.0, low=9.0, adj=2.0
        )
    ])

    repair_qlib_tail.repair_qlib_tail(root, parquet)

    metadata = (root / "instruments" / "all.txt").read_text(encoding="utf-8")
    assert "bj430017" not in metadata
