"""Backtest sector ETF share flow as a sector top/bottom signal."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from backtest_etf_flow_signal import (
    DATA,
    add_flow_features,
    api,
    cached_call,
    forward_outcomes,
    make_signal_events,
    summarize,
    ymd,
)

OUT = DATA / "sector_etf_flow_signal.json"

SECTOR_ETFS = {
    "512480.SH": "半导体ETF",
    "159995.SZ": "芯片ETF",
    "512760.SH": "半导体50ETF",
    "515260.SH": "电子ETF",
    "159997.SZ": "电子ETF",
    "512720.SH": "计算机ETF",
    "159998.SZ": "计算机ETF",
    "515880.SH": "通信ETF",
    "515050.SH": "5GETF",
    "159819.SZ": "人工智能ETF",
    "512930.SH": "AI ETF",
    "159770.SZ": "机器人ETF",
    "512880.SH": "证券ETF",
    "512000.SH": "券商ETF",
    "512800.SH": "银行ETF",
    "159841.SZ": "保险ETF",
    "512010.SH": "医药ETF",
    "512170.SH": "医疗ETF",
    "512290.SH": "生物医药ETF",
    "159883.SZ": "医疗器械ETF",
    "516020.SH": "化工ETF",
    "159870.SZ": "化工ETF",
    "516220.SH": "化工龙头ETF",
    "512400.SH": "有色金属ETF",
    "159881.SZ": "有色60ETF",
    "516780.SH": "稀土ETF",
    "515210.SH": "钢铁ETF",
    "159745.SZ": "建材ETF",
    "516750.SH": "建材ETF",
    "515790.SH": "光伏ETF",
    "516160.SH": "新能源ETF",
    "515030.SH": "新能源车ETF",
    "512660.SH": "军工ETF",
    "512670.SH": "国防ETF",
    "512690.SH": "酒ETF",
    "515170.SH": "食品饮料ETF",
    "159928.SZ": "消费ETF",
    "512600.SH": "主要消费ETF",
    "159996.SZ": "家电ETF",
    "159825.SZ": "农业ETF",
    "159865.SZ": "养殖ETF",
    "512980.SH": "传媒ETF",
    "159805.SZ": "传媒ETF",
    "159869.SZ": "游戏ETF",
    "515220.SH": "煤炭ETF",
    "159930.SZ": "能源ETF",
    "159611.SZ": "电力ETF",
    "512200.SH": "房地产ETF",
    "516970.SH": "基建ETF",
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--lookback", type=int, default=60)
    p.add_argument("--threshold", type=float, default=0.9)
    p.add_argument("--direction", choices=("increase", "decrease"), default="increase")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--output", default=str(OUT))
    return p.parse_args()


def load_one_etf(pro, code: str, start: str, end: str, refresh: bool) -> pd.DataFrame:
    share = cached_call(
        pro,
        "fund_share",
        f"sector_share_{code.replace('.', '_')}_{ymd(start)}_{ymd(end)}",
        refresh,
        ts_code=code,
        start_date=ymd(start),
        end_date=ymd(end),
    )
    daily = cached_call(
        pro,
        "fund_daily",
        f"sector_daily_{code.replace('.', '_')}_{ymd(start)}_{ymd(end)}",
        refresh,
        ts_code=code,
        start_date=ymd(start),
        end_date=ymd(end),
    )
    if share.empty or daily.empty:
        return pd.DataFrame()
    share_col = next((c for c in ("fd_share", "fund_share", "share") if c in share.columns), None)
    if not share_col or "close" not in daily.columns:
        return pd.DataFrame()
    panel = share[["trade_date", share_col]].rename(columns={share_col: "share"})
    panel = panel.merge(daily[["trade_date", "close"]], on="trade_date", how="inner")
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], format="%Y%m%d", errors="coerce")
    panel["share"] = pd.to_numeric(panel["share"], errors="coerce")
    panel["close"] = pd.to_numeric(panel["close"], errors="coerce")
    return panel.dropna(subset=["trade_date", "share", "close"]).sort_values("trade_date")


def backtest_one_etf(
    code: str,
    name: str,
    panel: pd.DataFrame,
    direction: str,
    threshold: float,
    lookback: int,
    horizons: tuple[int, ...] = (5, 10, 20, 60),
) -> pd.DataFrame:
    featured = add_flow_features(panel, lookback=lookback)
    events = make_signal_events(featured, threshold=threshold, direction=direction)
    outcomes = forward_outcomes(events, panel[["trade_date", "close"]], horizons=horizons)
    if outcomes.empty:
        return outcomes
    outcomes.insert(0, "name", name)
    outcomes.insert(0, "ts_code", code)
    return outcomes


def summarize_by_etf(rows: pd.DataFrame) -> list[dict]:
    out = []
    if rows.empty:
        return out
    for (code, name), group in rows.groupby(["ts_code", "name"]):
        item = {"ts_code": code, "name": name}
        item.update(summarize(group))
        out.append(item)
    return sorted(out, key=lambda x: x.get("n_signals", 0), reverse=True)


def main():
    cfg = parse_args()
    pro = api()
    frames = []
    missing = []
    for code, name in SECTOR_ETFS.items():
        panel = load_one_etf(pro, code, cfg.start, cfg.end, cfg.refresh)
        if panel.empty:
            missing.append({"ts_code": code, "name": name})
            continue
        out = backtest_one_etf(
            code,
            name,
            panel,
            direction=cfg.direction,
            threshold=cfg.threshold,
            lookback=cfg.lookback,
        )
        if not out.empty:
            frames.append(out)
    rows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    result = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "period": [cfg.start, cfg.end],
        "signal": {
            "direction": cfg.direction,
            "share_change_window": "5 trading days inclusive",
            "rolling_lookback": cfg.lookback,
            "percentile_threshold": cfg.threshold,
        },
        "summary_all": summarize(rows),
        "summary_by_etf": summarize_by_etf(rows),
        "missing": missing,
        "events": json.loads(rows.to_json(orient="records", date_format="iso", force_ascii=False)),
    }
    path = Path(cfg.output)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary_all": result["summary_all"], "missing": missing}, ensure_ascii=False, indent=2))
    print(f"written: {path}")


if __name__ == "__main__":
    main()
