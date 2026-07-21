from __future__ import annotations

import argparse
from bisect import bisect_right
import errno
import hashlib
import json
import math
import os
import sqlite3
import socket
import time as time_module
import uuid
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("PREDICT_DATA_DIR") or (ROOT / "data"))
OUT = DATA_DIR / "rolling_earnings_backtest_top50.json"
ANNOUNCEMENT_CACHE = DATA_DIR / "cninfo_earnings_announcements.json"
DEFAULT_LOCK = DATA_DIR / "rolling_earnings_backtest.lock"
DEFAULT_STATUS = DATA_DIR / "rolling_earnings_backtest_status.json"
LOCK_BUSY_EXIT = 75
SOURCE_CHANGED_EXIT = 76
DEFAULT_BUY_COST_RATE = float(os.environ.get("ROLLING_BUY_COST_RATE", "0.0005"))
DEFAULT_SELL_COST_RATE = float(os.environ.get("ROLLING_SELL_COST_RATE", "0.0015"))
DEFAULT_IMPACT_COST_RATE = float(os.environ.get("ROLLING_IMPACT_COST_RATE", "0.0010"))


def c6(ts_code: str) -> str:
    return "".join(ch for ch in str(ts_code or "") if ch.isdigit())[:6]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def file_fingerprint(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    payload = read_json(path)
    updated = payload.get("updated") if isinstance(payload, dict) else None
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
        "updated": updated,
    }


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


class BacktestLockBusy(RuntimeError):
    def __init__(self, owner: dict | None = None):
        self.owner = owner or {}
        super().__init__(f"rolling earnings backtest already running (pid={self.owner.get('pid')})")


