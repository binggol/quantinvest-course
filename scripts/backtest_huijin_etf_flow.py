"""Build the Huijin-held ETF flow proxy page payload and its timing-safe backtest.

The daily series is *not* Central Huijin's own trading record.  Fund holder
identity comes from periodic reports, while ``fund_share`` is the total ETF
share count created/redeemed by every investor.  We therefore call the daily
series a Huijin-held ETF *flow proxy* and keep point-in-time membership separate
from the longer, selection-biased exploratory history.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = DATA / "etf_flow_cache"
DEFAULT_ROSTER = DATA / "huijin_etf_roster.json"
DEFAULT_OUTPUT = DATA / "huijin_etf_flow.json"
DEFAULT_SERIES_OUTPUT = DATA / "huijin_etf_share_series.json"
FD_SHARE_UNIT = 10_000.0  # Tushare fund_share.fd_share is reported in 万份.
MIN_COVERAGE = 0.95
FLOW_WINDOW = 5
QUANTILE_LOOKBACK = 252
QUANTILE_MIN_PERIODS = 126


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--index", default="000300.SH")
    parser.add_argument("--roster", default=str(DEFAULT_ROSTER))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--series-output", default=str(DEFAULT_SERIES_OUTPUT))
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--execution-lag", type=int, default=2)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.12)
    return parser.parse_args()


def _token() -> str:
    value = os.environ.get("TUSHARE_TOKEN", "").strip()
    if value:
        return value
    path = DATA / ".tushare_token"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _api():
    value = _token()
    if not value:
        raise RuntimeError("缺少 TUSHARE_TOKEN 或 data/.tushare_token")
    return ts.pro_api(value)


def _ymd(value: str | pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def _date_chunks(start: str, end: str, years: int = 4) -> list[tuple[str, str]]:
    left = pd.Timestamp(start).normalize()
    finish = pd.Timestamp(end).normalize()
    chunks: list[tuple[str, str]] = []
    while left <= finish:
        right = min(finish, left + pd.DateOffset(years=years) - pd.Timedelta(days=1))
        chunks.append((_ymd(left), _ymd(right)))
        left = right + pd.Timedelta(days=1)
    return chunks


def _read_frame(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"trade_date": str, "ts_code": str})


def _best_cache(prefix: str) -> Path | None:
    paths = sorted(CACHE.glob(f"{prefix}_*.csv.gz"), key=lambda p: p.name)
    return paths[-1] if paths else None


def cached_history(
    pro,
    endpoint: str,
    prefix: str,
    code: str,
    start: str,
    end: str,
    *,
    refresh: bool,
    sleep_seconds: float,
) -> pd.DataFrame:
    """Read the newest cache or fetch in chunks so Tushare's row cap cannot truncate history."""
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / f"{prefix}_{_ymd(start)}_{_ymd(end)}.csv.gz"
    cached = out if out.exists() else _best_cache(prefix)
    if cached is not None and not refresh:
        frame = _read_frame(cached)
        if "trade_date" in frame.columns:
            dates = frame["trade_date"].astype(str)
            frame = frame[(dates >= _ymd(start)) & (dates <= _ymd(end))]
        return frame.reset_index(drop=True)

    frames: list[pd.DataFrame] = []
    method = getattr(pro, endpoint)
    for chunk_start, chunk_end in _date_chunks(start, end):
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                part = method(ts_code=code, start_date=chunk_start, end_date=chunk_end)
                if part is not None and not part.empty:
                    frames.append(part)
                last_error = None
                break
            except Exception as exc:  # pragma: no cover - network retry
                last_error = exc
                time.sleep(max(0.5, sleep_seconds * (attempt + 2)))
        if last_error is not None:
            raise RuntimeError(f"{endpoint} {code} {chunk_start}-{chunk_end} 抓取失败: {last_error}")
        time.sleep(max(0.0, sleep_seconds))
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not frame.empty:
        keys = [key for key in ("ts_code", "trade_date") if key in frame.columns]
        frame = frame.drop_duplicates(keys or None, keep="first")
    frame.to_csv(out, index=False, compression="gzip")
    return frame.reset_index(drop=True)


