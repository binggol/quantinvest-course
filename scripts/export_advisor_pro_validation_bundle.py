"""Export frozen targets and quote inputs for the independent vn.py validator."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import qlib
from qlib.data import D

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_engine.validation_bundle import build_bundle


QUOTE_FIELDS = ["$open", "$high", "$low", "$close", "$change", "$volume", "$adj"]


def normalize_code(value: object) -> str:
    text = str(value).strip().upper()
    if text.startswith(("SH", "SZ", "BJ")) and len(text) >= 8:
        return text[:8]
    digits = "".join(char for char in text if char.isdigit())[-6:]
    if len(digits) != 6:
        raise ValueError(f"invalid instrument code: {value!r}")
    if digits.startswith(("4", "8", "920")):
        return "BJ" + digits
    return ("SH" if digits.startswith(("5", "6", "9")) else "SZ") + digits


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_targets(periods: list[dict]) -> dict[str, dict[str, float]]:
    targets = {str(period["exit_date"]): {} for period in periods}
    for period in periods:
        codes = [normalize_code(code) for code in period["codes"]]
        basket_n = int(period.get("basket_n") or len(codes))
        if basket_n != len(codes) or basket_n <= 0:
            raise ValueError(f"invalid basket size on {period.get('entry_date')}")
        weight = 1.0 / basket_n
        targets[str(period["entry_date"])] = {code: weight for code in codes}
    return dict(sorted(targets.items()))


def adjustment_maxima(codes: list[str]) -> dict[str, float]:
    # Stored Qlib prices are normalized by each instrument's full-series max
    # adjustment factor, so the bundle must freeze the same normalization base.
    values = D.features(codes, ["$adj"], start_time="2000-01-01", freq="day")
    if values.empty:
        raise ValueError("Qlib adjustment-factor data is empty")
    series = pd.to_numeric(values["$adj"], errors="coerce")
    series.index = pd.MultiIndex.from_arrays(
        [
            [normalize_code(value) for value in series.index.get_level_values("instrument")],
            pd.to_datetime(series.index.get_level_values("datetime")).normalize(),
        ],
        names=["instrument", "datetime"],
    )
    maxima = series.groupby(level="instrument").max()
    result = {code: float(maxima.get(code, np.nan)) for code in codes}
    invalid = [code for code, value in result.items() if not np.isfinite(value) or value <= 0]
    if invalid:
        raise ValueError(f"missing adjustment maxima for: {invalid[:10]}")
    return result


def export_quotes(
    codes: list[str], benchmark: str, start_date: str, end_date: str
) -> tuple[pd.DataFrame, list[str]]:
    calendar = [
        pd.Timestamp(value).normalize()
        for value in D.calendar(start_time=start_date, end_time=end_date, freq="day")
    ]
    if not calendar or calendar[0].strftime("%Y-%m-%d") != start_date or calendar[-1].strftime("%Y-%m-%d") != end_date:
        raise ValueError("Qlib calendar does not exactly cover the requested replay window")
    instruments = sorted(set(codes) | {benchmark})
    frame = D.features(
        instruments,
        QUOTE_FIELDS,
        start_time=start_date,
        end_time=end_date,
        freq="day",
    )
    if frame.empty:
        raise ValueError("Qlib quote query returned no data")
    frame.index = pd.MultiIndex.from_arrays(
        [
            [normalize_code(value) for value in frame.index.get_level_values("instrument")],
            pd.to_datetime(frame.index.get_level_values("datetime")).normalize(),
        ],
        names=["instrument", "datetime"],
    )
    full_index = pd.MultiIndex.from_product(
        [instruments, calendar], names=["instrument", "datetime"]
    )
    frame = frame.reindex(full_index)
    maxima = adjustment_maxima(codes)
    benchmark_max = 1.0
    frame = frame.reset_index()
    frame["instrument"] = frame["instrument"].map(normalize_code)
    frame["date"] = pd.to_datetime(frame.pop("datetime")).dt.strftime("%Y-%m-%d")
    frame = frame.rename(
        columns={
            "$open": "open",
            "$high": "high",
            "$low": "low",
            "$close": "close",
            "$change": "change",
            "$volume": "volume_lots",
            "$adj": "adj",
        }
    )
    frame["max_adj"] = frame["instrument"].map({**maxima, benchmark: benchmark_max})
    benchmark_mask = frame["instrument"].eq(benchmark)
    frame.loc[benchmark_mask, "adj"] = benchmark_max
    frame["rule_source"] = np.where(benchmark_mask, "benchmark", "board_fallback")
    columns = [
        "date",
        "instrument",
        "open",
        "high",
        "low",
        "close",
        "change",
        "volume_lots",
        "adj",
        "max_adj",
        "rule_source",
    ]
    return frame[columns], [value.strftime("%Y-%m-%d") for value in calendar]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit", default="data/advisor_pro_execution_audit_published_fixed.json"
    )
    parser.add_argument(
        "--out", default="data/backtest_bundles/advisor_pro_published_fixed"
    )
    parser.add_argument("--qlib-data", default=r"C:\qlib_data\cn_data")
    parser.add_argument("--benchmark", default="SH000300")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    audit_path = Path(args.audit)
    audit = read_json(audit_path)
    periods = audit.get("periods") or []
    if not periods:
        raise ValueError("source audit has no periods")
    targets = build_targets(periods)
    start_date = min(targets)
    end_date = str(
        (audit.get("config") or {}).get("backtest_end_after_final_retry") or max(targets)
    )
    benchmark = normalize_code(args.benchmark)
    codes = sorted({code for weights in targets.values() for code in weights})

    qlib.init(provider_uri=str(Path(args.qlib_data)), region="cn")
    quotes, calendar = export_quotes(codes, benchmark, start_date, end_date)
    missing_targets = sorted(set(targets) - set(calendar))
    if missing_targets:
        raise ValueError(f"target dates are absent from the trading calendar: {missing_targets[:10]}")

    audit_config = dict(audit.get("config") or {})
    config = {
        key: audit_config[key]
        for key in (
            "account",
            "risk_degree",
            "retry_days",
            "commission",
            "max_volume_participation",
            "impact_cost",
            "hedge_yearly_cost",
            "trade_unit",
            "volume_unit_multiplier",
            "backtest_end_after_final_retry",
        )
        if key in audit_config
    }
    config.update(
        {
            "schema_version": 1,
            "benchmark": benchmark,
            "min_cost": 5.0,
            "deal_price": "open",
            "target_timing": "same-day open for the frozen target date",
            "order_sequence": "instrument-sorted sells before instrument-sorted buys",
            "target_collision": "entry overrides same-day exit",
            "share_unit": "normalized_shares = raw_shares / (daily_adj / max_adj)",
        }
    )
    provenance = {
        "schema_version": 1,
        "source_audit": {
            "logical_name": audit_path.name,
            "sha256": sha256_file(audit_path),
            "size": audit_path.stat().st_size,
            "schema_version": audit.get("schema_version"),
            "regime_mode": audit_config.get("regime_mode"),
        },
        "quote_source": {
            "kind": "Qlib local binary provider",
            "fields": QUOTE_FIELDS,
            "calendar_start": calendar[0],
            "calendar_end": calendar[-1],
            "calendar_days": len(calendar),
            "instrument_count": len(codes),
        },
        "independence_boundary": (
            "Targets and raw quote fields are exported; Qlib orders, fills, positions, "
            "daily NAV, and execution metrics are excluded."
        ),
    }
    manifest = build_bundle(Path(args.out), targets, quotes, config, provenance)
    print(
        json.dumps(
            {
                "out": str(Path(args.out).resolve()),
                "targets": len(targets),
                "quote_rows": len(quotes),
                "calendar_days": len(calendar),
                "instruments": len(codes) + 1,
                "source_audit_sha256": provenance["source_audit"]["sha256"],
                "manifest": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