def acquire_backtest_lock(path: Path, wait_seconds: int, reason: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time_module.monotonic() + max(0, wait_seconds)
    owner = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "token": uuid.uuid4().hex,
        "reason": reason,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(owner, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            return owner
        except FileExistsError:
            existing = read_json(path)
            stale = False
            try:
                lock_age = time_module.time() - path.stat().st_mtime
            except OSError:
                lock_age = 0
            if isinstance(existing, dict) and existing.get("pid"):
                same_host = str(existing.get("host") or "").lower() == socket.gethostname().lower()
                stale = (same_host and not _pid_is_running(int(existing.get("pid") or 0))) or lock_age > (12 * 3600)
            else:
                stale = lock_age > 60
            if stale:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time_module.monotonic() >= deadline:
                raise BacktestLockBusy(existing if isinstance(existing, dict) else None)
            time_module.sleep(1)


def release_backtest_lock(path: Path, owner: dict) -> None:
    existing = read_json(path)
    if isinstance(existing, dict) and existing.get("token") == owner.get("token"):
        path.unlink(missing_ok=True)


def _previous_quarter_end(end_date: str) -> str | None:
    text = str(end_date or "")[:8]
    if len(text) != 8 or not text[:4].isdigit():
        return None
    year = int(text[:4])
    suffix = text[4:]
    if suffix == "0331":
        return f"{year - 1}1231"
    if suffix == "0630":
        return f"{year}0331"
    if suffix == "0930":
        return f"{year}0630"
    if suffix == "1231":
        return f"{year}0930"
    return None


def _filter_growth_events(fin: pd.DataFrame, min_growth: float) -> pd.DataFrame:
    """Compatibility path for databases that have not created the PIT store."""

    fin["ann_date"] = fin["ann_date"].astype(str)
    fin["end_date"] = fin["end_date"].astype(str)
    fin = fin.drop_duplicates(["ts_code", "end_date"]).sort_values(["ts_code", "end_date"])
    q = {(r.ts_code, r.end_date): r.q_dtprofit for r in fin.itertuples(index=False)}

    def yoy(row):
        end = str(row.end_date)
        base_end = f"{int(end[:4]) - 1}{end[4:]}"
        base = q.get((row.ts_code, base_end))
        if base is None or base <= 0 or pd.isna(base):
            return None
        return (row.q_dtprofit / base - 1.0) * 100.0

    fin["dedt_yoy"] = [yoy(r) for r in fin.itertuples(index=False)]
    fin["prev_end"] = fin.groupby("ts_code")["end_date"].shift(1)
    fin["prev_dedt_yoy"] = fin.groupby("ts_code")["dedt_yoy"].shift(1)
    fin = fin.dropna(subset=["dedt_yoy", "prev_dedt_yoy"])
    fin = fin[(fin["dedt_yoy"] > min_growth) & (fin["dedt_yoy"] > fin["prev_dedt_yoy"])]
    fin["ann_dt"] = pd.to_datetime(fin["ann_date"], format="%Y%m%d", errors="coerce")
    fin = fin.dropna(subset=["ann_dt"])
    if "pit_quality" not in fin:
        fin["pit_quality"] = "legacy_current_snapshot"
    return fin.sort_values("ann_dt").reset_index(drop=True)


def _point_in_time_growth_events(versions: pd.DataFrame, min_growth: float) -> pd.DataFrame:
    """Replay disclosure versions in announcement order and compute as-of growth."""

    if versions.empty:
        return versions
    work = versions.copy()
    work["ann_date"] = work["ann_date"].astype(str).str.replace(r"\D", "", regex=True).str[:8]
    work["end_date"] = work["end_date"].astype(str).str.replace(r"\D", "", regex=True).str[:8]
    work["q_dtprofit"] = pd.to_numeric(work["q_dtprofit"], errors="coerce")
    work["update_order"] = pd.to_numeric(work.get("update_flag"), errors="coerce").fillna(0)
    work["source_priority"] = work.get("source", "").astype(str).eq("tushare.fina_indicator").astype(int)
    work = work.dropna(subset=["q_dtprofit"])
    work = work[(work["ann_date"].str.len() == 8) & (work["end_date"].str.len() == 8)]
    work = work.sort_values(
        ["ts_code", "ann_date", "end_date", "source_priority", "update_order", "ingested_at", "version_id"]
    ).drop_duplicates(["ts_code", "ann_date", "end_date"], keep="last")

    events: list[dict] = []
    for ts_code, company in work.groupby("ts_code", sort=True):
        state: dict[str, dict] = {}
        for ann_date, disclosed in company.groupby("ann_date", sort=True):
            rows = [row._asdict() for row in disclosed.itertuples(index=False)]
            for row in rows:
                state[str(row["end_date"])] = row
            for row in rows:
                end_date = str(row["end_date"])
                previous_end = _previous_quarter_end(end_date)
                prior_year_end = f"{int(end_date[:4]) - 1}{end_date[4:]}"
                if previous_end is None:
                    continue
                prior_year_previous_end = f"{int(previous_end[:4]) - 1}{previous_end[4:]}"
                current = state.get(end_date)
                current_base = state.get(prior_year_end)
                previous = state.get(previous_end)
                previous_base = state.get(prior_year_previous_end)
                if not all((current, current_base, previous, previous_base)):
                    continue
                base_value = float(current_base["q_dtprofit"])
                previous_base_value = float(previous_base["q_dtprofit"])
                if base_value <= 0 or previous_base_value <= 0:
                    continue
                dedt_yoy = (float(current["q_dtprofit"]) / base_value - 1.0) * 100.0
                prev_dedt_yoy = (float(previous["q_dtprofit"]) / previous_base_value - 1.0) * 100.0
                if dedt_yoy <= min_growth or dedt_yoy <= prev_dedt_yoy:
                    continue
                components = (current, current_base, previous, previous_base)
                native = all(
                    str(component.get("source") or "") == "tushare.fina_indicator"
                    for component in components
                )
                events.append({
                    "ts_code": ts_code,
                    "ann_date": ann_date,
                    "end_date": end_date,
                    "q_dtprofit": float(current["q_dtprofit"]),
                    "dedt_yoy": dedt_yoy,
                    "prev_end": previous_end,
                    "prev_dedt_yoy": prev_dedt_yoy,
                    "pit_quality": "native_versions" if native else "legacy_snapshot_component",
                    "source": current.get("source"),
                    "source_vintage": current.get("source_vintage"),
                    "version_id": current.get("version_id"),
                })
    result = pd.DataFrame(events)
    if result.empty:
        return result
    result["ann_dt"] = pd.to_datetime(result["ann_date"], format="%Y%m%d", errors="coerce")
    return result.dropna(subset=["ann_dt"]).sort_values(
        ["ann_dt", "ts_code", "end_date"]
    ).reset_index(drop=True)


def load_financial_events(db_path: Path, min_growth: float) -> pd.DataFrame:
    with sqlite3.connect(str(db_path)) as conn:
        has_versions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fina_indicator_versions'"
        ).fetchone()
        if has_versions:
            versions = pd.read_sql_query(
                "SELECT version_id, ts_code, ann_date, end_date, update_flag, q_dtprofit, "
                "ingested_at, source, source_vintage FROM fina_indicator_versions "
                "WHERE ann_date IS NOT NULL AND end_date IS NOT NULL AND q_dtprofit IS NOT NULL",
                conn,
            )
            return _point_in_time_growth_events(versions, min_growth)
        fin = pd.read_sql_query(
            "SELECT ts_code, ann_date, end_date, q_dtprofit FROM fina_indicators "
            "WHERE ann_date IS NOT NULL AND end_date IS NOT NULL AND q_dtprofit IS NOT NULL",
            conn,
        )
    return _filter_growth_events(fin, min_growth)


def load_price_panel(parquet_dir: Path, start: str, end: str, codes: set[str]) -> tuple[pd.DataFrame, list[str]]:
    files = sorted(parquet_dir.glob("*.parquet"))
    files = [p for p in files if start <= p.stem <= end]
    rows = []
    trade_dates = []
    for p in files:
        df = pd.read_parquet(p, columns=["ts_code", "trade_date", "open", "close", "adj_factor"])
        trade_dates.append(p.stem)
        df["c6"] = df["ts_code"].map(c6)
        df["open_adj"] = pd.to_numeric(df["open"], errors="coerce") * pd.to_numeric(df["adj_factor"], errors="coerce")
        df["close_adj"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(df["adj_factor"], errors="coerce")
        rows.append(df[df["c6"].isin(codes)][["c6", "trade_date", "open_adj", "close_adj"]])
    panel = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["c6", "trade_date", "open_adj", "close_adj"])
    return panel, trade_dates


def load_market_returns(
    parquet_dir: Path,
    trade_dates: list[str],
    horizons: list[int],
) -> dict[tuple, float]:
    """Build a daily-rebalanced all-A equal-weight benchmark with matched ids.

    Both open-to-close and close-to-close returns are retained because an event
    can enter at either price.  The legacy two-element key remains as an alias
    for open entry so older callers keep working.  The implementation streams
    adjacent days rather than retaining the full all-A panel in memory.
    """
    intraday_return: dict[str, float] = {}
    close_to_close_return: dict[str, float] = {}
    previous_close: pd.Series | None = None
    for d in trade_dates:
        p = parquet_dir / f"{d}.parquet"
        df = pd.read_parquet(p, columns=["ts_code", "open", "close", "adj_factor"])
        adj = pd.to_numeric(df["adj_factor"], errors="coerce")
        frame = pd.DataFrame({
            "code": df["ts_code"].map(c6),
            "open_adj": pd.to_numeric(df["open"], errors="coerce") * adj,
            "close_adj": pd.to_numeric(df["close"], errors="coerce") * adj,
        })
        frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["code"])
        frame = frame.drop_duplicates("code", keep="last").set_index("code")
        same_day = frame[["open_adj", "close_adj"]].dropna()
        same_day = same_day[(same_day["open_adj"] > 0) & (same_day["close_adj"] > 0)]
        if not same_day.empty:
            intraday_return[d] = float((same_day["close_adj"] / same_day["open_adj"] - 1.0).mean())
        if previous_close is not None:
            aligned = pd.concat(
                [previous_close.rename("start"), frame["close_adj"].rename("finish")],
                axis=1,
                join="inner",
            ).replace([np.inf, -np.inf], np.nan).dropna()
            aligned = aligned[(aligned["start"] > 0) & (aligned["finish"] > 0)]
            if not aligned.empty:
                close_to_close_return[d] = float((aligned["finish"] / aligned["start"] - 1.0).mean())
        previous_close = frame["close_adj"].copy()

    out: dict[tuple, float] = {}
    for i, d in enumerate(trade_dates):
        for h in horizons:
            j = i + h - 1
            if j >= len(trade_dates):
                continue
            later_days = trade_dates[i + 1:j + 1]
            later_legs = [close_to_close_return.get(day) for day in later_days]
            if any(value is None for value in later_legs):
                continue
            close_value = float(np.prod([1.0 + float(value) for value in later_legs]) - 1.0)
            out[(d, h, "close")] = close_value
            first_leg = intraday_return.get(d)
            if first_leg is not None:
                open_value = float(
                    (1.0 + first_leg)
                    * np.prod([1.0 + float(value) for value in later_legs])
                    - 1.0
                )
                out[(d, h, "open")] = open_value
                out[(d, h)] = open_value
    return out


