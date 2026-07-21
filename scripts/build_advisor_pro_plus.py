from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def c6(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:6]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def action_key(value: str) -> str:
    text = str(value or "")
    if "买" in text or "涔" in text:
        return "buy"
    if "卖" in text or "鍗" in text:
        return "sell"
    if "持" in text or "鎸" in text:
        return "hold"
    return "other"


def advisor_items(advisor: dict) -> list[dict]:
    trade = advisor.get("trade") or {}
    rows = trade.get("items")
    if isinstance(rows, list) and rows:
        return [dict(x) for x in rows if isinstance(x, dict)]
    cur = advisor.get("current") or {}
    basket = cur.get("basket")
    if isinstance(basket, list):
        out = []
        for row in basket:
            if isinstance(row, dict):
                item = dict(row)
                item.setdefault("action", "买入")
                out.append(item)
        return out
    return []


def rolling_items(rolling: dict) -> list[dict]:
    block = rolling.get("rolling") or {}
    rows = block.get("items")
    if isinstance(rows, list):
        return [dict(x) for x in rows if isinstance(x, dict)]
    rows = rolling.get("items")
    return [dict(x) for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []


def score_rolling(row: dict) -> float:
    try:
        return float(row.get("dedt_yoy") or 0) + float(row.get("delta") or 0) * 0.5
    except Exception:
        return 0.0


def performance_compare(advisor: dict, backtest: dict | None) -> dict:
    track = advisor.get("track") or {}
    advisor_summary = track.get("summary") or {}
    rolling_summary = (((backtest or {}).get("timed") or {}).get("summary") or {}).get("10") or {}

    advisor_ann = advisor_summary.get("net_ann")
    advisor_sharpe = advisor_summary.get("sharpe")
    advisor_win = advisor_summary.get("winrate")
    advisor_ann_pct = round(float(advisor_ann) * 100, 2) if advisor_ann is not None else None
    advisor_win_pct = round(float(advisor_win) * 100, 1) if advisor_win is not None else None

    rolling_ann_pct = rolling_summary.get("ann_pct")
    rolling_sharpe = rolling_summary.get("sharpe")
    rolling_win_pct = rolling_summary.get("win_rate_pct")

    diff_ann = (
        round(float(rolling_ann_pct) - float(advisor_ann_pct), 2)
        if rolling_ann_pct is not None and advisor_ann_pct is not None
        else None
    )
    diff_sharpe = (
        round(float(rolling_sharpe) - float(advisor_sharpe), 3)
        if rolling_sharpe is not None and advisor_sharpe is not None
        else None
    )
    diff_win = (
        round(float(rolling_win_pct) - float(advisor_win_pct), 1)
        if rolling_win_pct is not None and advisor_win_pct is not None
        else None
    )
    return {
        "advisor_pro": {
            "label": "顾问Pro对冲净超额",
            "ann_pct": advisor_ann_pct,
            "sharpe": advisor_sharpe,
            "win_rate_pct": advisor_win_pct,
            "final_nav": advisor_summary.get("final_nav"),
            "years": advisor_summary.get("years"),
            "n": advisor_summary.get("n"),
        },
        "rolling_earnings_10d": {
            "label": "滚动业绩10日事件回测",
            "ann_pct": rolling_ann_pct,
            "sharpe": rolling_sharpe,
            "win_rate_pct": rolling_win_pct,
            "mean_pct": rolling_summary.get("mean_pct"),
            "median_pct": rolling_summary.get("median_pct"),
            "n": rolling_summary.get("n"),
        },
        "diff": {
            "ann_pct": diff_ann,
            "sharpe": diff_sharpe,
            "win_rate_pct": diff_win,
        },
        "note": "两者口径不同：顾问Pro是季度调仓组合净超额；滚动业绩是公告后10日事件收益，适合作为增强/候补信号，不等同完整组合净值。",
    }


def rolling_10d_curve(backtest: dict | None) -> dict:
    portfolio = (((backtest or {}).get("timed") or {}).get("rolling_portfolio_curve") or {})
    if portfolio.get("dates") and portfolio.get("nav"):
        return {
            "label": "滚动业绩组合净值",
            "dates": portfolio.get("dates") or [],
            "nav": portfolio.get("nav") or [],
            "daily_return_pct": portfolio.get("daily_return_pct") or [],
            "holding_count": portfolio.get("holding_count") or [],
            "top_codes": portfolio.get("top_codes") or [],
            "source": "timed.rolling_portfolio_curve",
            "note": "新业绩信号进入后每日更新候选池，TopN等权滚动调仓。",
        }
    curve = ((((backtest or {}).get("timed") or {}).get("curves") or {}).get("10") or
             (((backtest or {}).get("curves") or {}).get("10") or {}))
    dates = curve.get("dates") or []
    nav = curve.get("nav") or []
    if dates and nav:
        return {
            "label": "滚动业绩10日事件回测",
            "dates": dates,
            "nav": nav,
            "daily_return_pct": curve.get("daily_return_pct") or [],
            "n_events": curve.get("n_events") or [],
            "source": "timed.curves.10",
            "note": "完整逐笔事件按入场日等权聚合后复利。",
        }
    by_year = (((backtest or {}).get("timed") or {}).get("by_year") or (backtest or {}).get("by_year") or {})
    dates: list[str] = []
    nav: list[float] = []
    value = 1.0
    for year in sorted(str(y) for y in by_year.keys()):
        row = (by_year.get(year) or {}).get("10") or {}
        ann_pct = row.get("ann_pct")
        if ann_pct is None:
            continue
        try:
            value *= max(0.0, 1.0 + float(ann_pct) / 100.0)
        except Exception:
            continue
        dates.append(f"{year}-12-31")
        nav.append(round(value, 4))
    return {
        "label": "滚动业绩10日事件回测",
        "dates": dates,
        "nav": nav,
        "source": "timed.by_year.10.ann_pct",
        "note": "按年度10日事件回测折算收益复利，非逐笔组合净值。",
    }


def merge_row(advisor_row: dict | None, rolling_row: dict | None, bucket: str) -> dict:
    row = dict(advisor_row or {})
    if rolling_row:
        row.update({k: v for k, v in rolling_row.items() if v not in ("", None)})
    code = c6(row.get("code") or row.get("ts_code"))
    row["code"] = code
    row["bucket"] = bucket
    row["rolling_score"] = round(score_rolling(rolling_row or {}), 3)
    row["advisor_action"] = (advisor_row or {}).get("action") or ""
    row["has_rolling_earnings"] = bool(rolling_row)
    return row


def build_plus(advisor: dict, rolling: dict, backtest: dict | None = None) -> dict:
    adv_rows = advisor_items(advisor)
    roll_rows = rolling_items(rolling)
    adv_by_code = {c6(x.get("code") or x.get("ts_code")): x for x in adv_rows if c6(x.get("code") or x.get("ts_code"))}
    roll_by_code = {c6(x.get("code") or x.get("ts_code")): x for x in roll_rows if c6(x.get("code") or x.get("ts_code"))}

    enhanced_buy: list[dict] = []
    enhanced_hold: list[dict] = []
    conflicts: list[dict] = []
    base_buy: list[dict] = []
    base_hold: list[dict] = []
    base_sell: list[dict] = []

    for code, adv in adv_by_code.items():
        act = action_key(adv.get("action"))
        roll = roll_by_code.get(code)
        if roll and act == "buy":
            enhanced_buy.append(merge_row(adv, roll, "pro_buy_with_earnings"))
        elif roll and act == "hold":
            enhanced_hold.append(merge_row(adv, roll, "pro_hold_with_earnings"))
        elif roll and act == "sell":
            conflicts.append(merge_row(adv, roll, "pro_sell_but_earnings"))
        elif act == "buy":
            base_buy.append(merge_row(adv, None, "pro_buy_only"))
        elif act == "hold":
            base_hold.append(merge_row(adv, None, "pro_hold_only"))
        elif act == "sell":
            base_sell.append(merge_row(adv, None, "pro_sell_only"))

    event_candidates = [
        merge_row(None, roll, "earnings_only_candidate")
        for code, roll in roll_by_code.items()
        if code not in adv_by_code
    ]

    for group in (enhanced_buy, enhanced_hold, conflicts, event_candidates):
        group.sort(key=lambda x: (-float(x.get("rolling_score") or 0), x.get("code") or ""))

    cur = advisor.get("current") or {}
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "as_of": cur.get("as_of") or advisor.get("updated_at") or "",
        "regime": cur.get("regime_label") or cur.get("regime") or "",
        "method": "顾问Pro主篮子 + 滚动业绩公告增强；Pro卖出与业绩命中冲突时只提示，不自动买入。",
        "summary": {
            "n_enhanced_buy": len(enhanced_buy),
            "n_enhanced_hold": len(enhanced_hold),
            "n_event_candidates": len(event_candidates),
            "n_conflicts": len(conflicts),
            "n_base_buy": len(base_buy),
            "n_base_hold": len(base_hold),
            "n_base_sell": len(base_sell),
        },
        "enhanced_buy": enhanced_buy,
        "enhanced_hold": enhanced_hold,
        "event_candidates": event_candidates,
        "conflicts": conflicts,
        "base_buy": base_buy,
        "base_hold": base_hold,
        "base_sell": base_sell,
        "advisor_track": advisor.get("track") or {},
        "advisor_longonly": advisor.get("longonly") or {},
        "performance_compare": performance_compare(advisor, backtest),
        "rolling_10d_curve": rolling_10d_curve(backtest),
        "backtest": backtest or {},
    }


def build_report(data_dir: Path, shared_dir: Path | None = None) -> dict:
    shared_dir = shared_dir or data_dir
    advisor = read_json(shared_dir / "regime_advisor_pro.json") or read_json(data_dir / "regime_advisor_pro.json") or {}
    rolling = read_json(shared_dir / "rolling_earnings.json") or read_json(data_dir / "rolling_earnings.json") or {}
    backtest = read_json(shared_dir / "rolling_earnings_backtest_top50.json") or read_json(data_dir / "rolling_earnings_backtest_top50.json") or {}
    return build_plus(advisor, rolling, backtest=backtest)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build Advisor Pro+ rolling earnings overlay.")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "data"))
    parser.add_argument("--shared-dir", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    payload = build_report(Path(args.data_dir), Path(args.shared_dir) if args.shared_dir else None)
    out = Path(args.out) if args.out else (Path(args.shared_dir or args.data_dir) / "advisor_pro_plus.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"advisor pro plus enhanced_buy={payload['summary']['n_enhanced_buy']} candidates={payload['summary']['n_event_candidates']}")


if __name__ == "__main__":
    main()
