"""Collect auditable US/Korean storage-market snapshots on the PC."""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import tushare as ts

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cross_market import evaluate_live_gate, match_holdings, validate_result

SHARED = Path(
    os.environ.get("PREDICT_DIR", r"Z:\claude\qlib\data\csv_tmp")
)
US_SYMBOLS = ("MU", "SNDK", "SMH", "SOXX")
KOREA_SYMBOLS = ("000660.KS", "005930.KS")
DECISION_TIME = "09:20"
KOREA_VISIBLE_CUTOFF = "09:05"


def yahoo_chart(symbol, interval="5m", range_="5d", session=requests):
    response = session.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval": interval, "range": range_},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [])
    if not result:
        raise ValueError(f"Yahoo returned no chart for {symbol}")
    return result[0]


def eastmoney_us_quote(symbol, session=requests):
    response = session.get(
        "https://push2.eastmoney.com/api/qt/stock/get",
        params={
            "secid": f"105.{symbol}",
            "fields": "f57,f58,f43,f60,f86",
        },
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json().get("data") or {}
    if not data.get("f43") or not data.get("f60"):
        raise ValueError(f"Eastmoney returned no quote for {symbol}")
    return {
        "symbol": symbol,
        "name": data.get("f58") or symbol,
        "price": float(data["f43"]) / 1000,
        "previous_close": float(data["f60"]) / 1000,
        "timestamp": int(data["f86"]),
        "source": "eastmoney",
    }


def collect_us_quote(
    symbol, primary_fetch=eastmoney_us_quote, backup_fetch=yahoo_chart
):
    """Use Eastmoney when available and degrade per symbol to Yahoo."""
    backup = backup_fetch(symbol, interval="1d", range_="5d")
    meta = backup.get("meta") or {}
    closes = (
        (((backup.get("indicators") or {}).get("quote") or [{}])[0])
        .get("close")
        or []
    )
    closes = [float(value) for value in closes if value is not None]
    backup_price = closes[-1] if closes else float(
        meta.get("regularMarketPrice") or 0
    )
    backup_previous = closes[-2] if len(closes) >= 2 else float(
        meta.get("chartPreviousClose") or meta.get("previousClose") or 0
    )
    try:
        quote = primary_fetch(symbol)
        if backup_price:
            quote["backup_price"] = backup_price
            quote["source_difference_pct"] = round(
                abs(quote["price"] - backup_price)
                / quote["price"]
                * 100,
                4,
            )
    except Exception as exc:
        if not backup_price or not backup_previous:
            raise
        quote = {
            "symbol": symbol,
            "name": meta.get("shortName") or symbol,
            "price": backup_price,
            "previous_close": backup_previous,
            "timestamp": meta.get("regularMarketTime"),
            "source": "yahoo",
            "primary_error": str(exc),
        }
    quote["return_pct"] = round(
        (quote["price"] / quote["previous_close"] - 1) * 100, 3
    )
    return quote


def append_snapshot(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent, suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def tushare_api():
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    token_path = ROOT / "data" / ".tushare_token"
    if not token and token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
    return ts.pro_api(token) if token else None


def collect_cn_daily_charts(candidates, pro=None, limit=12):
    if pro is None:
        return {}
    charts = {}
    for row in candidates[:limit]:
        code = row.get("code")
        if not code:
            continue
        try:
            df = pro.daily(ts_code=code)
            if df is None or df.empty:
                continue
            df = df.sort_values("trade_date").tail(120)
            charts[code] = {
                "daily": {
                    "points": [
                        {
                            "date": f"{str(r.trade_date)[:4]}-{str(r.trade_date)[4:6]}-{str(r.trade_date)[6:]}",
                            "open": float(r.open),
                            "close": float(r.close),
                            "low": float(r.low),
                            "high": float(r.high),
                        }
                        for r in df.itertuples(index=False)
                    ]
                }
            }
        except Exception as exc:
            charts[code] = {"error": str(exc)}
    return charts


def eastmoney_secid(ts_code):
    code, exchange = ts_code.split(".")
    market = "1" if exchange == "SH" else "0"
    return f"{market}.{code}"


def collect_cn_intraday_charts(candidates, session=requests, limit=12):
    charts = {}
    for row in candidates[:limit]:
        code = row.get("code")
        if not code:
            continue
        try:
            charts[code] = {"intraday": {"points": _cn_intraday_points(code, session)}}
        except Exception as exc:
            charts[code] = {"error": str(exc)}
    return charts


def _cn_symbol(ts_code):
    code, exchange = ts_code.split(".")
    return ("sh" if exchange == "SH" else ("bj" if exchange == "BJ" else "sz")) + code


def _pct_points(points, pre_close):
    if not pre_close:
        return points
    for point in points:
        if point.get("price") is not None:
            point["pct"] = round((float(point["price"]) / float(pre_close) - 1) * 100, 3)
    return points


def _cn_intraday_points(ts_code, session=requests):
    errors = []
    try:
        response = session.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": eastmoney_secid(ts_code),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "klt": "5",
                "fqt": "1",
                "end": "20500101",
                "lmt": "80",
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
            },
            timeout=20,
        )
        response.raise_for_status()
        klines = ((response.json().get("data") or {}).get("klines") or [])
        points = []
        prev_close = None
        for item in klines:
            parts = item.split(",")
            if len(parts) < 3:
                continue
            date_part = parts[0][:10]
            t = parts[0][-5:]
            price = float(parts[2])
            points.append({"date": date_part, "t": t, "price": price, "source": "eastmoney"})
        if points:
            latest_date = max(point["date"] for point in points)
            latest = [point for point in points if point["date"] == latest_date]
            prev = [point for point in points if point["date"] < latest_date]
            prev_close = prev[-1]["price"] if prev else None
            return _pct_points(latest, prev_close)
    except Exception as exc:
        errors.append(f"eastmoney: {exc}")

    try:
        import re

        symbol = _cn_symbol(ts_code)
        response = session.get(
            "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData",
            params={"symbol": symbol, "scale": 5, "ma": "no", "datalen": 320},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        match = re.search(r"=\((\[.*\])\)", response.text, re.S)
        rows = json.loads(match.group(1)) if match else []
        if not rows:
            raise ValueError("empty sina intraday")
        by_day = {}
        for item in rows:
            day = item["day"][:10]
            by_day.setdefault(day, []).append(item)
        days = sorted(by_day)
        latest_day = days[-1]
        prev_days = [day for day in days if day < latest_day]
        prev_close = float(by_day[prev_days[-1]][-1]["close"]) if prev_days else None
        points = [
            {
                "date": latest_day,
                "t": item["day"][11:16],
                "price": float(item["close"]),
                "source": "sina",
            }
            for item in by_day[latest_day]
        ]
        return _pct_points(points, prev_close)
    except Exception as exc:
        errors.append(f"sina: {exc}")
        raise RuntimeError("; ".join(errors))


def merge_chart_maps(*maps):
    merged = {}
    for chart_map in maps:
        for code, payload in (chart_map or {}).items():
            merged.setdefault(code, {}).update(payload)
    return merged


def yahoo_points(result, symbol):
    timestamps = result.get("timestamp") or []
    quotes = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    meta = result.get("meta") or {}
    closes = quotes.get("close") or []
    timezone_name = (
        (result.get("meta") or {}).get("exchangeTimezoneName") or "UTC"
    )
    exchange_timezone = ZoneInfo(timezone_name)
    rows = []
    valid_closes = [float(value) for value in closes if value is not None]
    pre_close = float(
        meta.get("chartPreviousClose") or meta.get("previousClose") or 0
    )
    if not pre_close and len(valid_closes) >= 2:
        pre_close = valid_closes[-2]
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        market_at = datetime.fromtimestamp(
            timestamp, timezone.utc
        ).astimezone(exchange_timezone)
        row = {
            "symbol": symbol,
            "t": market_at.strftime("%H:%M"),
            "market_at": market_at.isoformat(),
            "market_time": market_at.strftime("%H:%M"),
            "price": round(float(close), 4),
            "source": "yahoo",
            "exchange_timezone": timezone_name,
        }
        if pre_close:
            row["pct"] = round((float(close) / pre_close - 1) * 100, 3)
        rows.append(row)
    return rows


def yahoo_ohlc_points(result, symbol):
    timestamps = result.get("timestamp") or []
    quotes = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    timezone_name = (
        (result.get("meta") or {}).get("exchangeTimezoneName") or "UTC"
    )
    exchange_timezone = ZoneInfo(timezone_name)
    rows = []
    for timestamp, open_, high, low, close in zip(
        timestamps,
        quotes.get("open") or [],
        quotes.get("high") or [],
        quotes.get("low") or [],
        quotes.get("close") or [],
    ):
        if None in (open_, high, low, close):
            continue
        market_at = datetime.fromtimestamp(
            timestamp, timezone.utc
        ).astimezone(exchange_timezone)
        rows.append(
            {
                "symbol": symbol,
                "date": market_at.date().isoformat(),
                "open": round(float(open_), 4),
                "close": round(float(close), 4),
                "low": round(float(low), 4),
                "high": round(float(high), 4),
                "source": "yahoo",
                "exchange_timezone": timezone_name,
            }
        )
    return rows


def collect_yahoo_chart_bundle(symbols):
    intraday = []
    daily = []
    for symbol in symbols:
        try:
            rows = yahoo_points(yahoo_chart(symbol), symbol)
            latest_date = max(
                [row["market_at"][:10] for row in rows if row.get("market_at")]
                or [None]
            )
            intraday.extend(
                [
                    row
                    for row in rows
                    if not latest_date
                    or row.get("market_at", "").startswith(latest_date)
                ]
            )
        except Exception:
            pass
        try:
            daily.extend(
                yahoo_ohlc_points(
                    yahoo_chart(symbol, interval="1d", range_="6mo"),
                    symbol,
                )
            )
        except Exception:
            pass
    return {
        "intraday": {"points": intraday},
        "daily": {"points": daily},
    }


def collect_korea_snapshots(output_dir, fetched_at=None):
    fetched_at = fetched_at or datetime.now().astimezone().isoformat()
    collected = []
    for symbol in KOREA_SYMBOLS:
        result = yahoo_chart(symbol)
        points = yahoo_points(result, symbol)
        for point in points:
            row = {**point, "fetched_at": fetched_at}
            append_snapshot(
                Path(output_dir)
                / "cross_market"
                / "korea_storage_intraday.jsonl",
                row,
            )
            collected.append(row)
    return collected


def build_research_result(
    us_quotes, korea_points, candidates, generated_at, holdings=None,
    cn_charts=None,
):
    """Build a safe initial payload while forward evidence accumulates."""
    visible_korea = [
        point
        for point in korea_points
        if point.get("market_time", "") <= KOREA_VISIBLE_CUTOFF
    ]
    dated = [point for point in visible_korea if point.get("market_at")]
    if dated:
        latest_date = max(point["market_at"][:10] for point in dated)
        visible_korea = [
            point for point in dated
            if point["market_at"].startswith(latest_date)
        ]
    us_strength = max(
        [abs(float(row.get("return_pct") or 0)) for row in us_quotes] or [0]
    )
    korea_strength = 0.0
    if len(visible_korea) >= 2 and visible_korea[0].get("price"):
        korea_strength = abs(
            float(visible_korea[-1]["price"])
            / float(visible_korea[0]["price"])
            - 1
        ) * 100
    ranked = []
    for row in candidates:
        score = min(
            100.0,
            float(row.get("business_purity") or 0) * 0.45
            + min(us_strength * 10, 100) * 0.35
            + min(korea_strength * 20, 100) * 0.20,
        )
        ranked.append({**row, "score": round(score, 2)})
    ranked.sort(key=lambda row: row["score"], reverse=True)
    generated = datetime.fromisoformat(generated_at)
    korea_today = generated.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
    market_date = (
        visible_korea[-1]["market_at"][:10]
        if visible_korea and visible_korea[-1].get("market_at")
        else None
    )
    data_ok = bool(
        us_quotes
        and visible_korea
        and market_date == korea_today
        and generated.strftime("%H:%M") >= DECISION_TIME
    )
    gate = evaluate_live_gate(
        {
            "sample_years": 0,
            "win_rate": 0,
            "sharpe": 0,
            "mean_excess": 0,
            "recent_12m_valid": False,
            "forward_days": 0,
            "forward_valid": False,
            "data_ok": data_ok,
        }
    )
    return validate_result(
        {
            "schema_version": 1,
            "generated_at": generated_at,
            "decision_at": generated_at,
            "sector": "storage",
            "mode": "research",
            "data_health": {
                "status": "ok" if data_ok else "missing",
                "market_at": (
                    visible_korea[-1].get("market_at")
                    if visible_korea
                    else None
                ),
                "sources": ["eastmoney", "yahoo"],
            },
            "leaders": us_quotes,
            "upside": ranked,
            "downside": ranked if any(
                float(row.get("return_pct") or 0) < 0 for row in us_quotes
            ) else [],
            "holdings": holdings or [],
            "gate": gate,
            "charts": {
                "us": {
                    "intraday": {
                        "points": [
                            {
                                "t": row.get("symbol"),
                                "price": row.get("price"),
                            }
                            for row in us_quotes
                        ]
                    }
                },
                "korea": {
                    "intraday": {"points": visible_korea}
                },
                "cn": cn_charts or {},
            },
        }
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=SHARED)
    args = parser.parse_args()
    status_path = args.output_dir / "cross_market_storage_status.json"
    try:
        generated_at = datetime.now().astimezone().isoformat()
        rows = collect_korea_snapshots(args.output_dir, generated_at)
        us_quotes = []
        for symbol in US_SYMBOLS:
            us_quotes.append(collect_us_quote(symbol))
        config_path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "cross_market_sectors.json"
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        positions_path = Path(
            os.environ.get(
                "POSITIONS_JSON",
                r"\/app/data\positions.json",
            )
        )
        positions = []
        if positions_path.exists():
            stored = json.loads(positions_path.read_text(encoding="utf-8"))
            positions = stored if isinstance(stored, list) else stored.get("positions", [])
        candidates = config["storage"]["a_share_candidates"]
        result = build_research_result(
            us_quotes,
            rows,
            candidates,
            generated_at,
            cn_charts=merge_chart_maps(
                collect_cn_daily_charts(candidates, pro=tushare_api()),
                collect_cn_intraday_charts(candidates),
            ),
        )
        result["charts"]["us"] = collect_yahoo_chart_bundle(US_SYMBOLS)
        result["charts"]["korea"] = collect_yahoo_chart_bundle(KOREA_SYMBOLS)
        direction = "down" if result["downside"] else "up"
        result["holdings"] = match_holdings(
            result["downside"] or result["upside"], positions, direction
        )
        atomic_json(args.output_dir / "cross_market_storage.json", result)
        atomic_json(
            status_path,
            {
                "status": "done",
                "updated": generated_at,
                "us_quotes": len(us_quotes),
                "korea_points": len(rows),
            },
        )
    except Exception as exc:
        atomic_json(
            status_path,
            {
                "status": "error",
                "updated": datetime.now().astimezone().isoformat(),
                "message": str(exc),
            },
        )
        raise


if __name__ == "__main__":
    main()