def load_announcement_times(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[tuple[str, str], dict] = {}
    for row in payload.get("items") or []:
        code = c6(row.get("code") or row.get("symbol") or "")
        ann_date = str(row.get("ann_date") or row.get("date") or "")[:10].replace("-", "")
        if not code or len(ann_date) != 8:
            continue
        dt_text = str(row.get("ann_datetime") or "").strip()
        dt = None
        if dt_text:
            parsed = pd.to_datetime(dt_text, errors="coerce")
            if not pd.isna(parsed):
                dt = parsed
        old = out.get((code, ann_date))
        if old is None or (dt is not None and (old.get("ann_dt") is None or dt < old.get("ann_dt"))):
            out[(code, ann_date)] = {
                "ann_dt": dt,
                "ann_datetime": dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else "",
                "cninfo_ann_date": ann_date,
                "title": row.get("title") or "",
                "type": row.get("type") or "",
                "url": row.get("url") or "",
            }
    return out


def lookup_announcement_time(ann_times: dict[tuple[str, str], dict], code: str, ann: str) -> dict | None:
    exact = ann_times.get((code, ann))
    if exact:
        return exact
    ann_ts = pd.to_datetime(ann, format="%Y%m%d", errors="coerce")
    if pd.isna(ann_ts):
        return None
    candidates = []
    for offset in (-2, -1, 1, 2):
        day = (ann_ts + pd.Timedelta(days=offset)).strftime("%Y%m%d")
        row = ann_times.get((code, day))
        if row:
            candidates.append((abs(offset), row))
    return sorted(candidates, key=lambda x: x[0])[0][1] if candidates else None


def choose_entry_index(dates: list[str], ann: str, ann_info: dict | None, mode: str) -> tuple[int | None, str, str]:
    if mode == "conservative" or not ann_info or ann_info.get("ann_dt") is None:
        idx = next((i for i, d in enumerate(dates) if d > ann), None)
        return idx, "next_open_conservative" if mode == "conservative" else "next_open_no_cninfo_time", "open_adj"

    ann_dt = ann_info["ann_dt"]
    cninfo_day = ann_dt.strftime("%Y%m%d")
    if cninfo_day in dates:
        i = dates.index(cninfo_day)
        if ann_dt.time() <= time(9, 30):
            return i, "same_day_open_by_cninfo_time", "open_adj"
        if ann_dt.time() <= time(15, 0):
            return i, "same_day_close_intraday_cninfo_time", "close_adj"
        return (i + 1 if i + 1 < len(dates) else None), "next_open_after_close", "open_adj"
    idx = next((i for i, d in enumerate(dates) if d > cninfo_day), None)
    return idx, "next_open_non_trading_announcement_day", "open_adj"


def choose_entry_index_after_announcement_lag(
    dates: list[str],
    ann: str,
    lag_days: int,
) -> tuple[int | None, str, str]:
    """Return the open on the Nth trading day after the announcement date."""
    lag = max(1, int(lag_days))
    first_i = next((i for i, d in enumerate(dates) if d > ann), None)
    if first_i is None:
        return None, f"ann_plus_{lag}_trading_day_open", "open_adj"
    entry_i = first_i + lag - 1
    if entry_i >= len(dates):
        return None, f"ann_plus_{lag}_trading_day_open", "open_adj"
    return entry_i, f"ann_plus_{lag}_trading_day_open", "open_adj"


def summarize(vals: list[float], h: int) -> dict:
    arr = np.asarray(vals, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return {"n": 0}
    mean = float(arr.mean())
    sd = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    periods = 252.0 / h
    return {
        "n": int(len(arr)),
        "mean_pct": round(mean * 100, 3),
        "median_pct": round(float(np.median(arr)) * 100, 3),
        "win_rate_pct": round(float((arr > 0).mean()) * 100, 1),
        "ann_pct": round(mean * periods * 100, 2),
        "sharpe": round((mean / sd * math.sqrt(periods)) if sd > 0 else 0.0, 3),
    }


def build_trades(events: pd.DataFrame, by_code: dict[str, pd.DataFrame], market: dict[tuple[str, int], float],
                 horizons: list[int], topn: int, ann_times: dict[tuple[str, str], dict],
                 entry_mode: str, entry_lag_days: int | None = None) -> list[dict]:
    picks_by_date: dict[str, list[dict]] = defaultdict(list)
    for r in events.itertuples(index=False):
        ann = r.ann_dt.strftime("%Y%m%d")
        picks_by_date[ann].append({
            "ts_code": r.ts_code,
            "code": c6(r.ts_code),
            "ann_date": ann,
            "end_date": r.end_date,
            "dedt_yoy": float(r.dedt_yoy),
            "prev_dedt_yoy": float(r.prev_dedt_yoy),
            "delta": float(r.dedt_yoy - r.prev_dedt_yoy),
        })

    trades = []
    for ann, rows in sorted(picks_by_date.items()):
        rows = sorted(rows, key=lambda x: (-x["dedt_yoy"], -x["delta"], x["code"]))[:topn]
        for row in rows:
            px = by_code.get(row["code"])
            if px is None or px.empty:
                continue
            dates = px["trade_date"].astype(str).tolist()
            ann_info = lookup_announcement_time(ann_times, row["code"], ann)
            if entry_lag_days is not None:
                entry_i, entry_rule, entry_price_col = choose_entry_index_after_announcement_lag(
                    dates, ann, entry_lag_days
                )
            else:
                entry_i, entry_rule, entry_price_col = choose_entry_index(dates, ann, ann_info, entry_mode)
            if entry_i is None:
                continue
            entry_price = float(px.loc[entry_i, entry_price_col])
            if not np.isfinite(entry_price) or entry_price <= 0:
                continue
            cninfo_date = (ann_info or {}).get("cninfo_ann_date", "")
            rec = dict(row)
            rec["entry_date"] = dates[entry_i]
            rec["entry_price_type"] = "close" if entry_price_col == "close_adj" else "open"
            rec["entry_mode"] = entry_mode
            rec["entry_rule"] = entry_rule
            if entry_i > 0:
                prev_close = float(px.loc[entry_i - 1, "close_adj"])
                rec["entry_gap_pct"] = round((entry_price / prev_close - 1.0) * 100, 3) if prev_close > 0 else None
            else:
                rec["entry_gap_pct"] = None
            rec["cninfo_ann_date"] = cninfo_date
            rec["cninfo_ann_datetime"] = (ann_info or {}).get("ann_datetime", "")
            rec["ann_date_match"] = "same" if cninfo_date == ann else ("nearby" if cninfo_date else "missing")
            rec["cninfo_title"] = (ann_info or {}).get("title", "")
            rec["cninfo_url"] = (ann_info or {}).get("url", "")
            for h in horizons:
                j = entry_i + h - 1
                if j >= len(px):
                    rec[f"ret_{h}"] = None
                    rec[f"excess_{h}"] = None
                    continue
                exit_close = float(px.loc[j, "close_adj"])
                raw = exit_close / entry_price - 1.0 if np.isfinite(exit_close) and exit_close > 0 else None
                rec[f"ret_{h}"] = None if raw is None else round(raw * 100, 3)
                bm = market.get((dates[entry_i], h, rec["entry_price_type"]))
                if bm is None:
                    bm = market.get((dates[entry_i], h))
                rec[f"benchmark_{h}"] = None if bm is None else round(float(bm) * 100, 3)
                rec[f"excess_{h}"] = (
                    None if raw is None or bm is None else round((raw - float(bm)) * 100, 3)
                )
            trades.append(rec)
    return trades


def build_entry_lag_analysis(
    events: pd.DataFrame,
    by_code: dict[str, pd.DataFrame],
    market: dict[tuple[str, int], float],
    horizons: list[int],
    topn: int,
    lags: list[int] | None = None,
) -> dict:
    out = {}
    for lag in lags or [1, 2, 3, 4, 5]:
        trades = build_trades(
            events,
            by_code,
            market,
            horizons,
            topn,
            ann_times={},
            entry_mode=f"ann_plus_{lag}_trading_day",
            entry_lag_days=lag,
        )
        summary, by_year = summarize_trades(trades, horizons)
        out[str(lag)] = {
            "label": f"公告后第{lag}个交易日开盘买入",
            "n_events": len(trades),
            "summary": summary,
            "by_year": by_year,
            "sample": trades[-30:],
        }
    return out


def summarize_trades(trades: list[dict], horizons: list[int]) -> tuple[dict, dict]:
    summary = {}
    for h in horizons:
        summary[str(h)] = summarize([t[f"ret_{h}"] / 100.0 for t in trades if t.get(f"ret_{h}") is not None], h)
    by_year = {}
    for y in sorted({t["entry_date"][:4] for t in trades}):
        yr = [t for t in trades if t["entry_date"].startswith(y)]
        by_year[y] = {str(h): summarize([t[f"ret_{h}"] / 100.0 for t in yr if t.get(f"ret_{h}") is not None], h)
                      for h in horizons}
    return summary, by_year


def _chart_date(value: str) -> str:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def build_event_curves(trades: list[dict], horizons: list[int]) -> dict:
    curves = {}
    for h in horizons:
        by_day: dict[str, list[float]] = defaultdict(list)
        for trade in trades:
            day = str(trade.get("entry_date") or "")
            value = trade.get(f"ret_{h}")
            if not day or value is None:
                continue
            try:
                ret = float(value) / 100.0
            except Exception:
                continue
            if np.isfinite(ret):
                by_day[day].append(ret)
        dates = []
        nav = []
        daily_return_pct = []
        n_events = []
        value = 1.0
        for day in sorted(by_day):
            vals = np.asarray(by_day[day], dtype=float)
            vals = vals[np.isfinite(vals)]
            if len(vals) == 0:
                continue
            day_ret = float(vals.mean())
            value *= max(0.0, 1.0 + day_ret)
            dates.append(_chart_date(day))
            nav.append(round(value, 4))
            daily_return_pct.append(round(day_ret * 100, 3))
            n_events.append(int(len(vals)))
        curves[str(h)] = {
            "dates": dates,
            "nav": nav,
            "daily_return_pct": daily_return_pct,
            "n_events": n_events,
            "method": "same-entry-date equal-weight basket, compounded by event date",
        }
    return curves


def build_price_context(by_code: dict[str, pd.DataFrame]) -> dict:
    close_by_code: dict[str, dict[str, float]] = {}
    close_dates_by_code: dict[str, list[str]] = {}
    ma_by_code: dict[str, dict[str, float]] = {}
    feature_by_code: dict[str, dict[str, dict]] = {}
    for code, df in by_code.items():
        if df is None or df.empty or "close_adj" not in df:
            continue
        g = df.sort_values("trade_date").reset_index(drop=True).copy()
        close = pd.to_numeric(g["close_adj"], errors="coerce")
        g["ma20"] = close.rolling(20, min_periods=5).mean()
        g["max252"] = close.rolling(252, min_periods=20).max()
        g["ret20"] = close / close.shift(20) - 1.0
        g["ret60"] = close / close.shift(60) - 1.0
        g["peak120"] = close.rolling(120, min_periods=20).max()
        g["drawdown"] = close / g["peak120"] - 1.0
        g["vol20"] = close.pct_change().rolling(20, min_periods=5).std()
        close_by_code[code] = {}
        ma_by_code[code] = {}
        feature_by_code[code] = {}
        for r in g[["trade_date", "close_adj", "ma20", "max252", "ret20", "ret60", "drawdown", "vol20"]].itertuples(index=False):
            close_value = float(r.close_adj)
            if not np.isfinite(close_value) or close_value <= 0:
                continue
            dt = str(r.trade_date)
            close_by_code[code][dt] = close_value
            ma_by_code[code][dt] = float(r.ma20) if np.isfinite(r.ma20) else None
            max252 = float(r.max252) if np.isfinite(r.max252) and r.max252 else close_value
            feature_by_code[code][dt] = {
                "new_high": close_value / max252 if max252 else 0.0,
                "ret20": float(r.ret20) if np.isfinite(r.ret20) else 0.0,
                "ret60": float(r.ret60) if np.isfinite(r.ret60) else 0.0,
                "drawdown": float(r.drawdown) if np.isfinite(r.drawdown) else 0.0,
                "vol20": float(r.vol20) if np.isfinite(r.vol20) else 0.0,
            }
        close_dates_by_code[code] = sorted(close_by_code[code])
    return {
        "close_by_code": close_by_code,
        "close_dates_by_code": close_dates_by_code,
        "ma_by_code": ma_by_code,
        "feature_by_code": feature_by_code,
    }


def build_rolling_portfolio_curve(
    trades: list[dict],
    by_code: dict[str, pd.DataFrame],
    trade_dates: list[str],
    topn: int = 50,
    active_days: int = 180,
    market_proxy: dict[str, float] | None = None,
    use_market_risk: bool = False,
    use_momentum: bool = False,
    unlocks_by_code: dict[str, list[str]] | None = None,
    avoid_unlock: bool = False,
    score_style: str = "earnings",
    exclude_codes: set[str] | None = None,
    max_pre_runup_20: float | None = None,
    max_entry_gap_pct: float | None = None,
    min_signal_growth: float | None = None,
    min_signal_delta: float | None = None,
    price_context: dict | None = None,
    buy_cost_rate: float = 0.0,
    sell_cost_rate: float = 0.0,
    impact_cost_rate: float = 0.0,
) -> dict:
    signals_by_day: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        code = c6(trade.get("code") or trade.get("ts_code"))
        day = str(trade.get("entry_date") or "")
        if code and day:
            row = dict(trade)
            row["code"] = code
            signals_by_day[day].append(row)

    price_context = price_context or build_price_context(by_code)
    close_by_code = price_context.get("close_by_code") or {}
    close_dates_by_code = price_context.get("close_dates_by_code") or {
        code: sorted(px) for code, px in close_by_code.items()
    }
    ma_by_code = price_context.get("ma_by_code") or {}
    feature_by_code = price_context.get("feature_by_code") or {}
    market_dates = sorted((market_proxy or {}).keys())

    def market_exposure(day: str) -> float:
        if not use_market_risk or not market_dates:
            return 1.0
        vals = [market_proxy[d] for d in market_dates if d <= day and market_proxy.get(d) is not None]
        if len(vals) < 60:
            return 1.0
        cur = vals[-1]
        ma60 = float(np.mean(vals[-60:]))
        ma120 = float(np.mean(vals[-120:])) if len(vals) >= 120 else ma60
        if cur >= ma60:
            return 1.0
        if cur >= ma120:
            return 0.5
        return 0.3

    def momentum_ok(code: str, day: str) -> bool:
        if not use_momentum:
            return True
        close = (close_by_code.get(code) or {}).get(day)
        ma20 = (ma_by_code.get(code) or {}).get(day)
        return bool(close is not None and ma20 is not None and np.isfinite(ma20) and close >= ma20)

    def unlock_ok(code: str, day: str) -> bool:
        if not avoid_unlock:
            return True
        day_ts = pd.to_datetime(day, format="%Y%m%d", errors="coerce")
        if pd.isna(day_ts):
            return True
        for unlock in (unlocks_by_code or {}).get(code, []):
            unlock_ts = pd.to_datetime(unlock, errors="coerce")
            if pd.isna(unlock_ts):
                continue
            days = (unlock_ts - day_ts).days
            if -30 <= days <= 90:
                return False
        return True

    def pre_runup_ok(sig: dict) -> bool:
        if max_pre_runup_20 is None:
            return True
        code = sig.get("code") or ""
        ann = str(sig.get("ann_date") or sig.get("entry_date") or "")
        px = close_by_code.get(code) or {}
        ds_all = close_dates_by_code.get(code) or []
        pos = bisect_right(ds_all, ann)
        if pos < 2:
            return True
        end_i = pos - 1
        base_i = max(0, pos - 21)
        p0 = px.get(ds_all[base_i])
        p1 = px.get(ds_all[end_i])
        if not p0 or not p1:
            return True
        return (p1 / p0 - 1.0) * 100.0 <= float(max_pre_runup_20)

    def entry_gap_ok(sig: dict) -> bool:
        if max_entry_gap_pct is None:
            return True
        gap = sig.get("entry_gap_pct")
        if gap is None:
            return True
        try:
            return float(gap) <= float(max_entry_gap_pct)
        except Exception:
            return True

    def signal_strength_ok(sig: dict) -> bool:
        if min_signal_growth is not None:
            try:
                if float(sig.get("dedt_yoy") or 0.0) < float(min_signal_growth):
                    return False
            except Exception:
                return False
        if min_signal_delta is not None:
            try:
                if float(sig.get("delta") or 0.0) < float(min_signal_delta):
                    return False
            except Exception:
                return False
        return True

    def price_features(code: str, day: str) -> dict:
        by_day = feature_by_code.get(code) or {}
        if day in by_day:
            return by_day[day]
        ds = [d for d in by_day if d <= day]
        return by_day[max(ds)] if ds else {}

    def signal_score(sig: dict, day: str) -> float:
        base = math.log1p(max(float(sig.get("dedt_yoy") or 0), 0.0)) * 10.0
        base += max(min(float(sig.get("delta") or 0), 300.0), -100.0) / 20.0
        feat = price_features(sig.get("code") or "", day)
        style = str(score_style or "earnings").upper()
        if style == "MOM":
            return base + 25.0 * feat.get("new_high", 0.0) + 40.0 * feat.get("ret60", 0.0) + 25.0 * feat.get("ret20", 0.0)
        if style == "VAL":
            return base + 15.0 * feat.get("new_high", 0.0) - 120.0 * feat.get("vol20", 0.0) + 20.0 * feat.get("drawdown", 0.0)
        return base

    latest: dict[str, dict] = {}
    dates: list[str] = []
    nav: list[float] = []
    daily_return_pct: list[float] = []
    holding_count: list[int] = []
    top_codes: list[list[str]] = []
    turnover_pct: list[float] = []
    trading_cost_pct: list[float] = []
    value = 1.0
    cost_multiplier = 1.0
    prev_holdings: list[str] = []
    prev_day = None

    for day in sorted(str(d) for d in trade_dates):
        if prev_day and prev_holdings:
            rets = []
            for code in prev_holdings:
                px = close_by_code.get(code) or {}
                p0 = px.get(prev_day)
                p1 = px.get(day)
                if p0 and p1:
                    rets.append(p1 / p0 - 1.0)
            if rets:
                value *= max(0.0, 1.0 + float(np.mean(rets)) * market_exposure(prev_day))

        for sig in signals_by_day.get(day, []):
            if (
                sig["code"] not in (exclude_codes or set())
                and signal_strength_ok(sig)
                and pre_runup_ok(sig)
                and entry_gap_ok(sig)
            ):
                latest[sig["code"]] = sig

        day_ts = pd.to_datetime(day, format="%Y%m%d", errors="coerce")
        active = []
        for sig in latest.values():
            entry_ts = pd.to_datetime(sig.get("entry_date"), format="%Y%m%d", errors="coerce")
            if pd.isna(day_ts) or pd.isna(entry_ts) or (day_ts - entry_ts).days <= active_days:
                code = sig.get("code") or ""
                if momentum_ok(code, day) and unlock_ok(code, day):
                    active.append(sig)
        active.sort(key=lambda x: (-signal_score(x, day), x.get("code") or ""))
        holdings = [x["code"] for x in active[:topn]]

        prev_exposure = market_exposure(prev_day) if prev_day and prev_holdings else 0.0
        target_exposure = market_exposure(day) if holdings else 0.0
        prev_weight = (prev_exposure / len(prev_holdings)) if prev_holdings else 0.0
        target_weight = (target_exposure / len(holdings)) if holdings else 0.0
        names = set(prev_holdings) | set(holdings)
        buy_turnover = sum(
            max((target_weight if code in holdings else 0.0) - (prev_weight if code in prev_holdings else 0.0), 0.0)
            for code in names
        )
        sell_turnover = sum(
            max((prev_weight if code in prev_holdings else 0.0) - (target_weight if code in holdings else 0.0), 0.0)
            for code in names
        )
        cost_rate = (
            buy_turnover * max(0.0, float(buy_cost_rate))
            + sell_turnover * max(0.0, float(sell_cost_rate))
            + (buy_turnover + sell_turnover) * max(0.0, float(impact_cost_rate))
        )
        if cost_rate > 0:
            value *= max(0.0, 1.0 - cost_rate)
            cost_multiplier *= max(0.0, 1.0 - cost_rate)

        dates.append(_chart_date(day))
        nav.append(round(value, 4))
        daily_return_pct.append(round((value / nav[-2] - 1.0) * 100, 3) if len(nav) > 1 and nav[-2] else 0.0)
        holding_count.append(len(holdings))
        top_codes.append(holdings[:10])
        turnover_pct.append(round((buy_turnover + sell_turnover) * 100.0, 3))
        trading_cost_pct.append(round(cost_rate * 100.0, 4))
        prev_holdings = holdings
        prev_day = day

    return {
        "label": "滚动业绩组合净值",
        "dates": dates,
        "nav": nav,
        "daily_return_pct": daily_return_pct,
        "holding_count": holding_count,
        "top_codes": top_codes,
        "turnover_pct": turnover_pct,
        "trading_cost_pct": trading_cost_pct,
        "cumulative_cost_drag_pct": round((1.0 - cost_multiplier) * 100.0, 3),
        "topn": topn,
        "active_days": active_days,
        "use_market_risk": use_market_risk,
        "use_momentum": use_momentum,
        "avoid_unlock": avoid_unlock,
        "score_style": score_style,
        "exclude_codes": len(exclude_codes or set()),
        "max_pre_runup_20": max_pre_runup_20,
        "max_entry_gap_pct": max_entry_gap_pct,
        "min_signal_growth": min_signal_growth,
        "min_signal_delta": min_signal_delta,
        "execution_assumptions": {
            "buy_cost_rate": float(buy_cost_rate),
            "sell_cost_rate": float(sell_cost_rate),
            "impact_cost_rate": float(impact_cost_rate),
            "capacity_model": False,
            "tradability_model": False,
        },
        "method": "new earnings signals update candidate pool; daily topN equal-weight rebalance with explicit turnover costs",
    }


def build_market_proxy(by_code: dict[str, pd.DataFrame], trade_dates: list[str]) -> dict[str, float]:
    norm_by_code = {}
    for code, df in by_code.items():
        if df is None or df.empty or "close_adj" not in df:
            continue
        g = df.sort_values("trade_date").reset_index(drop=True)
        first = pd.to_numeric(g["close_adj"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if first.empty or first.iloc[0] <= 0:
            continue
        base = float(first.iloc[0])
        norm_by_code[code] = {
            str(r.trade_date): float(r.close_adj) / base
            for r in g[["trade_date", "close_adj"]].itertuples(index=False)
            if np.isfinite(float(r.close_adj)) and float(r.close_adj) > 0
        }
    out = {}
    for day in trade_dates:
        vals = [m[day] for m in norm_by_code.values() if day in m]
        if vals:
            out[day] = float(np.median(vals))
    return out


def load_unlock_dates(data_dir: Path | None) -> dict[str, list[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    if not data_dir:
        return {}
    for name in ("cninfo_transfer.json", "placement_status.json", "cninfo_placement.json", "asset_injection.json"):
        path = data_dir / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = c6(row.get("code") or row.get("symbol") or row.get("ts_code"))
            unlock = str(row.get("unlock_date") or row.get("unlockDate") or row.get("release_date") or "")[:10]
            if code and unlock:
                out[code].add(unlock)
    return {code: sorted(vals) for code, vals in out.items()}


def load_advisor_codes(data_dir: Path | None) -> set[str]:
    if not data_dir:
        return set()
    payload = None
    for path in (data_dir / "regime_advisor_pro.json", data_dir / "advisor_pro_plus.json"):
        payload = read_json(path)
        if payload:
            break
    if not isinstance(payload, dict):
        return set()
    rows = []
    cur = payload.get("current") or {}
    trade = payload.get("trade") or {}
    if isinstance(cur.get("basket"), list):
        rows.extend(cur.get("basket") or [])
    if isinstance(trade.get("items"), list):
        rows.extend([x for x in trade.get("items") or [] if str(x.get("action") or "") in ("买入", "持有", "涔板叆", "鎸佹湁")])
    return {c6(x.get("code") or x.get("ts_code")) for x in rows if c6(x.get("code") or x.get("ts_code"))}


def summarize_curve(curve: dict) -> dict:
    nav = np.asarray(curve.get("nav") or [], dtype=float)
    nav = nav[np.isfinite(nav)]
    if len(nav) == 0:
        return {"n": 0}
    peak = np.maximum.accumulate(nav)
    dd = nav / peak - 1.0
    daily = np.diff(nav) / nav[:-1] if len(nav) > 1 else np.asarray([])
    return {
        "n": int(len(nav)),
        "final_nav": round(float(nav[-1]), 4),
        "total_pct": round((float(nav[-1]) - 1.0) * 100, 2),
        "mdd_pct": round(float(dd.min()) * 100, 2),
        "sharpe": round(float(daily.mean() / daily.std(ddof=1) * math.sqrt(252)) if len(daily) > 1 and daily.std(ddof=1) > 0 else 0.0, 3),
        "win_rate_pct": round(float((daily > 0).mean()) * 100, 1) if len(daily) else None,
    }


def choose_rolling_recommended(
    variants: dict,
    baseline_key: str = "advisor_gap_cool",
    max_drawdown_slippage_pct: float = 2.0,
    respect_selection_eligibility: bool = True,
) -> str:
    if not variants:
        return baseline_key
    eligible = {
        key: curve for key, curve in variants.items()
        if not respect_selection_eligibility or (curve or {}).get("selection_eligible", True)
    }
    if not eligible:
        return baseline_key if baseline_key in variants else next(iter(variants))
    if baseline_key not in eligible:
        baseline_key = "base" if "base" in eligible else next(iter(eligible))
    baseline = ((eligible.get(baseline_key) or {}).get("summary") or {})
    base_nav = float(baseline.get("final_nav") or 0.0)
    base_mdd = float(baseline.get("mdd_pct") or -100.0)
    candidates = []
    for key, curve in eligible.items():
        summary = (curve or {}).get("summary") or {}
        nav = float(summary.get("final_nav") or 0.0)
        sharpe = float(summary.get("sharpe") or 0.0)
        win_rate = float(summary.get("win_rate_pct") or 0.0)
        mdd = float(summary.get("mdd_pct") or -100.0)
        if nav >= base_nav and mdd >= base_mdd - max_drawdown_slippage_pct:
            candidates.append((sharpe, nav, win_rate, mdd, key))
    if not candidates:
        return baseline_key
    return sorted(candidates, reverse=True)[0][-1]


def build_rolling_portfolio_variants(
    trades: list[dict],
    by_code: dict[str, pd.DataFrame],
    trade_dates: list[str],
    topn: int,
    data_dir: Path | None,
) -> dict:
    price_context = build_price_context(by_code)
    market_proxy = build_market_proxy(by_code, trade_dates)
    unlocks = load_unlock_dates(data_dir)
    advisor_codes = load_advisor_codes(data_dir)
    specs = [
        ("base", "原始滚动业绩", {}),
        ("market_risk", "市场风控", {"use_market_risk": True, "market_proxy": market_proxy}),
        ("advisor_gap", "Pro未覆盖", {"exclude_codes": advisor_codes}),
        ("advisor_gap_cool", "Pro未覆盖+公告前不过热", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0}),
        ("advisor_gap_cool_gap5", "Pro未覆盖+不过热+高开<5%", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "max_entry_gap_pct": 5.0}),
        ("advisor_gap_cool_gap8", "Pro未覆盖+不过热+高开<8%", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "max_entry_gap_pct": 8.0}),
        ("advisor_gap_cool_top10", "Pro未覆盖+不过热+Top10", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "topn": 10}),
        ("advisor_gap_cool_top20", "Pro未覆盖+不过热+Top20", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "topn": 20}),
        ("advisor_gap_cool_g50", "Pro未覆盖+不过热+扣非>50%", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "min_signal_growth": 50.0}),
        ("advisor_gap_cool_g100", "Pro未覆盖+不过热+扣非>100%", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "min_signal_growth": 100.0}),
        ("advisor_gap_cool_d20", "Pro未覆盖+不过热+改善>20", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "min_signal_delta": 20.0}),
        ("advisor_gap_cool_d50", "Pro未覆盖+不过热+改善>50", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "min_signal_delta": 50.0}),
        ("advisor_gap_cool_ma20", "Pro未覆盖+不过热+站上20日线", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "use_momentum": True}),
        ("advisor_gap_cool_mom", "Pro未覆盖+不过热+动量排序", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "score_style": "MOM"}),
        ("advisor_gap_cool_ma20_mom", "Pro未覆盖+不过热+20日线+动量", {"exclude_codes": advisor_codes, "max_pre_runup_20": 30.0, "use_momentum": True, "score_style": "MOM"}),
    ]
    out = {}
    for key, label, kwargs in specs:
        params = dict(kwargs)
        variant_topn = int(params.pop("topn", topn))
        curve = build_rolling_portfolio_curve(
            trades,
            by_code,
            trade_dates,
            topn=variant_topn,
            price_context=price_context,
            buy_cost_rate=DEFAULT_BUY_COST_RATE,
            sell_cost_rate=DEFAULT_SELL_COST_RATE,
            impact_cost_rate=DEFAULT_IMPACT_COST_RATE,
            **params,
        )
        curve["label"] = label
        # Only the pre-registered base specification can be called deployable.
        # Every alternative below was compared on the same full history; variants
        # using today's Advisor basket additionally carry point-in-time leakage.
        curve["selection_eligible"] = key == "base"
        curve["selection_note"] = (
            "pre_registered_base"
            if key == "base"
            else "research_only_full_sample_variant"
        )
        if params.get("exclude_codes"):
            curve["lookahead_risk"] = "current_advisor_codes_applied_to_history"
        curve["summary"] = summarize_curve(curve)
        out[key] = curve
    return out


