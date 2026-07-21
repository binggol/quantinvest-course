from __future__ import annotations

import argparse
import os
import pickle
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts


DEFAULT_CACHE = Path(r"C:\rdagent\_combo_cache_300_long.pkl")
DEFAULT_TOKEN_FILE = Path(r"C:\rdagent\data\.tushare_token")
INDEX_CODES = ("399300.SZ", "000300.SH")


def _token(token_file: Path) -> str:
    value = os.environ.get("TUSHARE_TOKEN", "").strip()
    if value:
        return value
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    raise RuntimeError("missing TUSHARE_TOKEN and token file")


def _latest_iw_date(cache: dict) -> str:
    iw = cache.get("iw")
    if iw is None or len(iw) == 0 or "trade_date" not in iw.columns:
        return ""
    return str(iw["trade_date"].max())


def _fetch_index_weight(pro, start_date: str, end_date: str) -> pd.DataFrame:
    frames = []
    for index_code in INDEX_CODES:
        try:
            df = pro.index_weight(index_code=index_code, start_date=start_date, end_date=end_date)
        except Exception as exc:
            print(f"[members] index_weight failed {index_code}: {exc}", file=sys.stderr)
            continue
        if df is not None and not df.empty:
            frames.append(df)
            print(f"[members] fetched {len(df)} rows from {index_code}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    keep = [c for c in ("trade_date", "con_code") if c in out.columns]
    if len(keep) < 2:
        raise RuntimeError(f"index_weight missing required columns: {list(out.columns)}")
    return out[keep].dropna().drop_duplicates()


def refresh_cache(cache_path: Path, token_file: Path, days_back: int = 45) -> bool:
    if not cache_path.exists():
        raise FileNotFoundError(str(cache_path))

    cache = pickle.load(open(cache_path, "rb"))
    old_iw = cache.get("iw")
    if old_iw is None or len(old_iw) == 0:
        raise RuntimeError("cache has no iw dataframe")

    latest = _latest_iw_date(cache)
    today = datetime.now().strftime("%Y%m%d")
    if latest:
        start_dt = datetime.strptime(latest, "%Y%m%d") - timedelta(days=days_back)
        start = start_dt.strftime("%Y%m%d")
    else:
        start = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")

    pro = ts.pro_api(_token(token_file))
    new_iw = _fetch_index_weight(pro, start, today)
    if new_iw.empty:
        print(f"[members] no new index_weight rows for {start}~{today}; latest remains {latest}")
        return False

    merged = pd.concat([old_iw[["trade_date", "con_code"]], new_iw], ignore_index=True)
    merged["trade_date"] = merged["trade_date"].astype(str)
    merged["con_code"] = merged["con_code"].astype(str)
    merged = merged.drop_duplicates(["trade_date", "con_code"]).sort_values(["trade_date", "con_code"])
    new_latest = str(merged["trade_date"].max())
    if new_latest <= latest:
        print(f"[members] latest unchanged: {latest}")
        return False

    backup = cache_path.with_suffix(cache_path.suffix + f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(cache_path, backup)
    cache["iw"] = merged
    cache["union"] = sorted(set(cache.get("union") or []) | set(merged["con_code"]))
    pickle.dump(cache, open(cache_path, "wb"))
    print(f"[members] updated {cache_path}: {latest} -> {new_latest}; backup={backup.name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    parser.add_argument("--days-back", type=int, default=45)
    args = parser.parse_args()
    try:
        refresh_cache(Path(args.cache), Path(args.token_file), args.days_back)
        return 0
    except Exception as exc:
        print(f"[members] refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