def load_roster(path: str | Path) -> tuple[dict, list[dict]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("items") or []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("汇金 ETF 名单为空")
    seen: set[str] = set()
    cleaned: list[dict] = []
    for raw in rows:
        row = dict(raw)
        code = str(row.get("code") or "").upper().strip()
        if not code or code in seen:
            raise RuntimeError(f"汇金 ETF 名单代码无效或重复: {code or raw!r}")
        seen.add(code)
        row["code"] = code
        row["disclosed_on"] = pd.Timestamp(row["disclosed_on"]).strftime("%Y-%m-%d")
        cleaned.append(row)
    return payload, cleaned


def load_index(pro, code: str, start: str, end: str, refresh: bool, sleep_seconds: float) -> pd.DataFrame:
    frame = cached_history(
        pro,
        "index_daily",
        f"index_{code.replace('.', '_')}",
        code,
        start,
        end,
        refresh=refresh,
        sleep_seconds=sleep_seconds,
    )
    if frame.empty or "trade_date" not in frame or "close" not in frame:
        raise RuntimeError(f"指数行情为空: {code}")
    keep = frame[[column for column in ("trade_date", "open", "close") if column in frame]].copy()
    keep["trade_date"] = pd.to_datetime(keep["trade_date"], format="%Y%m%d", errors="coerce")
    keep["close"] = pd.to_numeric(keep["close"], errors="coerce")
    if "open" in keep:
        keep["open"] = pd.to_numeric(keep["open"], errors="coerce")
    return (
        keep.dropna(subset=["trade_date", "close"])
        .drop_duplicates("trade_date", keep="first")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def build_fund_panel(
    share: pd.DataFrame,
    daily: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    roster_item: dict,
) -> pd.DataFrame:
    share_column = next((name for name in ("fd_share", "fund_share", "share") if name in share), None)
    if share_column is None or share.empty or daily.empty or "close" not in daily:
        return pd.DataFrame(index=calendar)
    left = share[["trade_date", share_column]].copy()
    right = daily[["trade_date", "close"]].copy()
    left["date"] = pd.to_datetime(left["trade_date"], format="%Y%m%d", errors="coerce")
    right["date"] = pd.to_datetime(right["trade_date"], format="%Y%m%d", errors="coerce")
    left["share"] = pd.to_numeric(left[share_column], errors="coerce")
    right["price"] = pd.to_numeric(right["close"], errors="coerce")
    left = left.dropna(subset=["date", "share"]).drop_duplicates("date", keep="first")
    right = right.dropna(subset=["date", "price"]).drop_duplicates("date", keep="first")
    frame = pd.DataFrame(index=calendar)
    frame.index.name = "date"
    frame = frame.join(left.set_index("date")[["share"]], how="left")
    frame = frame.join(right.set_index("date")[["price"]], how="left")
    frame["observed"] = frame["share"].notna() & frame["price"].notna()
    frame["share_delta"] = frame["share"] - frame["share"].shift(1)
    frame["share_change_1d_pct"] = (frame["share"] / frame["share"].shift(1) - 1.0) * 100.0
    full_6 = frame["observed"].rolling(FLOW_WINDOW + 1, min_periods=FLOW_WINDOW + 1).sum().eq(FLOW_WINDOW + 1)
    frame["share_change_5d_pct"] = (frame["share"] / frame["share"].shift(FLOW_WINDOW) - 1.0) * 100.0
    frame.loc[~full_6, "share_change_5d_pct"] = np.nan
    share_ratio = frame["share"] / frame["share"].shift(1)
    price_ratio = frame["price"] / frame["price"].shift(1)
    frame["mechanical_adjustment"] = (
        ((share_ratio > 1.30) | (share_ratio < 0.70))
        & ((share_ratio * price_ratio - 1.0).abs() <= 0.15)
    )
    frame["invalid_jump"] = frame["share_change_1d_pct"].abs() > 100.0
    frame["valid_flow"] = (
        frame["observed"]
        & frame["observed"].shift(1, fill_value=False)
        & ~frame["mechanical_adjustment"]
        & ~frame["invalid_jump"]
    )
    frame["aum_yi"] = frame["share"] * frame["price"] / 10_000.0
    frame["lag_aum_yi"] = frame["share"].shift(1) * frame["price"].shift(1) / 10_000.0
    frame["net_creation_yi"] = frame["share_delta"] * frame["price"] / 10_000.0
    frame.loc[~frame["valid_flow"], "net_creation_yi"] = np.nan
    frame["net_creation_5d_yi"] = (
        (frame["share"] - frame["share"].shift(FLOW_WINDOW)) * frame["price"] / 10_000.0
    )
    frame.loc[~full_6, "net_creation_5d_yi"] = np.nan
    frame["lag_aum_5d_yi"] = frame["share"].shift(FLOW_WINDOW) * frame["price"].shift(FLOW_WINDOW) / 10_000.0
    frame["code"] = roster_item["code"]
    valid_dates = frame.index[frame["observed"]]
    frame.attrs["first_date"] = valid_dates.min() if len(valid_dates) else pd.NaT
    frame.attrs["last_date"] = valid_dates.max() if len(valid_dates) else pd.NaT
    return frame


def _rolling_percentile(values: pd.Series, lookback: int = QUANTILE_LOOKBACK) -> pd.Series:
    array = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    out = np.full(len(array), np.nan)
    for index, current in enumerate(array):
        if not np.isfinite(current):
            continue
        history = array[max(0, index - lookback):index]
        history = history[np.isfinite(history)]
        if len(history) < QUANTILE_MIN_PERIODS:
            continue
        out[index] = float((history <= current).mean() * 100.0)
    return pd.Series(out, index=values.index)


def aggregate_panels(
    panels: dict[str, pd.DataFrame],
    roster: list[dict],
    calendar: pd.DatetimeIndex,
    *,
    point_in_time: bool,
) -> pd.DataFrame:
    rows: list[dict] = []
    item_by_code = {item["code"]: item for item in roster if item.get("strategy_eligible", True)}
    for date in calendar:
        active: list[tuple[dict, pd.Series]] = []
        for code, item in item_by_code.items():
            panel = panels.get(code)
            if panel is None or panel.empty or date not in panel.index:
                continue
            first_date = panel.attrs.get("first_date")
            if pd.isna(first_date) or date < first_date:
                continue
            if point_in_time and date < pd.Timestamp(item["disclosed_on"]):
                continue
            active.append((item, panel.loc[date]))
        total = len(active)
        observed = [(item, row) for item, row in active if bool(row.get("observed"))]
        valid_flows = [(item, row) for item, row in active if bool(row.get("valid_flow")) and np.isfinite(row.get("net_creation_yi", np.nan))]
        coverage = len(observed) / total if total else np.nan
        flow_coverage = len(valid_flows) / total if total else np.nan
        full_observation = total > 0 and len(observed) == total
        share_yi = sum(float(row["share"]) for _, row in observed) / 10_000.0 if full_observation else np.nan
        aum_yi = sum(float(row["aum_yi"]) for _, row in observed) if full_observation else np.nan
        net_creation = sum(float(row["net_creation_yi"]) for _, row in valid_flows) if total and flow_coverage >= MIN_COVERAGE else np.nan
        matched_lag_aum = sum(float(row["lag_aum_yi"]) for _, row in valid_flows) if valid_flows else np.nan
        missing_lag_aum = sum(
            float(row["lag_aum_yi"])
            for _, row in active
            if np.isfinite(row.get("lag_aum_yi", np.nan)) and not bool(row.get("valid_flow"))
        )
        all_lag_aum = matched_lag_aum + missing_lag_aum if np.isfinite(matched_lag_aum) else np.nan
        largest_missing_weight = (missing_lag_aum / all_lag_aum) if np.isfinite(all_lag_aum) and all_lag_aum > 0 else 0.0
        if largest_missing_weight > 0.10:
            net_creation = np.nan
        flow_5_rows = [
            (item, row) for item, row in active
            if np.isfinite(row.get("net_creation_5d_yi", np.nan)) and np.isfinite(row.get("lag_aum_5d_yi", np.nan))
        ]
        flow_5_coverage = len(flow_5_rows) / total if total else np.nan
        if total and flow_5_coverage >= MIN_COVERAGE:
            denom = sum(float(row["lag_aum_5d_yi"]) for _, row in flow_5_rows)
            positive = sum(float(row["lag_aum_5d_yi"]) for _, row in flow_5_rows if float(row["net_creation_5d_yi"]) > 0)
            breadth = positive / denom * 100.0 if denom > 0 else np.nan
        else:
            breadth = np.nan
        rows.append({
            "date": date,
            "active_count": total,
            "observed_count": len(observed),
            "coverage_pct": coverage * 100.0 if np.isfinite(coverage) else np.nan,
            "flow_coverage_pct": flow_coverage * 100.0 if np.isfinite(flow_coverage) else np.nan,
            "share_yi": share_yi,
            "aum_yi": aum_yi,
            "net_creation_yi": net_creation,
            "matched_lag_aum_yi": matched_lag_aum,
            "breadth_5d_pct": breadth,
            "anomaly_count": sum(bool(row.get("mechanical_adjustment")) or bool(row.get("invalid_jump")) for _, row in active),
        })
    out = pd.DataFrame(rows).set_index("date")
    out["net_creation_5d_yi"] = out["net_creation_yi"].rolling(FLOW_WINDOW, min_periods=FLOW_WINDOW).sum()
    out["flow_ratio_5d_pct"] = out["net_creation_5d_yi"] / out["matched_lag_aum_yi"].shift(FLOW_WINDOW - 1) * 100.0
    out["flow_percentile"] = _rolling_percentile(out["flow_ratio_5d_pct"])
    out["q20"] = out["flow_ratio_5d_pct"].shift(1).rolling(
        QUANTILE_LOOKBACK, min_periods=QUANTILE_MIN_PERIODS
    ).quantile(0.20)
    out["q80"] = out["flow_ratio_5d_pct"].shift(1).rolling(
        QUANTILE_LOOKBACK, min_periods=QUANTILE_MIN_PERIODS
    ).quantile(0.80)
    calibrated = out[["q20", "q80", "breadth_5d_pct", "flow_ratio_5d_pct"]].notna().all(axis=1)
    out["follow_target"] = np.nan
    out.loc[calibrated, "follow_target"] = 0.5
    out.loc[
        calibrated & (out["flow_ratio_5d_pct"] >= out["q80"]) & (out["breadth_5d_pct"] >= 60.0),
        "follow_target",
    ] = 1.0
    out.loc[
        calibrated & (out["flow_ratio_5d_pct"] <= out["q20"]) & (out["breadth_5d_pct"] <= 40.0),
        "follow_target",
    ] = 0.0
    out["contrarian_target"] = 1.0 - out["follow_target"]
    chain = 100.0
    share_index: list[float] = []
    for flow, lag_aum in zip(out["net_creation_yi"], out["matched_lag_aum_yi"]):
        if np.isfinite(flow) and np.isfinite(lag_aum) and lag_aum > 0:
            chain *= max(0.01, 1.0 + float(flow) / float(lag_aum))
            share_index.append(chain)
        else:
            share_index.append(np.nan)
    out["share_index"] = share_index
    return out


def _strategy_metrics(returns: pd.Series, position: pd.Series, cost_bps: float, label: str, key: str, scope: str) -> dict:
    valid = returns.notna() & position.notna()
    ret = returns[valid].astype(float)
    pos = position[valid].astype(float)
    if ret.empty:
        return {"key": key, "label": label, "scope": scope, "n_days": 0, "cost_bps": cost_bps}
    turnover_series = pos.diff().abs().fillna(pos.abs())
    net = pos * ret - turnover_series * (cost_bps / 10_000.0)
    curve = (1.0 + net).cumprod()
    total = float(curve.iloc[-1] - 1.0)
    years = max(len(net) / 252.0, 1.0 / 252.0)
    annual = float((1.0 + total) ** (1.0 / years) - 1.0) if total > -1 else -1.0
    volatility = float(net.std(ddof=1) * math.sqrt(252.0)) if len(net) > 1 else np.nan
    sharpe = float(net.mean() / net.std(ddof=1) * math.sqrt(252.0)) if len(net) > 1 and net.std(ddof=1) > 0 else np.nan
    drawdown = curve / curve.cummax() - 1.0
    return {
        "key": key,
        "label": label,
        "scope": scope,
        "n_days": int(len(net)),
        "start": net.index.min().strftime("%Y-%m-%d"),
        "end": net.index.max().strftime("%Y-%m-%d"),
        "total_return_pct": round(total * 100.0, 2),
        "annual_return_pct": round(annual * 100.0, 2),
        "volatility_pct": round(volatility * 100.0, 2) if np.isfinite(volatility) else None,
        "sharpe": round(sharpe, 2) if np.isfinite(sharpe) else None,
        "max_drawdown_pct": round(float(drawdown.min()) * 100.0, 2),
        "turnover": round(float(turnover_series.sum()), 1),
        "switches": int((turnover_series > 0).sum()),
        "average_exposure_pct": round(float(pos.mean()) * 100.0, 1),
        "cost_bps": round(float(cost_bps), 1),
    }


def run_strategy_set(
    aggregate: pd.DataFrame,
    benchmark: pd.DataFrame,
    *,
    execution_lag: int,
    cost_bps: float,
    scope: str,
) -> tuple[list[dict], pd.DataFrame]:
    frame = aggregate.join(benchmark.set_index("trade_date")[["close"]], how="inner")
    frame["benchmark_return"] = frame["close"].pct_change()
    frame["follow_position"] = frame["follow_target"].shift(execution_lag)
    frame["contrarian_position"] = frame["contrarian_target"].shift(execution_lag)
    available = frame["follow_position"].notna()
    frame["buy_hold_position"] = np.where(available, 1.0, np.nan)
    frame["static_50_position"] = np.where(available, 0.5, np.nan)
    matched_exposure = float(frame.loc[available, "follow_position"].mean()) if available.any() else np.nan
    frame["matched_static_position"] = np.where(available, matched_exposure, np.nan)
    definitions = [
        ("follow", "顺势：高流入加仓/高流出减仓", "follow_position", cost_bps),
        ("contrarian", "逆向：高流入减仓/高流出加仓", "contrarian_position", cost_bps),
        ("buy_hold", "沪深300买入持有", "buy_hold_position", cost_bps),
        ("static_50", "恒定50%仓位", "static_50_position", cost_bps),
        ("matched_static", "恒定同平均仓位", "matched_static_position", cost_bps),
    ]
    metrics = [
        _strategy_metrics(frame["benchmark_return"], frame[column], fee, label, key, scope)
        for key, label, column, fee in definitions
    ]
    return metrics, frame


def _forward_return(close: pd.Series, start_i: int, horizon: int, lag: int) -> float | None:
    begin = start_i + lag
    finish = begin + horizon
    if begin >= len(close) or finish >= len(close):
        return None
    base = float(close.iloc[begin])
    end = float(close.iloc[finish])
    return end / base - 1.0 if np.isfinite(base) and base > 0 and np.isfinite(end) else None


def event_study(frame: pd.DataFrame, execution_lag: int) -> tuple[list[dict], list[dict]]:
    work = frame.reset_index().rename(columns={frame.index.name or "index": "date"})
    close = pd.to_numeric(work["close"], errors="coerce")
    high = (work["follow_target"] == 1.0) & (work["follow_target"].shift(1) != 1.0)
    low = (work["follow_target"] == 0.0) & (work["follow_target"].shift(1) != 0.0)
    events: list[dict] = []
    for label, direction, mask in (("高流入", "inflow", high), ("高流出", "outflow", low)):
        for index in work.index[mask]:
            record = {
                "date": pd.Timestamp(work.loc[index, "date"]).strftime("%Y-%m-%d"),
                "label": label,
                "direction": direction,
                "flow_ratio_5d_pct": round(float(work.loc[index, "flow_ratio_5d_pct"]), 3),
            }
            for horizon in (1, 5, 10, 20):
                value = _forward_return(close, int(index), horizon, execution_lag)
                record[f"forward_{horizon}d_pct"] = round(value * 100.0, 2) if value is not None else None
            events.append(record)
    conditional: list[dict] = []
    for label in ("高流入", "高流出"):
        subset = [row for row in events if row["label"] == label]
        record: dict = {"label": label, "count": len(subset)}
        for horizon in (1, 5, 10, 20):
            values = np.array([
                row[f"forward_{horizon}d_pct"] for row in subset
                if row.get(f"forward_{horizon}d_pct") is not None
            ], dtype=float)
            record[f"forward_{horizon}d_pct"] = round(float(values.mean()), 2) if len(values) else None
            if horizon == 20:
                record["positive_rate_pct"] = round(float((values > 0).mean() * 100.0), 1) if len(values) else None
        conditional.append(record)
    return conditional, sorted(events, key=lambda row: row["date"], reverse=True)[:30]


def yearly_subperiods(frame: pd.DataFrame, execution_lag: int, cost_bps: float) -> list[dict]:
    rows: list[dict] = []
    for year, group in frame.groupby(frame.index.year):
        if len(group) < 30 or group["follow_position"].notna().sum() < 20:
            continue
        follow = _strategy_metrics(group["benchmark_return"], group["follow_position"], cost_bps, "顺势", "follow", "探索")
        hold = _strategy_metrics(group["benchmark_return"], group["buy_hold_position"], cost_bps, "买入持有", "buy_hold", "探索")
        rows.append({
            "period": str(year),
            "count": follow.get("n_days", 0),
            "annual_return_pct": follow.get("annual_return_pct"),
            "sharpe": follow.get("sharpe"),
            "max_drawdown_pct": follow.get("max_drawdown_pct"),
            "excess_return_pct": (
                round(float(follow["total_return_pct"]) - float(hold["total_return_pct"]), 2)
                if follow.get("total_return_pct") is not None and hold.get("total_return_pct") is not None else None
            ),
        })
    return rows


def _round_value(value, digits: int = 3):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if np.isfinite(number) else None


def fund_latest_rows(
    roster: list[dict], panels: dict[str, pd.DataFrame], as_of: pd.Timestamp
) -> list[dict]:
    rows: list[dict] = []
    for item in roster:
        panel = panels.get(item["code"])
        if panel is None or panel.empty or as_of not in panel.index:
            current = None
        else:
            current = panel.loc[as_of]
        holders = item.get("holders") or []
        disclosed_shares = sum(float(holder.get("shares") or 0) for holder in holders)
        rows.append({
            "code": item["code"],
            "name": item.get("name") or item["code"],
            "category": item.get("category") or "broad",
            "strategy_eligible": bool(item.get("strategy_eligible", True)),
            "holder_names": [str(holder.get("name") or "") for holder in holders if holder.get("name")],
            "holder_types": sorted({str(holder.get("type") or "") for holder in holders if holder.get("type")}),
            "disclosed_huijin_share_yi": round(disclosed_shares / 100_000_000.0, 2),
            "disclosed_huijin_pct_sum": round(sum(float(holder.get("pct") or 0) for holder in holders), 2),
            "report_period": item.get("report_period") or "",
            "disclosed_on": item.get("disclosed_on") or "",
            "evidence_grade": item.get("evidence_grade") or "",
            "evidence_url": item.get("evidence_url") or "",
            "share_yi": _round_value(current.get("share") / 10_000.0, 2) if current is not None else None,
            "aum_yi": _round_value(current.get("aum_yi"), 2) if current is not None else None,
            "share_change_1d_pct": _round_value(current.get("share_change_1d_pct"), 2) if current is not None else None,
            "share_change_5d_pct": _round_value(current.get("share_change_5d_pct"), 2) if current is not None else None,
            "net_creation_1d_yi": _round_value(current.get("net_creation_yi"), 2) if current is not None else None,
            "net_creation_5d_yi": _round_value(current.get("net_creation_5d_yi"), 2) if current is not None else None,
            "as_of": as_of.strftime("%Y-%m-%d") if current is not None and bool(current.get("observed")) else "",
        })
    return sorted(rows, key=lambda row: (not row["strategy_eligible"], -(row["aum_yi"] or -1)))


def _series_records(frame: pd.DataFrame, as_of: pd.Timestamp, limit: int = 1800) -> list[dict]:
    columns = [
        "share_yi", "aum_yi", "net_creation_yi", "net_creation_5d_yi", "share_index",
        "flow_percentile", "flow_ratio_5d_pct", "breadth_5d_pct", "coverage_pct",
    ]
    work = frame.loc[:as_of, columns].tail(limit).reset_index()
    records: list[dict] = []
    for row in work.to_dict(orient="records"):
        records.append({
            "date": pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
            **{column: _round_value(row.get(column), 4) for column in columns},
        })
    return records


def _signal_label(row: pd.Series) -> str:
    target = row.get("follow_target")
    if target == 1.0:
        return "ETF整体高流入（观察）"
    if target == 0.0:
        return "ETF整体高流出（观察）"
    if target == 0.5:
        return "ETF资金流中性（观察）"
    return "样本或覆盖不足"


def fund_series_payload(
    roster: list[dict],
    panels: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    *,
    limit: int = 1800,
) -> dict:
    """Per-ETF daily share history (total fund share, all investors) for the page's drill-down chart."""
    funds: dict[str, dict] = {}
    for item in roster:
        code = item["code"]
        panel = panels.get(code)
        if panel is None or panel.empty:
            continue
        records: list[dict] = []
        for date, row in panel.loc[:as_of].tail(limit).iterrows():
            if not bool(row.get("observed")):
                continue
            records.append({
                "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                "share_yi": _round_value(row.get("share") / 10_000.0, 4),
                "aum_yi": _round_value(row.get("aum_yi"), 4),
                "net_creation_yi": _round_value(row.get("net_creation_yi"), 4),
            })
        funds[code] = {
            "code": code,
            "name": item.get("name") or code,
            "category": item.get("category") or "broad",
            "series": records,
        }
    return {
        "schema_version": 1,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of.strftime("%Y-%m-%d"),
        "funds": funds,
    }


def build_payload(
    roster_meta: dict,
    roster: list[dict],
    panels: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame,
    fixed: pd.DataFrame,
    pit: pd.DataFrame,
    *,
    cost_bps: float,
    execution_lag: int,
    missing: list[dict],
) -> dict:
    eligible_total = sum(bool(item.get("strategy_eligible", True)) for item in roster)
    complete = fixed[
        (fixed["active_count"] == eligible_total)
        & (fixed["observed_count"] == eligible_total)
        & (fixed["flow_coverage_pct"] >= MIN_COVERAGE * 100.0)
    ]
    if complete.empty:
        raise RuntimeError("没有达到完整名单覆盖率的共同交易日")
    as_of = pd.Timestamp(complete.index.max())
    latest = fixed.loc[as_of]
    strict_metrics, strict_frame = run_strategy_set(
        pit, benchmark, execution_lag=execution_lag, cost_bps=cost_bps, scope="PIT严格"
    )
    exploratory_metrics, exploratory_frame = run_strategy_set(
        fixed, benchmark, execution_lag=execution_lag, cost_bps=cost_bps, scope="当前名单回填（探索）"
    )
    strict_follow = next((row for row in strict_metrics if row["key"] == "follow"), {})
    strict_days = int(strict_follow.get("n_days") or 0)
    strict_switches = int(strict_follow.get("switches") or 0)
    if strict_days < 500 or strict_switches < 30:
        verdict = (
            f"暂不能用作增减仓指令：按报告披露日启用名单后只有 {strict_days} 个可回测日、"
            f"{strict_switches} 次仓位变动，未达到500日/30次样本外门槛。"
        )
        preferred = "未判定（仅观察）"
    else:
        follow = next(row for row in strict_metrics if row["key"] == "follow")
        reverse = next(row for row in strict_metrics if row["key"] == "contrarian")
        preferred = "顺势" if (follow.get("sharpe") or -99) > (reverse.get("sharpe") or -99) else "逆向"
        verdict = "样本数量达到最低门槛，但仍须结合滚动样本外、费用敏感性和区块Bootstrap后再决定是否实盘。"
    conditional, events = event_study(exploratory_frame, execution_lag)
    stale_days = max(0, (pd.Timestamp(datetime.now().date()) - as_of.normalize()).days)
    status = "正常" if stale_days <= 3 and not missing else ("需关注" if stale_days <= 7 else "数据滞后")
    return {
        "schema_version": 1,
        "title": "国家队 ETF 资金代理",
        "updated": datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of.strftime("%Y-%m-%d"),
        "caveat": (
            "ETF每日总份额由所有投资者共同申购、赎回；中央汇金身份只由基金定期报告前十大持有人披露确认。"
            "本页日频曲线是汇金持仓ETF篮子的资金代理，不是汇金本人每日买卖记录。"
        ),
        "roster_scope": roster_meta.get("scope") or "",
        "data_quality": {
            "status": status,
            "coverage_pct": round(float(latest["coverage_pct"]), 1),
            "observed_count": int(latest["observed_count"]),
            "total_count": eligible_total,
            "roster_count": len(roster),
            "strategy_count": eligible_total,
            "stale_days": stale_days,
            "missing": missing,
            "anomaly_count": int(fixed.loc[:as_of, "anomaly_count"].sum()),
        },
        "latest": {
            "share_yi": _round_value(latest["share_yi"], 2),
            "aum_yi": _round_value(latest["aum_yi"], 2),
            "net_creation_1d_yi": _round_value(latest["net_creation_yi"], 2),
            "net_creation_5d_yi": _round_value(latest["net_creation_5d_yi"], 2),
            "flow_ratio_5d_pct": _round_value(latest["flow_ratio_5d_pct"], 3),
            "breadth_5d_pct": _round_value(latest["breadth_5d_pct"], 1),
            "flow_percentile": _round_value(latest["flow_percentile"], 1),
            "signal_label": _signal_label(latest),
        },
        "aggregate_series": _series_records(fixed, as_of),
        "etfs": fund_latest_rows(roster, panels, as_of),
        "backtest": {
            "verdict": verdict,
            "preferred_direction": preferred,
            "method": (
                f"主信号=5日估算净申购额/匹配基金期初规模；阈值只用此前252日20%/80%分位（至少126日）；"
                f"信号后第{execution_lag}个交易日收盘生效，单边成本{cost_bps:g}bp。"
                "PIT严格结果按定期报告披露日启用ETF；当前名单历史回填结果只作探索。"
            ),
            "strict_gate": {
                "n_days": strict_days,
                "switches": strict_switches,
                "required_days": 500,
                "required_switches": 30,
                "passed": strict_days >= 500 and strict_switches >= 30,
            },
            "strategies": strict_metrics + exploratory_metrics,
            "conditional": conditional,
            "events": events,
            "subperiods": yearly_subperiods(exploratory_frame, execution_lag, cost_bps),
        },
        "methodology": [
            "基金份额使用Tushare fund_share.fd_share（单位万份）；ETF总份额=fd_share×1万。",
            "估算规模=总份额×ETF收盘价；估算净申购额=当日份额变化×收盘价。规模涨跌中由价格造成的部分不计作申购。",
            "所有份额差分先与沪深300交易日历对齐；跨缺口、机械拆分和极端跳变不进入资金信号。",
            "聚合资金流按人民币金额相加，不再直接用不同ETF原始份额的百分比求和；每日要求至少95%匹配覆盖。",
            "正式结论只看报告披露日之后的PIT名单；用今天名单回填历史的长回测明确标为探索结果。",
            "份额数据没有精确available_at，回测保守使用T+2收盘，且按仓位变化扣除单边交易成本。",
        ],
        "limitations": [
            "ETF总份额变化包含所有申赎，不能识别中央汇金本人当天买卖；汇金二级市场买卖还可能完全不改变ETF总份额。",
            "定期报告持有人信息只有半年报/年报频率，披露日至下一次报告之间不能假设汇金持仓不变。",
            "金额按收盘价估算，不是基金公司最终确认的申购赎回清算金额或基金净值。",
            "严格PIT样本目前很短；未达到500个有效交易日和30次样本外仓位切换前，不发布加减仓建议。",
            "当前官方证据名单是已核验种子而非永不变化的全集，后续报告新增/退出需要继续维护生效区间。",
            "历史回测不保证未来表现，本页只用于研究和风控。",
        ],
    }


def main() -> None:
    args = parse_args()
    roster_meta, roster = load_roster(args.roster)
    pro = _api()
    benchmark = load_index(pro, args.index, args.start, args.end, args.refresh, args.sleep)
    calendar = pd.DatetimeIndex(benchmark["trade_date"], name="date")
    panels: dict[str, pd.DataFrame] = {}
    missing: list[dict] = []
    for index, item in enumerate(roster, start=1):
        code = item["code"]
        cache_code = code.replace(".", "_")
        try:
            share = cached_history(
                pro, "fund_share", f"share_{cache_code}", code, args.start, args.end,
                refresh=args.refresh, sleep_seconds=args.sleep,
            )
            daily = cached_history(
                pro, "fund_daily", f"fund_daily_{cache_code}", code, args.start, args.end,
                refresh=args.refresh, sleep_seconds=args.sleep,
            )
            panel = build_fund_panel(share, daily, calendar, item)
            if panel.empty or pd.isna(panel.attrs.get("first_date")):
                raise RuntimeError("份额或行情为空")
            panels[code] = panel
            print(f"[{index}/{len(roster)}] {code} {item.get('name', '')} loaded", flush=True)
        except Exception as exc:
            missing.append({"code": code, "message": str(exc)[:240]})
            print(f"[{index}/{len(roster)}] {code} skipped: {exc}", flush=True)
    available_roster = [item for item in roster if item["code"] in panels]
    if sum(bool(item.get("strategy_eligible", True)) for item in available_roster) < 3:
        raise RuntimeError(f"可用汇金宽基ETF不足3只: {missing}")
    fixed = aggregate_panels(panels, available_roster, calendar, point_in_time=False)
    pit = aggregate_panels(panels, available_roster, calendar, point_in_time=True)
    payload = build_payload(
        roster_meta,
        available_roster,
        panels,
        benchmark,
        fixed,
        pit,
        cost_bps=args.cost_bps,
        execution_lag=max(1, args.execution_lag),
        missing=missing,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    series_payload = fund_series_payload(available_roster, panels, pd.Timestamp(payload["as_of"]))
    series_output = Path(args.series_output)
    series_output.parent.mkdir(parents=True, exist_ok=True)
    series_output.write_text(json.dumps(series_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "as_of": payload["as_of"],
        "funds": len(payload["etfs"]),
        "latest": payload["latest"],
        "verdict": payload["backtest"]["verdict"],
    }, ensure_ascii=False, indent=2))
    print(f"written: {output}")
    print(f"written: {series_output}")


if __name__ == "__main__":
    main()