def run_backtest(db_path: Path, parquet_dir: Path, min_growth: float = 20.0, topn: int = 50,
                 horizons: list[int] | None = None, announcement_cache: Path | None = None) -> dict:
    horizons = horizons or [5, 10, 20, 60]
    events = load_financial_events(db_path, min_growth=min_growth)
    if events.empty:
        return {"events": [], "summary": {}, "message": "no events"}
    pit_counts = {
        str(key): int(value)
        for key, value in events.get("pit_quality", pd.Series(dtype=str)).value_counts().items()
    }
    native_events = pit_counts.get("native_versions", 0)
    native_share = native_events / len(events) if len(events) else 0.0
    start = (events["ann_dt"].min() - pd.Timedelta(days=10)).strftime("%Y%m%d")
    end = (pd.Timestamp.now() + pd.Timedelta(days=120)).strftime("%Y%m%d")
    codes = set(events["ts_code"].map(c6))
    prices, trade_dates = load_price_panel(parquet_dir, start, end, codes)
    by_code = {code: g.sort_values("trade_date").reset_index(drop=True) for code, g in prices.groupby("c6")}
    market = load_market_returns(parquet_dir, trade_dates, horizons)
    ann_times = load_announcement_times(announcement_cache or ANNOUNCEMENT_CACHE)

    trades = build_trades(events, by_code, market, horizons, topn, ann_times, "conservative")
    timed_trades = build_trades(events, by_code, market, horizons, topn, ann_times, "timed")
    summary, by_year = summarize_trades(trades, horizons)
    timed_summary, timed_by_year = summarize_trades(timed_trades, horizons)
    entry_lag_analysis = build_entry_lag_analysis(events, by_code, market, horizons, topn, lags=[1, 2, 3, 4, 5])
    data_dir = (announcement_cache or ANNOUNCEMENT_CACHE).parent
    rolling_variants = build_rolling_portfolio_variants(timed_trades, by_code, trade_dates, topn=topn, data_dir=data_dir)
    in_sample_best_key = choose_rolling_recommended(
        rolling_variants,
        respect_selection_eligibility=False,
    )
    recommended_key = choose_rolling_recommended(rolling_variants)
    rolling_curve = rolling_variants.get(recommended_key) or rolling_variants.get("base") or {}
    match_counts = {
        "same": sum(1 for t in timed_trades if t.get("ann_date_match") == "same"),
        "nearby": sum(1 for t in timed_trades if t.get("ann_date_match") == "nearby"),
        "missing": sum(1 for t in timed_trades if t.get("ann_date_match") == "missing"),
    }
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "保守口径=公告日后下一交易日开盘买入；时间精确口径=核对巨潮发布时间，盘中发布当日可买，收盘后或非交易日发布顺延下一交易日买入。信号为扣非单季同比>20%且高于上一季度。",
        "params": {"min_growth": min_growth, "topn_per_announcement_day": topn, "horizons": horizons},
        "benchmark": {
            "name": "all_a_equal_weight",
            "entry_price_matched": True,
            "missing_benchmark_returns_are_null": True,
        },
        "publication_gate": {
            "passed": False,
            "status": "research_only",
            "reasons": [
                *([] if native_share == 1.0 else ["financial_event_vintage_not_fully_native"]),
                "historical_tradability_and_capacity_not_modeled",
                "research_variants_compared_on_full_sample",
            ],
        },
        "financial_point_in_time": {
            "quality_counts": pit_counts,
            "native_event_share": round(native_share, 6),
            "fully_native": native_share == 1.0,
        },
        "announcement_time_cache": str(announcement_cache or ANNOUNCEMENT_CACHE),
        "announcement_time_match_counts": match_counts,
        "n_events": len(trades),
        "summary": summary,
        "by_year": by_year,
        "entry_lag_analysis": entry_lag_analysis,
        "curves": build_event_curves(trades, horizons),
        "sample": trades[-50:],
        "timed": {
            "n_events": len(timed_trades),
            "summary": timed_summary,
            "by_year": timed_by_year,
            "curves": build_event_curves(timed_trades, horizons),
            "rolling_portfolio_curve": rolling_curve,
            "rolling_portfolio_recommended": recommended_key,
            "rolling_portfolio_in_sample_best": in_sample_best_key,
            "rolling_portfolio_selection_note": (
                "recommended is restricted to pre-registered eligible variants; "
                "in_sample_best is descriptive and must not drive live allocation"
            ),
            "rolling_portfolio_variants": rolling_variants,
            "sample": timed_trades[-50:],
        },
    }

    picks_by_date: dict[str, list[dict]] = defaultdict(list)
    for r in events.itertuples(index=False):
        ann = r.ann_dt.strftime("%Y%m%d")
        picks_by_date[ann].append({
            "ts_code": r.ts_code,
            "code": c6(r.ts_code),
            "ann_date": ann,
            "end_date": r.end_date,
            "dedt_yoy": float(r.dedt_yoy),
            "prev_dedt_yoy": float(r.prev_dedt_yoy),
            "delta": float(r.dedt_yoy - r.prev_dedt_yoy),
        })

    trades = []
    for ann, rows in sorted(picks_by_date.items()):
        rows = sorted(rows, key=lambda x: (-x["dedt_yoy"], -x["delta"], x["code"]))[:topn]
        for row in rows:
            px = by_code.get(row["code"])
            if px is None or px.empty:
                continue
            dates = px["trade_date"].astype(str).tolist()
            entry_i = next((i for i, d in enumerate(dates) if d > ann), None)
            if entry_i is None:
                continue
            entry_open = float(px.loc[entry_i, "open_adj"])
            if not np.isfinite(entry_open) or entry_open <= 0:
                continue
            rec = dict(row)
            rec["entry_date"] = dates[entry_i]
            for h in horizons:
                j = entry_i + h - 1
                if j >= len(px):
                    rec[f"ret_{h}"] = None
                    rec[f"excess_{h}"] = None
                    continue
                exit_close = float(px.loc[j, "close_adj"])
                raw = exit_close / entry_open - 1.0 if np.isfinite(exit_close) and exit_close > 0 else None
                rec[f"ret_{h}"] = None if raw is None else round(raw * 100, 3)
                bm = market.get((dates[entry_i], h), 0.0)
                rec[f"excess_{h}"] = None if raw is None else round((raw - bm) * 100, 3)
            trades.append(rec)

    summary = {}
    for h in horizons:
        summary[str(h)] = summarize([t[f"ret_{h}"] / 100.0 for t in trades if t.get(f"ret_{h}") is not None], h)
    by_year = {}
    for y in sorted({t["entry_date"][:4] for t in trades}):
        yr = [t for t in trades if t["entry_date"].startswith(y)]
        by_year[y] = {str(h): summarize([t[f"ret_{h}"] / 100.0 for t in yr if t.get(f"ret_{h}") is not None], h)
                      for h in horizons}
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "公告日后下一交易日开盘买入；扣非单季同比>20%且高于上一季度；同一公告日按增速/提升取TopN；当前版本使用正式财务指标公告，未含历史预告上下限。",
        "params": {"min_growth": min_growth, "topn_per_announcement_day": topn, "horizons": horizons},
        "n_events": len(trades),
        "summary": summary,
        "by_year": by_year,
        "sample": trades[-50:],
    }


def run_entry_lag_backtest(
    db_path: Path,
    parquet_dir: Path,
    min_growth: float = 20.0,
    topn: int = 50,
    horizons: list[int] | None = None,
    lags: list[int] | None = None,
    period_suffix: str = "",
) -> dict:
    horizons = horizons or [5, 10, 20, 60]
    lags = lags or [1, 2, 3, 4, 5]
    events = load_financial_events(db_path, min_growth=min_growth)
    suffix = str(period_suffix or "").strip()
    if suffix:
        events = events[events["end_date"].astype(str).str.endswith(suffix)].copy()
    if events.empty:
        return {"events": [], "summary": {}, "message": "no events"}
    start = (events["ann_dt"].min() - pd.Timedelta(days=10)).strftime("%Y%m%d")
    end = (pd.Timestamp.now() + pd.Timedelta(days=max(horizons) + max(lags) + 10)).strftime("%Y%m%d")
    codes = set(events["ts_code"].map(c6))
    prices, trade_dates = load_price_panel(parquet_dir, start, end, codes)
    by_code = {code: g.sort_values("trade_date").reset_index(drop=True) for code, g in prices.groupby("c6")}
    entry_lag_analysis = build_entry_lag_analysis(events, by_code, {}, horizons, topn, lags=lags)
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "同一批业绩事件按公告后第N个交易日开盘买入；信号为扣非单季同比>阈值且高于上一季度；同一公告日按增速和改善幅度取TopN；收益为持有期收盘/入场开盘。",
        "params": {
            "min_growth": min_growth,
            "topn_per_announcement_day": topn,
            "horizons": horizons,
            "entry_lags": lags,
            "period_suffix": suffix,
        },
        "n_source_events": int(len(events)),
        "n_codes": int(len(codes)),
        "date_range": {
            "ann_start": events["ann_date"].min(),
            "ann_end": events["ann_date"].max(),
            "price_start": start,
            "price_end": end,
            "n_trade_dates": len(trade_dates),
        },
        "entry_lag_analysis": entry_lag_analysis,
    }


def build_argument_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=r"\/app/data\financials.db")
    ap.add_argument("--parquet-dir", default=r"\/app/qlib_data\csv_tmp\tushare_daily")
    ap.add_argument("--min-growth", type=float, default=20.0)
    ap.add_argument("--topn", type=int, default=50)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--announcement-cache", default=str(ANNOUNCEMENT_CACHE))
    ap.add_argument("--entry-lag-only", action="store_true", help="only run announcement-day entry lag comparison")
    ap.add_argument("--entry-lags", default="1,2,3,4,5", help="comma-separated trading-day lags after announcement")
    ap.add_argument("--period-suffix", default="", help="filter financial end_date by suffix, e.g. 0630 for interim reports")
    ap.add_argument("--lock-file", default=str(DEFAULT_LOCK))
    ap.add_argument("--status-file", default=str(DEFAULT_STATUS))
    ap.add_argument("--lock-wait-seconds", type=int, default=0)
    ap.add_argument("--reason", default="manual-cli")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    lock_path = Path(args.lock_file)
    status_path = Path(args.status_file)
    announcement_path = Path(args.announcement_cache)
    out = Path(args.out)
    try:
        owner = acquire_backtest_lock(lock_path, args.lock_wait_seconds, args.reason)
    except BacktestLockBusy as exc:
        print(json.dumps({
            "state": "busy",
            "message": str(exc),
            "owner": exc.owner,
        }, ensure_ascii=False))
        return LOCK_BUSY_EXIT

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source = file_fingerprint(announcement_path)
    base_status = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "reason": args.reason,
        "started_at": started_at,
        "source": source,
    }
    atomic_write_json(status_path, {
        **base_status,
        "state": "running",
        "stage": "compute",
        "updated_at": started_at,
    })
    try:
        lags = [int(x) for x in str(args.entry_lags or "").split(",") if str(x).strip()]
        if args.entry_lag_only:
            result = run_entry_lag_backtest(
                Path(args.db),
                Path(args.parquet_dir),
                args.min_growth,
                args.topn,
                lags=lags,
                period_suffix=args.period_suffix,
            )
        else:
            result = run_backtest(Path(args.db), Path(args.parquet_dir), args.min_growth, args.topn,
                                  announcement_cache=announcement_path)
        source_after = file_fingerprint(announcement_path)
        if source_after.get("sha256") != source.get("sha256"):
            stale_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            atomic_write_json(status_path, {
                **base_status,
                "state": "stale",
                "stage": "source-changed",
                "updated_at": stale_at,
                "source_current": source_after,
                "message": "announcement data changed during the backtest; result was not published",
            })
            return SOURCE_CHANGED_EXIT
        atomic_write_json(out, result)
        completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_json(status_path, {
            **base_status,
            "state": "done",
            "stage": "complete",
            "updated_at": completed_at,
            "completed_at": completed_at,
            "output": file_fingerprint(out),
        })
        print(json.dumps({
            "n_events": result.get("n_events") or result.get("n_source_events"),
            "summary": result.get("summary"),
            "entry_lag_analysis": {
                k: v.get("summary") for k, v in (result.get("entry_lag_analysis") or {}).items()
            },
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        failed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_json(status_path, {
            **base_status,
            "state": "error",
            "stage": "failed",
            "updated_at": failed_at,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        })
        raise
    finally:
        release_backtest_lock(lock_path, owner)


if __name__ == "__main__":
    raise SystemExit(main())
